"""阶段一：获取素材 —— 解析、下载、字幕提取与断句。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.pipeline.context import ItemState, StageContext
from app.providers.base import ProviderError
from app.services.subtitles import (
    merge_into_sentences,
    normalize_cues,
    parse_subtitle_file,
    reindex,
    to_srt,
)
from app.utils import ffmpeg as ffmpeg_utils

logger = logging.getLogger(__name__)


async def stage_probe(ctx: StageContext, state: ItemState) -> None:
    """补齐视频元信息（任务创建时用 flat 探测，这里拿完整信息）。"""
    if state.info.duration and state.info.title:
        await ctx.reporter.item_stage(state.item.id, "probe", 100, "已有视频信息", overall=ctx.overall_for("probe", 100))
        return

    await ctx.reporter.item_stage(state.item.id, "probe", 20, "正在解析视频信息…")
    downloader = ctx.providers["downloader"]
    result = await downloader.probe(state.item.url or state.item.video_id)
    if result.entries:
        state.info = result.entries[0]
    info = state.info
    state.item.video_id = info.video_id or state.item.video_id
    state.item.title = info.title or state.item.title
    state.item.author = info.author or state.item.author
    state.item.duration = info.duration or state.item.duration
    state.item.thumbnail = info.thumbnail or state.item.thumbnail
    state.item.description = info.description or state.item.description
    await ctx.reporter.item_update(
        state.item.id,
        video_id=state.item.video_id,
        title=state.item.title,
        author=state.item.author,
        duration=state.item.duration,
        thumbnail=state.item.thumbnail,
        description=state.item.description,
    )
    await ctx.reporter.item_stage(state.item.id, "probe", 100, "解析完成", overall=ctx.overall_for("probe", 100))
    await ctx.reporter.log(f"解析完成：{state.item.title}", stage="probe", item_id=state.item.id)


async def stage_download(ctx: StageContext, state: ItemState) -> None:
    downloader = ctx.providers["downloader"]
    ctx.reporter.raise_if_canceled()

    # 若已有下载产物（重试场景），直接复用
    if state.item.video_path:
        existing = Path(state.paths.root) / state.item.video_path
        if existing.exists():
            state.paths.video = existing
            state.subtitle_kind = state.stats.get("subtitle_kind", "")
            if state.item.subtitle_source_path:
                sub = Path(state.paths.root) / state.item.subtitle_source_path
                if sub.exists():
                    state.cues_en = reindex(normalize_cues(parse_subtitle_file(sub)))
            await ctx.reporter.item_stage(
                state.item.id, "download", 100, "复用已下载文件", overall=ctx.overall_for("download", 100)
            )
            return

    last_message = ""

    def on_progress(percent: float, message: str) -> None:
        nonlocal last_message
        last_message = message
        # yt-dlp 的 progress_hook 在下载线程里同步执行，这里把上报排回事件循环
        loop = asyncio.get_event_loop()
        loop.call_soon_threadsafe(
            asyncio.create_task,
            ctx.reporter.item_stage(
                state.item.id,
                "download",
                percent,
                message,
                overall=ctx.overall_for("download", percent),
            ),
        )

    await ctx.reporter.item_stage(state.item.id, "download", 0, "开始下载…", overall=ctx.overall_for("download", 0))
    result = await downloader.download(
        state.info,
        state.paths.video_dir,
        progress=on_progress,
        cancel_check=lambda: ctx.reporter.cancel_event.is_set(),
    )

    state.paths.video = result.video_path
    state.subtitle_kind = result.subtitle_kind
    state.stats["subtitle_kind"] = result.subtitle_kind

    # 校验并读取真实媒体信息
    media = await ffmpeg_utils.probe(result.video_path)
    if media.duration:
        state.item.duration = media.duration
        state.info.duration = media.duration
    state.stats["resolution"] = f"{media.width}x{media.height}"
    state.stats["has_audio"] = media.has_audio

    subtitle_path = result.subtitle_path
    cues: list = []
    if subtitle_path and subtitle_path.exists():
        cues = reindex(normalize_cues(parse_subtitle_file(subtitle_path)))

    update: dict = {
        "video_path": ctx.relative(result.video_path),
        "duration": state.item.duration,
        "stats": {**state.item.stats, **state.stats},
    }
    if cues:
        state.paths.subtitle_en.parent.mkdir(parents=True, exist_ok=True)
        state.paths.subtitle_en.write_text(to_srt(cues), encoding="utf-8")
        state.cues_en = cues
        update["subtitle_source_path"] = ctx.relative(state.paths.subtitle_en)
        state.stats["subtitle_source"] = {"kind": result.subtitle_kind, "cues": len(cues)}
    await ctx.reporter.item_update(state.item.id, **update)

    await ctx.reporter.item_stage(
        state.item.id, "download", 100, last_message or "下载完成", overall=ctx.overall_for("download", 100)
    )
    size_mb = result.video_path.stat().st_size / 1024 / 1024
    await ctx.reporter.log(
        f"下载完成：{result.video_path.name}（{size_mb:.1f} MB，{state.stats.get('resolution', '')}）",
        stage="download",
        item_id=state.item.id,
    )


async def stage_subtitle(ctx: StageContext, state: ItemState) -> None:
    """字幕清洗与断句：把碎片化字幕合并成完整句子，便于逐句翻译配音。"""
    await ctx.reporter.item_stage(state.item.id, "subtitle", 20, "整理字幕…", overall=ctx.overall_for("subtitle", 20))

    if not state.cues_en:
        # 没有字幕：标记为「原声直发」，跳过翻译/配音
        state.stats["no_subtitle"] = True
        await ctx.reporter.item_update(state.item.id, stats={**state.item.stats, **state.stats})
        await ctx.reporter.item_stage(
            state.item.id, "subtitle", 100, "无可用字幕，将保留原声", overall=ctx.overall_for("subtitle", 100)
        )
        await ctx.reporter.log(
            "该视频没有英文字幕，已跳过翻译与配音，成片将保留原声（可在原视频开启自动字幕源后重试）",
            level="warning",
            stage="subtitle",
            item_id=state.item.id,
        )
        return

    cfg = ctx.config.merged("video")
    max_duration = float(cfg.get("sentence_max_duration", 8.0))
    max_chars = int(cfg.get("sentence_max_chars", 120))

    merged = reindex(merge_into_sentences(state.cues_en, max_duration=max_duration, max_chars=max_chars))
    state.cues_en = merged
    state.paths.subtitle_en.parent.mkdir(parents=True, exist_ok=True)
    state.paths.subtitle_en.write_text(to_srt(merged), encoding="utf-8")

    source_total = len(state.cues_en)
    state.stats["sentences"] = len(merged)
    await ctx.reporter.item_update(
        state.item.id,
        subtitle_source_path=ctx.relative(state.paths.subtitle_en),
        stats={**state.item.stats, **state.stats},
    )
    await ctx.reporter.item_stage(
        state.item.id,
        "subtitle",
        100,
        f"断句完成，共 {len(merged)} 句",
        overall=ctx.overall_for("subtitle", 100),
    )
    await ctx.reporter.log(
        f"字幕断句完成：{source_total} 条碎片 → {len(merged)} 句",
        stage="subtitle",
        item_id=state.item.id,
    )


async def ensure_video_present(state: ItemState) -> None:
    if not state.paths.video.exists():
        raise ProviderError(f"视频文件缺失：{state.paths.video}")
