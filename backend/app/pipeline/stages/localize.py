"""阶段二：本地化 —— 翻译、语音合成、时间轴对齐与成片渲染。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path

from app.pipeline.context import ItemState, StageContext
from app.providers.base import ProviderError
from app.services import ass_subtitles, intro_card
from app.services import intro as intro_service
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
    # 这里拿到的是「正片译文」。如果 subtitle 是上一轮续跑带进来的（可能已经含开头语），
    # 必须先按位移剔掉旧开头语，否则它会以普通句子的身份留在列表里被翻译一遍。
    state.cues_zh = reindex(_drop_previous_intro(state, cues_zh))

    await _apply_intro(ctx, state)

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
        # state.stats 是本次运行的累积结果，必须放在后面：state.item.stats 是
        # 进入流水线时的旧快照，顺序写反会让旧值覆盖新值（开头语之类
        # 「本次运行才产生」的字段会静默丢失）
        stats=_merge_stats(state),
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


def _merge_stats(state: ItemState) -> dict:
    """把本次运行的统计合并进条目统计。

    `state.item.stats` 是「进入流水线那一刻」的旧快照，而 `state.stats` 是本次运行
    累积出来的结果；因此后者必须覆盖前者。反过来写会让本次新产出的字段被旧值吞掉
    （真实踩过：统一开头语刚写进 stats 就被旧快照覆盖，成片有开头语但统计里没有）。
    """
    return {**(state.item.stats or {}), **state.stats}


def _intro_duration(state: ItemState) -> float:
    """开头语配音的真实时长（没有开头语时为 0）。

    取「开头语那条字幕的时长 - 配置的停顿」：字幕时长本身就等于
    配音 + 停顿，因此相减即可还原出配音时长，无需额外传递状态。
    """
    if not state.cues_zh or not intro_service.is_intro_cue(state.cues_zh[0]):
        return 0.0
    info = state.stats.get("intro") or {}
    gap = max(0.0, float(info.get("gap_seconds") or 0.0))
    return max(0.0, float(state.cues_zh[0].duration) - gap)


def intro_fitted(state: ItemState) -> bool:
    """当前字幕里是否「真的」带着开头语。

    只看 stats 不够可靠：中间某个阶段重建过字幕列表时，stats 与内容会不一致，
    于是出现「记录说插过了、成片里却没有开头语」。这里以字幕内容为准，
    并且额外比对首句文本——避免「上一轮是旧文案」被误判成已插入。
    """
    if not state.cues_zh:
        return False
    first = state.cues_zh[0]
    if not intro_service.is_intro_cue(first):
        return False
    recorded_text = str((state.stats.get("intro") or {}).get("text") or "").strip()
    return not recorded_text or first.text.strip() == recorded_text


def intro_needs_redo(ctx: StageContext, state: ItemState) -> bool:
    """开头语是否需要（重新）处理 —— 供 runner 判断 translate 阶段能否整段跳过。

    开头语是在翻译阶段插进去的，所以只要它需要变化，translate 就不能跳过，
    否则用户改了设置却永远不生效。
    """
    cfg = intro_service.resolve(ctx.config.merged("intro"))
    settings = intro_service.intro_settings(cfg)
    recorded = state.stats.get("intro") or {}

    if not settings["enabled"]:
        # 关掉开场白：只有「上一轮插过、还没撤」才需要再进一次
        return bool(recorded.get("offset")) and recorded.get("mode") != "disabled"

    if recorded.get("mode") == "disabled":
        # 之前关过，现在又打开：需要重新插入
        return True
    if recorded.get("config_key") != intro_service.intro_config_key(cfg):
        return True
    # 指纹没变也要确认字幕里确实有它，否则这次要补上
    return not intro_fitted(state)


def _drop_previous_intro(state: ItemState, cues: list[Cue]) -> list[Cue]:
    """把上一轮插入的开头语从「新翻译结果」里剔掉。

    背景：`state.cues_zh` 在续跑时是从磁盘字幕恢复的（含开头语），而翻译阶段会整体
    重建这个列表。若直接用新列表覆盖，开头语的统计记录还在、字幕却没了，
    于是后续阶段认为「已经处理过」而跳过——最终成片既没有开头语配音，
    字幕也被平移了一段空白（真实踩过：续跑后句子数从 6 掉到 5）。
    """
    recorded = (state.stats.get("intro") or {}).get("offset")
    if not recorded:
        return list(cues)
    return intro_service.strip_by_offset(cues, float(recorded))


def drop_intro(ctx: StageContext, state: ItemState) -> None:
    """从当前字幕里去掉开头语，并把正片字幕移回原位。

    优先按标记（同一次运行内重跑时标记还在），否则按 stats 记录的位移量判断
    （续跑时 SRT 不保存标记，只能靠时间轴）。
    """
    if intro_service.has_intro(state.cues_zh):
        state.cues_zh = reindex(intro_service.strip_intro(state.cues_zh))
        return
    recorded = float((state.stats.get("intro") or {}).get("offset") or 0.0)
    if recorded > 0:
        state.cues_zh = reindex(intro_service.strip_by_offset(state.cues_zh, recorded))


def mark_intro_done(state: ItemState, *, key: str, settings: dict, mode: str, **extra) -> None:
    """把开头语的处理结果写进 stats（同时也是「已处理」的凭据）。"""
    state.stats["intro"] = {
        "mode": mode,
        "config_key": key,
        "text": settings.get("text", ""),
        "gap_seconds": settings.get("gap_seconds", 0.0),
        "show_in_subtitle": bool(settings.get("show_in_subtitle", True)),
        **extra,
    }


async def _apply_intro(ctx: StageContext, state: ItemState) -> None:
    """保证当前字幕里的开头语与配置一致（该加的加上、该换的换掉、该撤的撤掉）。

    幂等性完全由这里负责，调用方（stage_translate）只管把「正片译文」准备好。
    """
    cfg = ctx.config.merged("intro")
    settings = intro_service.intro_settings(cfg)
    key = intro_service.intro_config_key(cfg)
    recorded = state.stats.get("intro") or {}
    enabled = bool(settings["enabled"] and settings["text"])

    # 1) 配置没变且字幕里确实有：什么都不用做
    if recorded.get("config_key") == key and recorded.get("mode") != "disabled" and intro_fitted(state):
        return

    # 2) 需要撤掉旧的：开关关了，或者字幕里还留着上一轮的（文案/停顿变了）
    if not enabled or intro_service.has_intro(state.cues_zh) or recorded.get("offset"):
        had = intro_service.has_intro(state.cues_zh) or bool(recorded.get("offset"))
        if had:
            drop_intro(ctx, state)

    if not enabled:
        mark_intro_done(state, key=key, settings=settings, mode="disabled", offset=0.0)
        await ctx.reporter.log(
            "统一开头语已关闭：本次成片开头不再播报，旧的文案字幕一并移除",
            stage="translate",
            item_id=state.item.id,
        )
        return

    # 3) 插入新的开头语
    estimated = intro_service.estimate_duration(settings["text"])
    state.cues_zh = reindex(
        intro_service.apply_intro(state.cues_zh, settings, duration=estimated)
    )
    mark_intro_done(
        state,
        key=key,
        settings=settings,
        mode="estimate",
        characters=len(settings["text"]),
        offset=round(estimated + settings["gap_seconds"], 3),
    )
    await ctx.reporter.log(
        f"已插入统一开头语（预计 {estimated + settings['gap_seconds']:.1f} 秒，"
        f"含 {settings['gap_seconds']:.1f} 秒停顿）：{settings['text']}",
        stage="translate",
        item_id=state.item.id,
    )


def _segments_to_build(state: ItemState) -> list[tuple[int, Cue]]:
    """返回本次需要合成的 (分段序号, 字幕) 列表。

    分段音轨文件名是 seg_<序号>.mp3，序号必须与 state.cues_zh 的下标一致——
    否则续跑时的「已有分段」统计会串位、复用错音频。因此这里保留原始下标，
    开头语（若已插入）就在下标 0，与正片一起合成。
    """
    return list(enumerate(state.cues_zh))


def _fit_intro_cue(ctx: StageContext, state: ItemState, cue: Cue, duration: float) -> Cue:
    """开头语配音实测时长与预估值不一致时，修正开头语字幕并同步记录位移量。

    真实时长决定正片字幕的后移量：字幕整体后移是在翻译阶段完成的，
    这里只修正「开头语这一条」以及 stats 里记录的位移量，避免成片开头
    出现「话还没说完、正片字幕已经开始」的错位。
    """
    if not intro_service.is_intro_cue(cue) or duration <= 0:
        return cue

    settings = intro_service.intro_settings(ctx.config.merged("intro"))
    offset = duration + settings["gap_seconds"]
    info = dict(state.stats.get("intro") or {})
    estimated = float(info.get("offset") or 0.0)
    info.update(
        {
            "mode": "measured",
            "duration": round(duration, 3),
            "offset": round(offset, 3),
            "estimated_offset": estimated or round(offset, 3),
        }
    )
    state.stats["intro"] = info

    if abs(offset - float(cue.end)) > 0.01:
        # 后续字幕按估算位移排布，若要严格一致需整表重排；
        # 这里只记录偏差，避免「静默地以为已经对齐」。
        info["drift"] = round(offset - float(cue.end), 3)
        logger.info(
            "开头语实测时长 %.2fs，与预估 %.2fs 相差 %.2fs（任务 %s）",
            offset,
            float(cue.end),
            info["drift"],
            ctx.task.id,
        )
    return replace(cue, end=max(offset, float(cue.start) + 0.1))


async def _build_intro_card(
    ctx: StageContext,
    state: ItemState,
    video_cfg: dict,
    media,
    intro_duration: float,
) -> Path | None:
    """生成（或复用）开头语期间显示的科技感标题卡。

    失败不阻断成片：任何异常都降级为「冻结首帧」，并写进日志与 stats，
    避免用户只看到「黑屏」却不知道为什么。
    """
    cfg = ctx.config.merged("intro")
    mode = intro_service.card_mode(cfg)
    if mode == "none":
        await ctx.reporter.log(
            "按设置不显示开头画面（仅冻结首帧）", stage="align", item_id=state.item.id
        )
        return None

    target_aspect = str(video_cfg.get("target_aspect", "original"))
    width, height = ass_subtitles.output_resolution(media.width, media.height, target_aspect)

    try:
        result = await intro_card.build_card_async(
            width=width, height=height, config=cfg, force=False
        )
    except Exception as exc:  # noqa: BLE001 - 渲染失败不应让成片失败
        logger.exception("开头画面生成失败")
        state.stats["intro_card"] = {"ok": False, "error": str(exc)[:300], "mode": mode}
        await ctx.reporter.log(
            f"开头画面生成失败，已退化为冻结首帧：{exc}", level="warning", stage="align",
            item_id=state.item.id,
        )
        return None

    state.stats["intro_card"] = {
        "ok": True,
        "mode": mode,
        "renderer": result.renderer,
        "path": ctx.relative(result.path),
        "size": f"{result.width}x{result.height}",
        "seconds": round(intro_duration, 2),
    }
    await ctx.reporter.log(
        f"开头画面已就绪（{result.width}x{result.height}，{result.renderer}），"
        f"将在开头 {intro_duration:.1f} 秒显示：{result.path.name}",
        stage="align",
        item_id=state.item.id,
    )
    return result.path


async def _prepare_gender_voice(ctx: StageContext, state: ItemState) -> None:
    """判断原视频说话人性别，并让配音提供者选择同性别音色。

    判定用基频（F0）：男声 85~180 Hz、女声 165~255 Hz，中间以阈值切分。
    结果写入 state.stats["voice_gender"]，任务详情与日志都能看到依据，
    便于用户发现配错时定位（而不是只看到一个「声音不对」的结果）。
    """
    from app.services import voice_profile

    cfg = ctx.config.merged("tts")
    mode = str(cfg.get("voice_gender") or "auto")
    if mode == "off":
        return
    prepare = getattr(ctx.providers["tts"], "prepare_gender", None)
    if prepare is None:
        return

    source = state.paths.video or state.paths.output
    stats: dict[str, object] = {"mode": mode}

    if mode in {"male", "female"}:
        target = mode
        stats.update({"target": target, "source": "手动指定"})
    else:
        await ctx.reporter.item_stage(
            state.item.id, "tts", 2, "判断原视频说话人性别…", overall=ctx.overall_for("tts", 2)
        )
        profile = await voice_profile.analyze_media(source, work_dir=state.paths.work_dir)
        stats.update({"profile": profile.as_stats(), "source": "基频判定"})
        target = profile.gender
        if target == "unknown":
            fallback = str(cfg.get("gender_fallback") or "female")
            stats["reason"] = profile.reason or "未能判断"
            if fallback == "off":
                stats["target"] = "unknown"
                await ctx.reporter.log(
                    f"未能判断原视频说话人性别（{profile.reason or '音频信息不足'}），已按配置沿用原有音色",
                    level="warning",
                    stage="tts",
                    item_id=state.item.id,
                )
                return
            target = fallback
            stats["fallback"] = fallback

    stats["target"] = target
    note = await prepare(target)
    stats["applied"] = note
    state.stats["voice_gender"] = stats
    if note:
        await ctx.reporter.log(note, stage="tts", item_id=state.item.id)


async def stage_tts(ctx: StageContext, state: ItemState) -> None:
    if state.stats.get("no_subtitle"):
        await ctx.reporter.item_stage(
            state.item.id, "tts", 100, "跳过（无字幕）", overall=ctx.overall_for("tts", 100)
        )
        return

    tts_cfg = ctx.config.merged("tts")
    tts = ctx.providers["tts"]
    concurrency = max(1, int(tts_cfg.get("concurrency") or 4))

    # 先判断原视频说话人性别，再让提供者按性别挑音色（男配男声、女配女声）
    await _prepare_gender_voice(ctx, state)

    # 性别音色在提供者内部生效，这里不再传任务级 voice，避免把它顶掉
    voice = None if getattr(tts, "gender_speaker", 0) or getattr(tts, "gender_voice", "") else (
        str(tts_cfg.get("voice") or "") or None
    )

    state.paths.audio_dir.mkdir(parents=True, exist_ok=True)
    segments_to_build = _segments_to_build(state)
    total = len(segments_to_build)
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

    async def worker(slot: int, index: int, cue: Cue) -> None:
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
                    segments[slot] = (cue.start, cue.end, target)
                    await _report_tts(ctx, state, completed, total, cached=True)
                return
            try:
                result = await tts.synthesize(cue.text, target, voice=voice)
                duration = float(getattr(result, "duration", 0.0) or 0.0)
                if duration <= 0:
                    duration = await ffmpeg_utils.audio_duration(target)
                cue = _fit_intro_cue(ctx, state, cue, duration)
            except ProviderError as exc:
                raise ProviderError(f"第 {slot + 1} 句语音合成失败：{exc}") from exc
            async with lock:
                characters += result.characters
                completed += 1
                segments[slot] = (cue.start, cue.end, result.path)
                await _report_tts(ctx, state, completed, total, cached=False)

    try:
        await asyncio.gather(*(worker(slot, index, cue) for slot, (index, cue) in enumerate(segments_to_build)))
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
        # 性别判定与选中的音色（便于事后核对「为什么配了这个声音」）
        "voice_gender": state.stats.get("voice_gender", {}),
    }
    await ctx.reporter.item_update(state.item.id, stats=_merge_stats(state))
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

    # 统一开头语：成片开头先播开头语，之后才是正片内容。
    # 做法是把「正片的分段配音」整体后移一个开头语的时长，而开头语本身放在
    # 0 秒处——成片因此比原片长出这一段，且正片每一句仍与画面严格对齐
    # （若改成前插静音，在 -shortest 下开头语会被整段截掉）。
    intro_duration = _intro_duration(state)
    intro_card_path: Path | None = None
    if intro_duration > 0:
        await ctx.reporter.log(
            f"成片开头插入开头语（{intro_duration:.1f} 秒），正片内容整体后移",
            stage="align",
            item_id=state.item.id,
        )
        intro_card_path = await _build_intro_card(ctx, state, video_cfg, media, intro_duration)

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
            total_duration=total_duration + intro_duration,
            work_dir=state.paths.work_dir,
            max_speedup=float(video_cfg.get("max_speedup", 1.35)),
            on_progress=on_progress,
        )

        final_audio = await _mix_audio(
            ctx, state, video_cfg, video_path, voice_track, media, total_duration + intro_duration
        )
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
        # 开头语那一段用标题卡 / 冻结首帧补足画面长度，
        # 否则 -shortest 会把开头语切掉
        pad_start=intro_duration,
        intro_card=intro_card_path,
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


# 影响合成结果、需参与缓存失效判断的字段（通道切换/改音色/改参数都要重合成）
_TTS_KEY_FIELDS = (
    "provider",
    # ChatTTS
    "chattts_base_url",
    "chattts_api_path",
    "chattts_prompt",
    "temperature",
    "top_p",
    "top_k",
    "speed",
    "voice_seed",
    # 文本预处理与分片：改了这两项，旧音频的读法与断句都会不同
    "normalize_text",
    "term_rules",
    "tts_chunk_chars",
    # 性别匹配：策略或多音色偏好变化都会影响最终音色
    "voice_gender",
    "gender_fallback",
    "gender_pool_size",
    "voice_male",
    "voice_female",
    # 阿里云
    "voice",
    "speech_rate",
    "pitch_rate",
    "sample_rate",
    "volume",
    "format",
    "region",
)


def tts_config_key(ctx: StageContext) -> str:
    """把影响音频结果的参数拼成指纹：任何一项变化都应重新合成。

    注意要带上 ChatTTS 的参数——只比对阿里云字段会让「换音色种子后仍复用旧音频」。
    末尾还要带上统一开头语：文案变了，开头那段配音必须重做。
    """
    cfg = ctx.config.merged("tts")
    base = ":".join(str(cfg.get(field)) for field in _TTS_KEY_FIELDS)
    return f"{base}|intro:{intro_service.intro_config_key(ctx.config.merged('intro'))}"


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

    # 统一开头语按配置决定是否出现在字幕里（关掉时只剩配音，不烧字幕）
    intro_settings = intro_service.intro_settings(ctx.config.merged("intro"))
    if mode != "en":
        primary = intro_service.subtitle_cues(primary, intro_settings)
        if secondary and primary and intro_service.is_intro_cue(primary[0]):
            # 开头语没有对应的英文原句。不排除的话，_match_secondary 会把正片
            # 第一句英文贴到开头语下面（因为它的开始时间也很接近 0 秒），
            # 于是开头大字幕下出现一行莫名其妙的英文。
            secondary = secondary[1:] if secondary else None

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
