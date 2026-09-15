"""阶段三：交付 —— 生成发布文案、发布到抖音。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from app.pipeline.context import ItemState, StageContext
from app.providers.base import ProviderError, PublishRequest
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
    auto_publish = bool(publish_cfg.get("auto_publish", True))
    if ctx.config.options.get("auto_publish") is not None:
        auto_publish = bool(ctx.config.options["auto_publish"])

    if not auto_publish:
        await ctx.reporter.item_update(state.item.id, publish_status="skipped", message="已跳过发布")
        await ctx.reporter.item_stage(
            state.item.id, "publish", 100, "已跳过自动发布", overall=ctx.overall_for("publish", 100)
        )
        await ctx.reporter.log(
            "任务设置为不自动发布，成片已产出，可在任务详情页手动发布",
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
