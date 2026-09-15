"""阶段二：本地化 —— 翻译、语音合成、时间轴对齐与成片渲染。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.pipeline.context import ItemState, StageContext
from app.providers.base import ProviderError
from app.services.subtitles import Cue, reindex, to_srt
from app.utils import ffmpeg as ffmpeg_utils
from app.utils.text import normalize_punct

logger = logging.getLogger(__name__)


async def stage_translate(ctx: StageContext, state: ItemState) -> None:
    if state.stats.get("no_subtitle"):
        await ctx.reporter.item_stage(
            state.item.id, "translate", 100, "跳过（无字幕）", overall=ctx.overall_for("translate", 100)
        )
        return

    await ctx.reporter.item_stage(state.item.id, "translate", 5, "开始翻译…", overall=ctx.overall_for("translate", 5))
    translator = ctx.providers["translator"]
    texts = [cue.text for cue in state.cues_en]

    hint = state.item.title or ""
    if ctx.config.translator.get("provider") == "mock":
        await ctx.reporter.log(
            "当前为 Mock 翻译（未配置 DeepSeek Key），译文仅供流程验证", level="warning",
            stage="translate", item_id=state.item.id,
        )

    try:
        result = await translator.translate(texts, hint=hint)
    except ProviderError as exc:
        raise ProviderError(f"翻译失败：{exc}") from exc

    if len(result.texts) != len(texts):
        raise ProviderError(f"翻译结果条数异常：期望 {len(texts)}，实际 {len(result.texts)}")

    cues_zh: list[Cue] = []
    for cue, translated in zip(state.cues_en, result.texts, strict=True):
        text = normalize_punct((translated or cue.text).strip())
        cues_zh.append(Cue(start=cue.start, end=cue.end, text=text))
    state.cues_zh = reindex(cues_zh)

    state.paths.subtitle_zh.parent.mkdir(parents=True, exist_ok=True)
    state.paths.subtitle_zh.write_text(to_srt(state.cues_zh), encoding="utf-8")

    usage = result.usage or {}
    state.stats["translate"] = {
        "sentences": len(state.cues_zh),
        "source_chars": sum(len(t) for t in texts),
        "target_chars": sum(len(c.text) for c in state.cues_zh),
        "tokens": usage.get("total_tokens", 0),
        "provider": getattr(translator, "name", "unknown"),
    }
    await ctx.reporter.item_update(
        state.item.id,
        subtitle_zh_path=ctx.relative(state.paths.subtitle_zh),
        stats={**state.item.stats, **state.stats},
    )
    await ctx.reporter.item_stage(
        state.item.id,
        "translate",
        100,
        f"翻译完成，共 {len(state.cues_zh)} 句",
        overall=ctx.overall_for("translate", 100),
    )
    await ctx.reporter.log(
        f"翻译完成：{len(state.cues_zh)} 句，消耗 {usage.get('total_tokens', 0)} tokens",
        stage="translate",
        item_id=state.item.id,
    )


async def stage_tts(ctx: StageContext, state: ItemState) -> None:
    if state.stats.get("no_subtitle"):
        await ctx.reporter.item_stage(
            state.item.id, "tts", 100, "跳过（无字幕）", overall=ctx.overall_for("tts", 100)
        )
        return

    tts_cfg = ctx.config.merged("tts")
    tts = ctx.providers["tts"]
    voice = str(tts_cfg.get("voice") or "") or None
    concurrency = max(1, int(tts_cfg.get("concurrency") or 4))

    state.paths.audio_dir.mkdir(parents=True, exist_ok=True)
    total = len(state.cues_zh)
    if total == 0:
        raise ProviderError("没有可配音的中文字幕")

    completed = 0
    lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(concurrency)
    segments: list[tuple[float, float, Path] | None] = [None] * total
    cached_count = 0
    characters = 0

    if ctx.config.tts.get("provider") == "mock":
        await ctx.reporter.log(
            "当前为 Mock 语音合成（未配置阿里云凭证），生成的是等长静音占位音轨",
            level="warning", stage="tts", item_id=state.item.id,
        )

    async def worker(index: int, cue: Cue) -> None:
        nonlocal completed, cached_count, characters
        ctx.reporter.raise_if_canceled()
        async with semaphore:
            target = state.paths.audio_dir / f"seg_{index:04d}.mp3"
            # 断点续跑：已存在的分段直接复用，避免重复计费
            if target.exists() and target.stat().st_size > 0:
                async with lock:
                    cached_count += 1
                    completed += 1
                    segments[index] = (cue.start, cue.end, target)
                    await _report_tts(ctx, state, completed, total, cached=True)
                return
            try:
                result = await tts.synthesize(cue.text, target, voice=voice)
            except ProviderError as exc:
                raise ProviderError(f"第 {index + 1} 句语音合成失败：{exc}") from exc
            async with lock:
                characters += result.characters
                completed += 1
                segments[index] = (cue.start, cue.end, result.path)
                await _report_tts(ctx, state, completed, total, cached=False)

    try:
        await asyncio.gather(*(worker(i, cue) for i, cue in enumerate(state.cues_zh)))
    except Exception:
        # 让已成功的分段保留在磁盘，便于重试续跑
        raise

    state.voice_segments = [seg for seg in segments if seg is not None]
    state.stats["tts"] = {
        "segments": len(state.voice_segments),
        "characters": characters,
        "cached_segments": cached_count,
        "voice": tts_cfg.get("voice", ""),
        "provider": getattr(tts, "name", "unknown"),
    }
    await ctx.reporter.item_update(state.item.id, stats={**state.item.stats, **state.stats})
    await ctx.reporter.item_stage(
        state.item.id,
        "tts",
        100,
        f"配音完成，共 {len(state.voice_segments)} 段",
        overall=ctx.overall_for("tts", 100),
    )
    await ctx.reporter.log(
        f"语音合成完成：{len(state.voice_segments)} 段，{characters} 字符"
        + (f"（复用已有 {cached_count} 段）" if cached_count else ""),
        stage="tts",
        item_id=state.item.id,
    )


async def _report_tts(ctx: StageContext, state: ItemState, completed: int, total: int, *, cached: bool) -> None:
    percent = completed / total * 100 if total else 100.0
    message = f"已合成 {completed}/{total} 段" + ("（含缓存复用）" if cached else "")
    await ctx.reporter.item_stage(
        state.item.id, "tts", percent, message, overall=ctx.overall_for("tts", percent)
    )


async def stage_align(ctx: StageContext, state: ItemState) -> None:
    """生成配音音轨 → 与原声混音 → 渲染成片（含字幕烧录与画面比例）。"""
    video_cfg = ctx.config.merged("video")
    await ctx.reporter.item_stage(state.item.id, "align", 5, "对齐配音时间轴…", overall=ctx.overall_for("align", 5))

    video_path = state.paths.video
    if not video_path.exists():
        raise ProviderError(f"视频文件缺失：{video_path}")

    media = await ffmpeg_utils.probe(video_path)
    total_duration = media.duration or state.item.duration or 0.0

    final_audio: Path | None = None
    if state.voice_segments:
        loop = asyncio.get_event_loop()

        def on_progress(percent: float, message: str) -> None:
            loop.call_soon_threadsafe(
                asyncio.create_task,
                ctx.reporter.item_stage(
                    state.item.id,
                    "align",
                    5 + percent * 0.6,
                    message,
                    overall=ctx.overall_for("align", 5 + percent * 0.6),
                ),
            )

        voice_track = state.paths.audio_dir / "voice_track.mp3"
        await ffmpeg_utils.build_timeline_track(
            state.voice_segments,
            voice_track,
            total_duration=total_duration,
            work_dir=state.paths.work_dir,
            max_speedup=float(video_cfg.get("max_speedup", 1.35)),
            on_progress=on_progress,
        )

        if video_cfg.get("keep_bgm", True) and media.has_audio:
            await ctx.reporter.item_stage(
                state.item.id, "align", 70, "混入原声背景…", overall=ctx.overall_for("align", 70)
            )
            mixed = state.paths.audio_dir / "mixed.m4a"
            await ffmpeg_utils.mix_voice_with_bgm(
                voice_track,
                video_path,
                mixed,
                bgm_volume=float(video_cfg.get("bgm_volume", 0.12)),
                voice_volume=float(video_cfg.get("voice_volume", 1.0)),
                total_duration=total_duration,
            )
            final_audio = mixed
        else:
            final_audio = voice_track

        state.paths.dub_audio = final_audio
        await ctx.reporter.item_update(
            state.item.id, dubbed_audio_path=ctx.relative(final_audio)
        )

    await ctx.reporter.item_stage(state.item.id, "align", 80, "渲染成片…", overall=ctx.overall_for("align", 80))

    subtitle_for_burn: Path | None = None
    if state.cues_zh and video_cfg.get("burn_subtitles", True):
        # 烧录用「短行」字幕：长句拆成两行更易读
        subtitle_for_burn = state.paths.subtitle_zh
        if not subtitle_for_burn.exists():
            subtitle_for_burn = None

    style = ffmpeg_utils.build_subtitle_style(
        int(video_cfg.get("subtitle_font_size", 20)),
        str(video_cfg.get("subtitle_font_name", "") or ""),
        int(video_cfg.get("subtitle_margin_v", 60)),
    )
    if subtitle_for_burn is not None:
        chosen_font = ffmpeg_utils.resolve_subtitle_font(
            str(video_cfg.get("subtitle_font_name", "") or "")
        )
        if chosen_font != (video_cfg.get("subtitle_font_name") or ""):
            await ctx.reporter.log(
                f"字幕字体使用「{chosen_font}」（系统可用字体中自动选择；"
                "macOS 的 PingFang SC 无法被 libass 加载，故未采用）",
                stage="align",
                item_id=state.item.id,
            )

    state.paths.output.parent.mkdir(parents=True, exist_ok=True)
    await ffmpeg_utils.render_final(
        video=video_path,
        audio=final_audio,
        subtitle=subtitle_for_burn,
        out_path=state.paths.output,
        target_aspect=str(video_cfg.get("target_aspect", "original")),
        burn=bool(video_cfg.get("burn_subtitles", True)) and subtitle_for_burn is not None,
        style=style,
        crf=int(video_cfg.get("crf", 20)),
        preset=str(video_cfg.get("preset", "medium")),
    )

    # 封面：优先用原视频缩略图，否则从成片抽帧
    cover_path: Path | None = None
    try:
        if state.item.thumbnail:
            cover_path = await ctx.providers["downloader"].fetch_thumbnail(state.info, state.paths.cover)
        if cover_path is None:
            cover_path = await ffmpeg_utils.extract_cover(
                state.paths.output, state.paths.cover, at=min(1.0, max(0.1, total_duration / 10))
            )
    except Exception as exc:  # noqa: BLE001 - 封面失败不影响成片
        logger.warning("封面生成失败：%s", exc)

    out_media = await ffmpeg_utils.probe(state.paths.output)
    state.stats["output"] = {
        "duration": round(out_media.duration, 2),
        "resolution": f"{out_media.width}x{out_media.height}",
        "size_mb": round(state.paths.output.stat().st_size / 1024 / 1024, 2),
        "dubbed": bool(state.voice_segments),
    }

    update: dict = {
        "output_path": ctx.relative(state.paths.output),
        "stats": {**state.item.stats, **state.stats},
    }
    if cover_path and cover_path.exists():
        update["cover_path"] = ctx.relative(cover_path)
    await ctx.reporter.item_update(state.item.id, **update)

    await ctx.reporter.item_stage(
        state.item.id, "align", 100, "成片渲染完成", overall=ctx.overall_for("align", 100)
    )
    await ctx.reporter.log(
        f"成片渲染完成：{state.paths.output.name}（{state.stats['output']['size_mb']} MB，"
        f"{state.stats['output']['resolution']}）",
        stage="align",
        item_id=state.item.id,
    )
