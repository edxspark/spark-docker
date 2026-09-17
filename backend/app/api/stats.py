"""统计接口：仪表盘概览与近 14 天趋势。"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db import get_session
from app.models import STAGE_LABELS, STAGE_ORDER, Task, TaskItem, TaskStatus
from app.pipeline.runner import task_runner
from app.schemas import StatsOverview, TaskOut

router = APIRouter(prefix="/api/stats", tags=["stats"])

# 这些状态视为「正在处理」
ACTIVE_STATUSES = [TaskStatus.PENDING.value, TaskStatus.RUNNING.value, TaskStatus.PAUSED.value]


def _dir_size_mb(path) -> float:
    if not path.exists():
        return 0.0
    total = 0
    for file in path.rglob("*"):
        if file.is_file():
            try:
                total += file.stat().st_size
            except OSError:
                continue
    return round(total / 1024 / 1024, 1)


@router.get("/overview", response_model=StatsOverview)
async def overview(session: AsyncSession = Depends(get_session)) -> StatsOverview:
    total_tasks = int((await session.execute(select(func.count(Task.id)))).scalar() or 0)
    running_tasks = int(
        (await session.execute(select(func.count(Task.id)).where(Task.status.in_(ACTIVE_STATUSES)))).scalar() or 0
    )
    succeeded_tasks = int(
        (
            await session.execute(
                select(func.count(Task.id)).where(Task.status == TaskStatus.SUCCEEDED.value)
            )
        ).scalar()
        or 0
    )
    failed_tasks = int(
        (await session.execute(select(func.count(Task.id)).where(Task.status == TaskStatus.FAILED.value))).scalar()
        or 0
    )

    total_videos = int((await session.execute(select(func.count(TaskItem.id)))).scalar() or 0)
    published_videos = int(
        (
            await session.execute(
                select(func.count(TaskItem.id)).where(TaskItem.publish_status == "published")
            )
        ).scalar()
        or 0
    )

    items = (await session.execute(select(TaskItem))).scalars().all()
    total_sentences = 0
    total_characters = 0
    total_tokens = 0
    for item in items:
        stats = item.stats or {}
        total_sentences += int((stats.get("translate") or {}).get("sentences") or stats.get("sentences") or 0)
        total_characters += int((stats.get("tts") or {}).get("characters") or 0)
        total_tokens += int((stats.get("translate") or {}).get("tokens") or 0)

    recent = (
        await session.execute(select(Task).order_by(Task.created_at.desc()).limit(8))
    ).scalars().all()

    # 近 14 天趋势
    since = datetime.now() - timedelta(days=13)
    daily_rows = (
        await session.execute(
            select(Task.created_at, Task.status).where(Task.created_at >= since)
        )
    ).all()
    buckets: dict[str, dict[str, int]] = {}
    for offset in range(14):
        day = (since + timedelta(days=offset)).strftime("%Y-%m-%d")
        buckets[day] = {"date": day, "created": 0, "succeeded": 0, "failed": 0}
    for created_at, status in daily_rows:
        if created_at is None:
            continue
        key = created_at.strftime("%Y-%m-%d")
        if key not in buckets:
            continue
        buckets[key]["created"] += 1
        if status == TaskStatus.SUCCEEDED.value:
            buckets[key]["succeeded"] += 1
        elif status in (TaskStatus.FAILED.value, TaskStatus.PARTIAL.value):
            buckets[key]["failed"] += 1

    return StatsOverview(
        total_tasks=total_tasks,
        running_tasks=running_tasks,
        succeeded_tasks=succeeded_tasks,
        failed_tasks=failed_tasks,
        total_videos=total_videos,
        published_videos=published_videos,
        total_sentences=total_sentences,
        total_characters=total_characters,
        total_tokens=total_tokens,
        disk_usage_mb=_dir_size_mb(settings.data_dir),
        recent_tasks=[TaskOut.model_validate(task) for task in recent],
        daily=list(buckets.values()),
    )


@router.get("/runtime")
async def runtime() -> dict:
    """当前进程正在执行的任务 id，用于前端高亮。"""
    return {"running_task_ids": task_runner.running_task_ids()}


def _percentile(values: list[float], ratio: float) -> float:
    """线性插值分位数。样本很少时也能给个稳定的值，不返回 None。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = ratio * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


@router.get("/stage-timings")
async def stage_timings(limit: int = 200, session: AsyncSession = Depends(get_session)) -> dict:
    """按阶段聚合耗时，用来决定「下一步该优化哪个节点」。

    背景：一次搬运里各阶段耗时差着数量级，只看任务总时长看不出该优化哪里。
    数据来自每个条目 stats["timings"]（由 TaskRunner 在阶段边界打点）。

    两个刻意的设计：
      - **跳过的阶段不计入平均值**。断点续跑会跳过已有产物的阶段，它们没有真实耗时；
        若按 0 秒算进去，会把真正的热点稀释掉（例如 TTS 明明要 5 分钟，
        却因为多数条目复用了缓存而显示成「平均 20 秒」）。
      - **返回 share（占比）**。绝对值受视频长短影响，占比才能回答
        「时间都花在哪一步」。
    """
    capped = max(1, min(int(limit), 2000))
    rows = (
        await session.execute(
            select(TaskItem.status, TaskItem.stats)
            .order_by(TaskItem.updated_at.desc())
            .limit(capped)
        )
    ).all()

    samples: dict[str, list[float]] = {}
    skipped: dict[str, int] = {}
    totals: list[float] = []
    sampled_items = 0

    for _status, stats in rows:
        blob = stats or {}
        timings = blob.get("timings") or {}
        if timings:
            sampled_items += 1
        for stage_name, value in timings.items():
            if not isinstance(value, dict):
                continue
            if value.get("skipped"):
                # 「本轮跳过」与「跑了但极快」是两回事：前者没有测量值，后者有。
                # 用 skipped 标志区分，不能拿「秒数是否为 0」代替——
                # 快速阶段（如解析链接）会被四舍五入成 0.0，那样就被误当成没测。
                skipped[stage_name] = skipped.get(stage_name, 0) + 1
                continue
            seconds = value.get("seconds")
            if isinstance(seconds, (int, float)) and seconds >= 0:
                samples.setdefault(stage_name, []).append(float(seconds))
        total = blob.get("total_seconds")
        if isinstance(total, (int, float)) and total > 0:
            totals.append(float(total))

    grand_total = sum(sum(v) for v in samples.values()) or 1.0
    stages = []
    # 按流水线顺序输出，便于对照「阶段进度」看
    ordered_names = [name for name in STAGE_ORDER if name in samples or name in skipped]
    ordered_names += [name for name in (samples.keys() | skipped.keys()) if name not in ordered_names]
    for name in ordered_names:
        values = samples.get(name, [])
        subtotal = sum(values)
        stages.append(
            {
                "stage": name,
                "label": STAGE_LABELS.get(name, name),
                "samples": len(values),
                "skipped": skipped.get(name, 0),
                "avg_seconds": round(subtotal / len(values), 1) if values else 0.0,
                "p50_seconds": round(_percentile(values, 0.5), 1),
                "p90_seconds": round(_percentile(values, 0.9), 1),
                "max_seconds": round(max(values), 1) if values else 0.0,
                "total_seconds": round(subtotal, 1),
                "share": round(subtotal / grand_total, 4) if values else 0.0,
            }
        )

    stages.sort(key=lambda row: row["total_seconds"], reverse=True)
    return {
        "sampled_items": sampled_items,
        "limit": capped,
        "stages": stages,
        "items": {
            "samples": len(totals),
            "avg_seconds": round(sum(totals) / len(totals), 1) if totals else 0.0,
            "p50_seconds": round(_percentile(totals, 0.5), 1),
            "p90_seconds": round(_percentile(totals, 0.9), 1),
            "max_seconds": round(max(totals), 1) if totals else 0.0,
        },
    }
