"""统计接口：仪表盘概览与近 14 天趋势。"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db import get_session
from app.models import Task, TaskItem, TaskStatus
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
