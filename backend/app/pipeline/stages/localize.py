"""阶段二：本地化 —— 翻译、语音合成、时间轴对齐与成片渲染。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.pipeline.context import ItemState, StageContext
from app.providers.base import ProviderError
from app.services import ass_subtitles
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
            "当前翻译服务为 Mock：译文是占位文本，不能直接发布。"
            "如需真实译文，请在「系统配置 → 翻译」把服务改为 DeepSeek 并填写 API Key（凭证已填写时也要切换服务商）",
            level="warning",
            stage="translate",
            item_id=state.item.id,
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
        # 配置指纹：换了翻译服务/模型后，旧译文必须视为无效
        "config_key": translate_config_key(ctx),
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
    tts_config_changed = (state.stats.get("tts") or {}).get("config_key") != tts_config_key(ctx)

    if ctx.config.tts.get("provider") == "mock":
        await ctx.reporter.log(
            "当前语音合成服务为 Mock：产出的是静音占位音轨，成片会几乎没有配音。"
            "如需真实配音，请在「系统配置 → 语音合成」把服务改为「阿里云智能语音交互」"
            "（凭证已填写时也要切换服务商）",
            level="warning",
            stage="tts",
            item_id=state.item.id,
        )

    async def worker(index: int, cue: Cue) -> None:
        nonlocal completed, cached_count, characters
        ctx.reporter.raise_if_canceled()
        async with semaphore:
            target = state.paths.audio_dir / f"seg_{index:04d}.mp3"
            # 断点续跑：已存在的分段直接复用，避免重复计费。
            # 但配置指纹变了（例如从 mock 换成阿里云真实配音）就必须重合成，
            # 否则会把上一次的占位音频当成真实配音用下去。
            reusable = target.exists() and target.stat().st_size > 0 and not tts_config_changed
            if reusable:
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
        # 配置指纹：换了服务商/音色/语速后，旧分段音频必须视为无效
        "config_key": tts_config_key(ctx),
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
        loop = asyncio.get_running_loop()

        def on_progress(percent: float, message: str) -> None:
            try:
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
            except RuntimeError:
                # 事件循环已关闭：忽略上报失败，不影响成片渲染
                pass

        voice_track = state.paths.audio_dir / "voice_track.mp3"
        await ffmpeg_utils.build_timeline_track(
            state.voice_segments,
            voice_track,
            total_duration=total_duration,
            work_dir=state.paths.work_dir,
            max_speedup=float(video_cfg.get("max_speedup", 1.35)),
            on_progress=on_progress,
        )

        final_audio = await _mix_audio(ctx, state, video_cfg, video_path, voice_track, media, total_duration)
        if str(video_cfg.get("original_audio", "remove")) == "remove" and final_audio == voice_track:
            if ctx.config.tts.get("provider") == "mock":
                await ctx.reporter.log(
                    "已按设置丢弃原声，但当前语音合成是 Mock（产出静音），因此成片会没有声音。"
                    "请在「系统配置 → 语音合成」切换到阿里云真实配音后再重跑",
                    level="error",
                    stage="align",
                    item_id=state.item.id,
                )

        state.paths.dub_audio = final_audio
        await ctx.reporter.item_update(
            state.item.id, dubbed_audio_path=ctx.relative(final_audio)
        )

    await ctx.reporter.item_stage(state.item.id, "align", 80, "渲染成片…", overall=ctx.overall_for("align", 80))

    subtitle_for_burn = await _build_subtitle_file(ctx, state, video_cfg, media)

    state.paths.output.parent.mkdir(parents=True, exist_ok=True)
    await ffmpeg_utils.render_final(
        video=video_path,
        audio=final_audio,
        subtitle=subtitle_for_burn,
        out_path=state.paths.output,
        target_aspect=str(video_cfg.get("target_aspect", "original")),
        burn=subtitle_for_burn is not None,
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
        "config_key": render_config_key(ctx),
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


# --------------------------------------------------------------------------------------
# 配置指纹：用于判断已有产物是否仍然有效
#
# 背景（真实事故）：把翻译/配音从 mock 切换为真实服务后，断点续跑仍然复用了
# 上一次的占位译文与静音音频，用户拿到的成片「有中文字幕但没声音」。
# 产物必须与产出它的配置绑定，配置变了就要重做。
# --------------------------------------------------------------------------------------


def translate_config_key(ctx: StageContext) -> str:
    cfg = ctx.config.merged("translator")
    return f"{cfg.get('provider')}:{cfg.get('model')}:{cfg.get('style_prompt', '')[:40]}"


def tts_config_key(ctx: StageContext) -> str:
    cfg = ctx.config.merged("tts")
    return (
        f"{cfg.get('provider')}:{cfg.get('voice')}:{cfg.get('speech_rate')}:"
        f"{cfg.get('sample_rate')}:{cfg.get('volume')}"
    )


def render_config_key(ctx: StageContext) -> str:
    cfg = ctx.config.merged("video")
    return ":".join(
        str(cfg.get(key))
        for key in (
            "target_aspect",
            "burn_subtitles",
            "original_audio",
            "bgm_volume",
            "voice_volume",
            "max_speedup",
            "subtitle_mode",
            "subtitle_max_duration",
            "subtitle_max_chars",
            "subtitle_font_size",
            "subtitle_font_name",
            "subtitle_margin_v",
            "subtitle_alignment",
            "subtitle_outline",
        )
    )


async def _mix_audio(
    ctx: StageContext,
    state: ItemState,
    video_cfg: dict,
    video_path: Path,
    voice_track: Path,
    media,
    total_duration: float,
) -> Path:
    """决定成片的音轨。

    remove（默认）—— 完全丢弃原音轨，成片只有 AI 配音
    keep          —— 原音轨压低后与配音混合
    """
    mode = str(video_cfg.get("original_audio", "remove"))

    if mode == "remove":
        if not media.has_audio:
            pass  # 原片本来就没音轨，没什么可去掉的
        return voice_track

    if not media.has_audio:
        await ctx.reporter.log(
            "原视频没有音轨，成片只包含配音", stage="align", item_id=state.item.id
        )
        return voice_track

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
    return mixed


async def _build_subtitle_file(
    ctx: StageContext,
    state: ItemState,
    video_cfg: dict,
    media,
) -> Path | None:
    """生成用于烧录的 ASS 字幕（支持中英双语），返回文件路径。

    自己生成 ASS 而不是直接烧 SRT：libass 解析 SRT 时会套用 PlayResY=288 的
    默认坐标系，字号与边距被放大约 6 倍。显式指定 PlayRes 后，字号就是真实像素。
    """
    if not video_cfg.get("burn_subtitles", True):
        return None

    mode = str(video_cfg.get("subtitle_mode", "bilingual"))
    primary = state.cues_zh if mode != "en" else state.cues_en
    secondary = state.cues_en if mode == "bilingual" else None

    if not primary:
        # 没有中文字幕时退回英文字幕，总比没有字幕好
        primary = state.cues_en
        secondary = None
        mode = "en"
    if not primary:
        return None

    target_aspect = str(video_cfg.get("target_aspect", "original"))
    width, height = ass_subtitles.output_resolution(media.width, media.height, target_aspect)

    style = ass_subtitles.SubtitleStyle(
        font_name=ffmpeg_utils.resolve_subtitle_font(str(video_cfg.get("subtitle_font_name", "") or "")),
        font_size=int(video_cfg.get("subtitle_font_size", 14)),
        margin_v=int(video_cfg.get("subtitle_margin_v", 40)),
        alignment=str(video_cfg.get("subtitle_alignment", "bottom")),
        outline=int(video_cfg.get("subtitle_outline", 1)),
    )
    content = ass_subtitles.build_ass(
        primary,
        width=width,
        height=height,
        style=style,
        secondary_cues=secondary,
    )

    ass_path = state.paths.subtitle_zh.with_suffix(".ass")
    ass_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path.write_text(content, encoding="utf-8")

    state.stats["subtitle_burn"] = {
        "mode": mode,
        "font": style.font_name,
        "font_size_reference": style.font_size,
        "font_size_actual": style.scaled_font_size(height),
        "margin_v_actual": style.scaled_margin(height),
        "alignment": style.alignment,
        "resolution": f"{width}x{height}",
        "lines": len(primary),
    }
    await ctx.reporter.log(
        f"字幕（{ {'bilingual': '中英双语', 'zh': '仅中文', 'en': '仅英文'}.get(mode, mode) }）："
        f"字号 {style.scaled_font_size(height)}px（1080p 基准 {style.font_size}）、"
        f"位置{ {'bottom': '底部', 'middle': '居中', 'top': '顶部'}.get(style.alignment, style.alignment) }、"
        f"画布 {width}x{height}",
        stage="align",
        item_id=state.item.id,
    )
    return ass_path
