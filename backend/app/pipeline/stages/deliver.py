"""阶段三：交付 —— 生成发布文案、发布到抖音。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from app.pipeline.context import ItemState, StageContext
from app.providers.base import ProviderError, PublishRequest
from app.services import cover as cover_mod
from app.utils import ffmpeg as ffmpeg_utils
from app.utils.text import truncate

logger = logging.getLogger(__name__)


async def stage_metadata(ctx: StageContext, state: ItemState) -> None:
    """生成中文标题与话题标签。"""
    publish_cfg = ctx.config.merged("publish")
    await ctx.reporter.item_stage(
        state.item.id, "metadata", 20, "生成标题与话题…", overall=ctx.overall_for("metadata", 20)
    )

    max_title_len = int(publish_cfg.get("title_max_len", 30))
    fallback_tags = list(publish_cfg.get("default_tags") or [])
    translator = ctx.providers["translator"]

    base_title = state.info.title or state.item.title or f"搬运视频 {state.item.id}"
    try:
        title_zh, tags = await translator.generate_metadata(
            title=base_title,
            description=state.info.description or state.item.description or "",
            translated_body=" ".join(cue.text for cue in state.cues_zh[:8]),
            max_title_len=max_title_len,
            fallback_tags=fallback_tags,
        )
    except Exception as exc:  # noqa: BLE001 - 文案生成失败不应中断发布
        logger.warning("标题/话题生成失败，使用兜底值：%s", exc)
        await ctx.reporter.log(
            f"标题与话题生成失败，已使用默认值：{exc}", level="warning", stage="metadata", item_id=state.item.id
        )
        title_zh, tags = truncate(base_title, max_title_len), fallback_tags

    state.item.title_zh = title_zh
    state.item.tags = tags
    state.stats["metadata"] = {"title": title_zh, "tags": tags}
    await ctx.reporter.item_update(
        state.item.id,
        title_zh=title_zh,
        tags=tags,
        stats={**state.item.stats, **state.stats},
    )
    await _generate_cover(ctx, state, title_zh, tags)

    await ctx.reporter.item_stage(
        state.item.id, "metadata", 100, f"标题：{title_zh}", overall=ctx.overall_for("metadata", 100)
    )


def resolve_schedule(options: dict, publish_config: dict) -> str | None:
    """计算定时发布时间：任务选项优先，其次配置里的偏移分钟数。None 或 0 表示立即发布。"""
    explicit = (options or {}).get("schedule_at")
    if explicit:
        return str(explicit)
    offset = (options or {}).get("schedule_offset_minutes")
    if offset is None:
        offset = (publish_config or {}).get("schedule_offset_minutes", 0)
    try:
        offset = int(offset or 0)
    except (TypeError, ValueError):
        offset = 0
    if offset <= 0:
        return None
    return (datetime.now() + timedelta(minutes=offset)).strftime("%Y-%m-%d %H:%M")


async def stage_publish(ctx: StageContext, state: ItemState) -> None:
    publish_cfg = ctx.config.merged("publish")
    # 默认手动发布：未显式开启时只产出成片，条目停在「待发布」等人工确认
    auto_publish = bool(publish_cfg.get("auto_publish", False))
    if ctx.config.options.get("auto_publish") is not None:
        auto_publish = bool(ctx.config.options["auto_publish"])

    if not auto_publish:
        await ctx.reporter.item_update(state.item.id, publish_status="pending", message="待手动发布")
        await ctx.reporter.item_stage(
            state.item.id, "publish", 100, "待手动发布", overall=ctx.overall_for("publish", 100)
        )
        await ctx.reporter.log(
            "成片与文案已产出，等待人工确认发布：在任务详情页点「立即发布」即可上传抖音。"
            "如需全过程自动发布，请到「系统配置 → 发布」打开「自动发布」。",
            stage="publish",
            item_id=state.item.id,
        )
        return

    if not state.paths.output.exists():
        raise ProviderError(f"成片不存在，无法发布：{state.paths.output}")

    await ctx.reporter.item_stage(
        state.item.id, "publish", 10, "正在发布到抖音…", overall=ctx.overall_for("publish", 10)
    )
    await ctx.reporter.item_update(state.item.id, publish_status="publishing")

    schedule_at = resolve_schedule(ctx.config.options, publish_cfg)
    title = state.item.title_zh or truncate(state.info.title or "搬运视频", int(publish_cfg.get("title_max_len", 30)))
    tags = list(state.item.tags or publish_cfg.get("default_tags") or [])
    description = ctx.config.options.get("description") or ""

    request = PublishRequest(
        video_path=state.paths.output,
        title=title,
        tags=tags,
        cover_path=state.paths.cover if state.paths.cover.exists() else None,
        description=description,
        schedule_at=schedule_at,
        headless=bool(publish_cfg.get("headless", False)),
    )

    publisher = ctx.providers["publisher"]
    try:
        result = await publisher.publish(request)
    except ProviderError as exc:
        await ctx.reporter.item_update(
            state.item.id, publish_status="failed", publish_error=str(exc), message=f"发布失败：{exc}"
        )
        raise ProviderError(f"发布失败：{exc}") from exc

    await ctx.reporter.item_update(
        state.item.id,
        publish_status="published" if result.success else "failed",
        publish_url=result.work_url,
        publish_error="",
        published_at=datetime.now() if result.success else None,
        message=result.message,
    )
    await ctx.reporter.item_stage(
        state.item.id, "publish", 100, result.message, overall=ctx.overall_for("publish", 100)
    )
    await ctx.reporter.log(
        f"发布完成：{result.message}" + (f"｜链接 {result.work_url}" if result.work_url else ""),
        stage="publish",
        item_id=state.item.id,
    )

async def _generate_cover(ctx: StageContext, state: ItemState, title: str, tags: list[str]) -> None:
    """生成发布用封面。

    默认直接使用视频原始缩略图：它是创作者为吸引点击专门设计的，
    效果通常好于程序生成的模板。取不到缩略图时才退回生成式设计稿。

    放在 metadata 阶段而不是 align：中文标题与话题标签是在这里才产出的，
    生成式封面（以及失败回退）需要用到它们。
    """
    video_cfg = ctx.config.merged("video")
    source_mode = str(video_cfg.get("cover_source", "thumbnail"))
    target_aspect = str(video_cfg.get("target_aspect", "original"))
    media = None
    video_file = state.paths.output if state.paths.output.exists() else state.paths.video
    if video_file.exists():
        try:
            media = await ffmpeg_utils.probe(video_file)
        except Exception:  # noqa: BLE001
            media = None
    width, height = cover_mod.cover_size_for(
        target_aspect, media.width if media else 0, media.height if media else 0
    )

    # 1) 优先使用视频原始缩略图
    if source_mode == "thumbnail":
        local_thumb = None
        try:
            candidates = [
                p
                for p in state.paths.video_dir.glob(f"{state.item.video_id}.*")
                if p.suffix.lower() in {".webp", ".jpg", ".jpeg", ".png"}
            ]
            if candidates:
                local_thumb = max(candidates, key=lambda p: p.stat().st_size)
        except Exception:  # noqa: BLE001
            local_thumb = None

        data = await cover_mod.fetch_thumbnail_bytes(state.item.thumbnail or "", local_thumb)
        if data:
            try:
                await asyncio.to_thread(
                    cover_mod.build_thumbnail_cover,
                    data,
                    cover_mod.CoverStyle(width=width, height=height),
                    state.paths.cover,
                )
                state.stats["cover"] = {
                    "mode": "thumbnail",
                    "size": f"{width}x{height}",
                    "source_url": cover_mod.best_thumbnail_url(state.item.thumbnail or "")[:200],
                }
                await ctx.reporter.item_update(
                    state.item.id,
                    cover_path=ctx.relative(state.paths.cover),
                    stats={**state.item.stats, **state.stats},
                )
                await ctx.reporter.log(
                    f"已使用视频原始缩略图作为封面：{width}x{height}", stage="metadata", item_id=state.item.id
                )
                return
            except Exception as exc:  # noqa: BLE001
                await ctx.reporter.log(
                    f"缩略图封面生成失败，改用设计封面：{exc}",
                    level="warning", stage="metadata", item_id=state.item.id,
                )

    # 2) 程序生成的科技风设计稿
    source = video_file
    if not source.exists():
        return

    if source_mode == "frame":
        try:
            await ffmpeg_utils.extract_cover(source, state.paths.cover, at=1.0)
            state.stats["cover"] = {"mode": "frame", "size": f"{width}x{height}"}
            await ctx.reporter.item_update(
                state.item.id, cover_path=ctx.relative(state.paths.cover),
                stats={**state.item.stats, **state.stats},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("封面抽取失败：%s", exc)
        return

    style = cover_mod.CoverStyle(
        width=width,
        height=height,
        theme=str(video_cfg.get("cover_theme", "tech_blue")),
        brand=str(video_cfg.get("cover_brand", "") or ""),
        max_tags=int(video_cfg.get("cover_max_tags", 4)),
        background=str(video_cfg.get("cover_background", "generated")),
        source=source_mode,
    )
    try:
        await asyncio.to_thread(
            cover_mod.generate_cover,
            video=source,
            title=state.item.title_zh or title,
            tags=list(state.item.tags or tags),
            out_path=state.paths.cover,
            style=style,
            frame_at=min(1.0, max(0.1, (media.duration if media else 12) / 10)),
            work_dir=state.paths.work_dir / "cover",
        )
    except Exception as exc:  # noqa: BLE001 - 封面失败不应影响发布主流程
        await ctx.reporter.log(
            f"封面生成失败，将退回抽帧：{exc}", level="warning", stage="metadata", item_id=state.item.id
        )
        try:
            await ffmpeg_utils.extract_cover(source, state.paths.cover, at=1.0)
        except Exception:  # noqa: BLE001
            return

    state.stats["cover"] = {"mode": "generated", "size": f"{width}x{height}",
                            "theme": video_cfg.get("cover_theme", "tech_blue")}
    await ctx.reporter.item_update(
        state.item.id, cover_path=ctx.relative(state.paths.cover), stats={**state.item.stats, **state.stats}
    )
    await ctx.reporter.log(
        f"已生成设计封面：{width}x{height}，含标题与 {len(state.item.tags or tags)} 个标签",
        stage="metadata", item_id=state.item.id,
    )
