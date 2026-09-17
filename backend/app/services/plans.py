"""搬运计划的执行：探测链接 → 挑出要搬的条目 → 创建任务 → 交给流水线。

从 `app/api/tasks.py` 抽出来的原因：创建任务这件事现在有两个调用方
（手动的新建接口、计划到点后的自动执行），逻辑必须只有一份——
尤其是「已搬运过的不重复创建」这条规则，两边行为不一致会让用户看到重复任务。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import event_bus
from app.models import Plan, Task, TaskItem, TaskLog, TaskStatus, utcnow
from app.providers import build_downloader
from app.providers.base import ProbeResult, ProviderError, VideoInfo
from app.services.settings_store import DownloadConfig, settings_store

logger = logging.getLogger(__name__)

SELECTION_MODES = ("all", "first_n", "latest", "range", "selected")

# 视为「上游已经搬过」的条目状态：正在跑或已经跑出结果的都不再重复建
_TAKEN_STATUSES = (
    TaskStatus.PENDING.value,
    TaskStatus.RUNNING.value,
    TaskStatus.PAUSED.value,
    TaskStatus.SUCCEEDED.value,
)


def selection_config(selection: dict[str, Any] | None) -> dict[str, Any]:
    """规整搬运范围配置。"""
    data = dict(selection or {})
    mode = str(data.get("mode") or "all")
    if mode not in SELECTION_MODES:
        mode = "all"

    def _int(key: str, default: int, low: int, high: int) -> int:
        try:
            value = int(data.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(low, min(value, high))

    return {
        "mode": mode,
        "count": _int("count", 1, 1, 500),
        "start": _int("start", 1, 1, 5000),
        "end": _int("end", 0, 0, 5000),
        "page_size": _int("page_size", 20, 1, 200),
        "selected_video_ids": [str(v) for v in (data.get("selected_video_ids") or []) if str(v)],
        "ignore_uploaded": bool(data.get("ignore_uploaded", True)),
    }


def pick_entries(
    probe: ProbeResult,
    selection: dict[str, Any],
    *,
    taken: set[str] | None = None,
) -> list[VideoInfo]:
    """按搬运范围从探测结果里挑条目。

    ignore_uploaded=True 时会跳过 `taken` 里的 video_id（已经搬过的），
    这样「每天搬最新 1 条」不会反复搬同一个视频。
    """
    entries = list(probe.entries)
    mode = selection["mode"]
    taken = taken or set()

    # 顺序很重要：对全量列表，先剔除已搬过的，再按数量截取。
    # 反过来会把「最新的 1 条」固定成那个已经搬过的视频，计划从此再也搬不到新内容。
    def _fresh(items: list[VideoInfo]) -> list[VideoInfo]:
        if not selection.get("ignore_uploaded"):
            return items
        return [entry for entry in items if entry.video_id not in taken]

    if mode == "selected":
        wanted = selection.get("selected_video_ids") or []
        index = {entry.video_id: entry for entry in entries}
        return _fresh([index[vid] for vid in wanted if vid in index])

    if mode == "range":
        # start / end 都是 1 起的「第几个」，且 end 含端点（"第 2 到第 3 个" = 2 个）
        start = max(0, selection["start"] - 1)
        # end 是含端点的「第几个」，所以要减 1 才是切片上界
        end = selection["end"] - 1 if selection["end"] >= selection["start"] else 0
        # 区间是有界选择，数量上限不再叠加，避免用户设了区间却搬不到
        return _fresh(entries[start : end + 1] if selection["end"] >= selection["start"] else entries[start:])

    if mode == "all":
        return _fresh(entries)

    # first_n / latest：同一套「先剔除再取前 N 个」的语义
    return _fresh(entries)[: selection["count"]]


async def probe_source(session: AsyncSession, url: str) -> ProbeResult:
    """用当前下载配置探测链接。"""
    config = await settings_store.get_section(session, "download")
    downloader = build_downloader(DownloadConfig(**config))
    try:
        return await downloader.probe(url)
    except ProviderError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ProviderError(str(exc)) from exc


async def taken_video_ids(session: AsyncSession, video_ids: list[str]) -> set[str]:
    """查询这些 video_id 里哪些已经存在于任务条目中。"""
    if not video_ids:
        return set()
    rows = (
        await session.execute(
            select(TaskItem.video_id).where(
                TaskItem.video_id.in_(video_ids),
                TaskItem.status.in_(_TAKEN_STATUSES),
            )
        )
    ).scalars().all()
    return set(rows)


async def create_task(
    session: AsyncSession,
    *,
    entries: list[VideoInfo],
    probe: ProbeResult,
    url: str,
    title: str = "",
    options: dict[str, Any] | None = None,
    auto_start: bool = True,
    plan_id: int | None = None,
    source_note: str = "",
) -> Task:
    """按给定条目创建任务（条目已挑选完毕），并可选立即入队执行。

    调用方负责 commit；本函数只 flush 以便拿到 task.id。
    """
    from app.utils.text import truncate

    task = Task(
        title=title or truncate(probe.title or "未命名搬运任务", 200),
        source_url=url,
        source_type=probe.source_type,
        source_id=probe.source_id,
        author=probe.author,
        status=TaskStatus.PENDING.value,
        total_items=len(entries),
        options=dict(options or {}),
        message="已创建，等待执行" if not plan_id else "由搬运计划创建，等待执行",
        plan_id=plan_id,
    )
    session.add(task)
    await session.flush()

    for index, entry in enumerate(entries):
        session.add(
            TaskItem(
                task_id=task.id,
                idx=index,
                video_id=entry.video_id,
                url=entry.url or entry.webpage_url,
                title=entry.title,
                author=entry.author,
                duration=entry.duration,
                thumbnail=entry.thumbnail,
                description=entry.description,
                upload_date=entry.upload_date,
                view_count=entry.view_count,
                status=TaskStatus.PENDING.value,
                message="等待执行",
            )
        )

    session.add(
        TaskLog(
            task_id=task.id,
            stage="probe",
            message=source_note or f"已解析 {probe.source_type}：共 {len(entries)} 个视频",
        )
    )
    await session.flush()
    return task


async def start_task(task_id: int) -> None:
    """提交给流水线执行（幂等：已在运行则复用）。"""
    from app.pipeline.runner import task_runner

    await task_runner.submit(task_id)


async def run_plan(session: AsyncSession, plan: Plan, *, trigger: str) -> dict[str, Any]:
    """执行一次计划：探测 → 挑条目 → 建任务 → 入队。

    返回执行结果摘要（供接口与日志使用）。异常由调用方兜底并写入 plan.last_error。
    """
    from app.utils.text import truncate

    started = utcnow()
    plan.status = "running"
    plan.last_message = f"开始执行（{trigger}）"
    await session.commit()

    probe = await probe_source(session, plan.source_url)
    selection = selection_config(plan.selection)

    taken: set[str] = set()
    if selection.get("ignore_uploaded"):
        taken = await taken_video_ids(session, [entry.video_id for entry in probe.entries])

    picked = pick_entries(probe, selection, taken=taken)
    note = (
        f"由搬运计划 #{plan.id} 触发：从 {len(probe.entries)} 个视频中挑出 {len(picked)} 个"
        + (f"（已跳过上游已搬运的 {len(taken)} 个）" if taken else "")
    )

    if not picked:
        plan.status = "idle"
        plan.last_message = "没有新的可搬运视频（都已在之前的任务里）"
        plan.last_run_at = datetime.now(timezone.utc)
        plan.run_count += 1
        plan.next_run_at = _advance(plan)
        await session.commit()
        return {"ok": True, "created": 0, "task_id": None, "message": plan.last_message}

    task = await create_task(
        session,
        entries=picked,
        probe=probe,
        url=plan.source_url,
        title=plan.name or truncate(probe.title or "", 200),
        options=dict(plan.options or {}),
        auto_start=plan.auto_start,
        plan_id=plan.id,
        source_note=note,
    )

    plan.status = "idle"
    plan.last_error = ""
    plan.last_message = f"已创建任务 #{task.id}（{len(picked)} 个视频）"
    plan.last_task_id = task.id
    plan.last_run_at = datetime.now(timezone.utc)
    plan.run_count += 1
    plan.next_run_at = _advance(plan)
    await session.commit()
    await session.refresh(task)

    await event_bus.emit(task.id, "task.created", id=task.id, total_items=task.total_items)

    if plan.auto_start:
        await start_task(task.id)

    logger.info("计划 #%s 已创建任务 #%s（%s 个视频，%s）", plan.id, task.id, len(picked), trigger)
    return {
        "ok": True,
        "created": len(picked),
        "task_id": task.id,
        "skipped": len(taken),
        "message": plan.last_message,
        "started_at": started.isoformat(),
    }


def _advance(plan: Plan) -> datetime | None:
    """算下一次触发时间；一次性计划执行完自动关闭。"""
    from app.services import schedule as schedule_service

    spec = dict(plan.schedule or {})
    if str(spec.get("type")) == "once":
        plan.enabled = False
        return None
    if not plan.enabled:
        return None
    return schedule_service.next_run_at(spec)
