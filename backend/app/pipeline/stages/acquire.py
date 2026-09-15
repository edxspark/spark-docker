"""阶段一：获取素材 —— 解析、下载、字幕提取与断句。"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from app.pipeline.context import ItemState, StageContext, make_threadsafe_progress
from app.providers.base import ProviderError
from app.services.subtitles import (
    merge_into_sentences,
    normalize_cues,
    parse_subtitle_file,
    reindex,
    split_long_cues,
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

    # yt-dlp 的 progress_hook 在工作线程里同步执行；必须在协程中先拿到 loop，
    # 否则 hook 会抛 RuntimeError 并中断下载（详见 make_threadsafe_progress 的说明）。
    schedule_progress = make_threadsafe_progress(asyncio.get_running_loop())

    def on_progress(percent: float, message: str) -> None:
        nonlocal last_message
        last_message = message
        schedule_progress(
            ctx.reporter.item_stage(
                state.item.id,
                "download",
                percent,
                message,
                overall=ctx.overall_for("download", percent),
            )
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
    """获取原文：优先用视频自带字幕；没有则用语音识别生成，再断句。"""
    await ctx.reporter.item_stage(state.item.id, "subtitle", 10, "整理字幕…", overall=ctx.overall_for("subtitle", 10))

    if not state.cues_en:
        await _acquire_subtitle_via_asr(ctx, state)

    if not state.cues_en:
        # 既没有字幕也无法识别：保留原声，跳过翻译/配音
        state.stats["no_subtitle"] = True
        await ctx.reporter.item_update(state.item.id, stats={**state.item.stats, **state.stats})
        await ctx.reporter.item_stage(
            state.item.id, "subtitle", 100, "无可用字幕，将保留原声", overall=ctx.overall_for("subtitle", 100)
        )
        await ctx.reporter.log(
            "该视频没有字幕，语音识别也未能生成内容，成片将保留原声。"
            "可在「系统配置 → 语音识别」中检查凭证与识别模型后重试。",
            level="warning",
            stage="subtitle",
            item_id=state.item.id,
        )
        return

    cfg = ctx.config.merged("video")
    # 断句与显示使用同一组上限：先按句合并碎片，再把仍然过长的条目切开。
    # 两者共用一套参数，避免「合并到 8 秒、显示又要求 4 秒」这种自相矛盾。
    max_duration = float(cfg.get("subtitle_max_duration", 5.0))
    max_chars = int(cfg.get("subtitle_max_chars", 84))

    source_total = len(state.cues_en)
    if state.stats.get("asr_timed"):
        # 语音识别已给出词级时间轴：边界是真实的，不要再按标点重新合并
        split = split_long_cues(state.cues_en, max_duration=max_duration, max_chars=max_chars)
    else:
        merged = merge_into_sentences(state.cues_en, max_duration=max_duration, max_chars=max_chars)
        split = split_long_cues(merged, max_duration=max_duration, max_chars=max_chars)
    state.cues_en = reindex(split)
    merged = state.cues_en
    state.paths.subtitle_en.parent.mkdir(parents=True, exist_ok=True)
    state.paths.subtitle_en.write_text(to_srt(merged), encoding="utf-8")

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


async def _acquire_subtitle_via_asr(ctx: StageContext, state: ItemState) -> None:
    """视频没有字幕时，用语音识别生成原文，后续照常翻译、配音、替换音轨。"""
    asr_cfg = ctx.config.merged("asr")
    if not asr_cfg.get("enabled", True):
        await ctx.reporter.log(
            "该视频没有字幕，且「语音识别」已关闭，将保留原声",
            level="warning",
            stage="subtitle",
            item_id=state.item.id,
        )
        return

    video = state.paths.video
    if not video.exists():
        return

    factory = ctx.providers.get("asr_factory")
    if factory is None:
        return
    try:
        asr = factory()
    except Exception as exc:  # noqa: BLE001 - 兜底能力不可用时不应中断任务
        # 凭证缺失这类是确定性失败，标记后不再每轮重试都白跑一次
        state.stats["asr_error"] = str(exc)[:500]
        state.stats["asr_done"] = True
        await ctx.reporter.item_update(state.item.id, stats={**state.item.stats, **state.stats})
        await ctx.reporter.log(
            f"该视频没有字幕，但语音识别不可用（{exc}），将保留原声",
            level="warning",
            stage="subtitle",
            item_id=state.item.id,
        )
        return

    # 注意：asr_done 只在「识别完成」或「确定性失败」时置位。
    # 若在开始时置位，任务被中断后重试会直接跳过识别，用户永远拿不到配音
    # （实际发生过：服务重载打断了识别，重试却因为该标记而跳过）。
    provider = getattr(asr, "name", "unknown")
    duration = state.item.duration or 0.0
    await ctx.reporter.item_stage(
        state.item.id,
        "subtitle",
        25,
        "无字幕，正在语音识别…",
        overall=ctx.overall_for("subtitle", 25),
    )
    await ctx.reporter.log(
        f"该视频没有字幕，改用语音识别生成原文（{provider}，约 {duration / 60:.0f} 分钟音频，可能需要较长时间）",
        stage="subtitle",
        item_id=state.item.id,
    )

    loop = asyncio.get_running_loop()
    schedule = make_threadsafe_progress(loop)

    def on_progress(percent: float, message: str) -> None:
        # 字幕阶段内占 25%~85%，其余留给断句
        schedule(
            ctx.reporter.item_stage(
                state.item.id,
                "subtitle",
                25 + max(0.0, min(1.0, percent)) * 60,
                message,
                overall=ctx.overall_for("subtitle", 25 + max(0.0, min(1.0, percent)) * 60),
            )
        )

    started = time.monotonic()
    try:
        cues = await asr.transcribe(
            video,
            on_progress=on_progress,
            total_duration=duration or None,
            # 分块结果落盘：中断后重试可直接复用，不必重新识别（省钱也省时间）
            cache_dir=state.paths.work_dir / "asr",
        )
    except Exception as exc:  # noqa: BLE001 - 识别失败仍要产出成片（保留原声）
        state.stats["asr_error"] = str(exc)[:500]
        state.stats["asr_done"] = True
        await ctx.reporter.item_update(state.item.id, stats={**state.item.stats, **state.stats})
        await ctx.reporter.log(
            f"语音识别失败，将保留原声：{exc}",
            level="error",
            stage="subtitle",
            item_id=state.item.id,
        )
        return

    if not cues:
        state.stats["asr_error"] = "语音识别未返回任何内容"
        state.stats["asr_done"] = True
        await ctx.reporter.item_update(state.item.id, stats={**state.item.stats, **state.stats})
        return

    elapsed = time.monotonic() - started
    state.cues_en = reindex(normalize_cues(cues))
    state.stats["asr_done"] = True
    # 记录时间轴是否来自词级识别：为真时后续跳过按标点的碎片合并
    state.stats["asr_timed"] = bool(getattr(asr, "cues_are_timed", False))
    state.stats["subtitle_source"] = {"kind": "asr", "cues": len(state.cues_en), "provider": provider}
    state.stats["asr"] = {
        "provider": provider,
        "segments": len(state.cues_en),
        "elapsed_seconds": round(elapsed, 1),
        "characters": sum(len(c.text) for c in state.cues_en),
    }
    state.stats.pop("no_subtitle", None)
    await ctx.reporter.item_update(state.item.id, stats={**state.item.stats, **state.stats})
    await ctx.reporter.log(
        f"语音识别完成：生成 {len(state.cues_en)} 条原文，耗时 {elapsed:.0f} 秒，"
        "接下来将翻译并替换原音轨",
        stage="subtitle",
        item_id=state.item.id,
    )
