"""搬运计划接口：管理计划、手动跑一次、查看计划产出的任务。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Plan, Task, TaskStatus
from app.schemas import (
    MessageOut,
    PageOut,
    PlanCreateRequest,
    PlanOut,
    PlanPageOut,
    PlanRunRequest,
    PlanRunResult,
    PlanUpdateRequest,
    TaskOut,
)
from app.services import plans as plans_service
from app.services import schedule as schedule_service
from app.services.plan_scheduler import plan_scheduler

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/plans", tags=["plans"])


def _to_out(plan: Plan, *, runnable: int = 0) -> PlanOut:
    schedule = dict(plan.schedule or {})
    # 字段兜底：库里可能存着 NULL（旧数据或直接构造的实例），
    # 而 PlanOut 里这些字段是非空字符串，缺失会让整个列表接口 500
    return PlanOut(
        id=plan.id or 0,
        name=plan.name or "",
        source_url=plan.source_url or "",
        source_type=plan.source_type or "video",
        author=plan.author or "",
        selection=dict(plan.selection or {}),
        options=dict(plan.options or {}),
        schedule=schedule,
        schedule_text=schedule_service.describe(schedule),
        enabled=bool(plan.enabled),
        auto_start=bool(plan.auto_start),
        status=plan.status or "idle",
        last_message=plan.last_message or "",
        last_error=plan.last_error or "",
        next_run_at=plan.next_run_at,
        last_run_at=plan.last_run_at,
        run_count=plan.run_count or 0,
        last_task_id=plan.last_task_id,
        created_at=plan.created_at or datetime.now(timezone.utc),
        updated_at=plan.updated_at or plan.created_at or datetime.now(timezone.utc),
    )


def _apply_payload(plan: Plan, payload: PlanCreateRequest | PlanUpdateRequest) -> None:
    """把请求里的字段写进计划（未提供的字段保持不变）。"""
    data = payload.model_dump(exclude_unset=True)

    if "name" in data:
        plan.name = str(data["name"] or "").strip()[:256]
    if "source_url" in data:
        url = str(data["source_url"] or "").strip()
        if not url:
            raise HTTPException(status_code=400, detail="请提供视频/合集链接")
        plan.source_url = url
    if "source_type" in data and data["source_type"]:
        plan.source_type = str(data["source_type"])
    if "author" in data:
        plan.author = str(data["author"] or "")[:256]
    if "selection" in data:
        plan.selection = plans_service.selection_config(data["selection"])
    if "options" in data:
        plan.options = dict(data["options"] or {})
    if "auto_start" in data and data["auto_start"] is not None:
        plan.auto_start = bool(data["auto_start"])
    if "schedule" in data and data["schedule"] is not None:
        spec = schedule_service.normalize(data["schedule"])
        if spec["type"] == "once" and not spec.get("at"):
            raise HTTPException(status_code=400, detail="定时一次需要指定执行时间")
        plan.schedule = spec

    if "enabled" in data and data["enabled"] is not None:
        plan.enabled = bool(data["enabled"])


def _refresh_next_run(plan: Plan) -> None:
    """按当前调度配置与启用状态刷新下次执行时间。

    `plan.enabled` 可能是 None（旧数据或直接构造的实例），这里一并归一，
    否则计划会「看起来启用、却永远不排期」。
    """
    if plan.enabled is None:
        plan.enabled = True
    if not plan.enabled:
        plan.next_run_at = None
        return
    plan.next_run_at = schedule_service.next_run_at(dict(plan.schedule or {}))


@router.get("/meta")
async def plan_meta() -> dict:
    """调度器状态与可选项（前端展示用）。"""
    return {
        "scheduler": plan_scheduler.status(),
        "schedule_types": [
            {"value": "manual", "label": "仅手动运行"},
            {"value": "interval", "label": "按间隔重复"},
            {"value": "daily", "label": "每天定时"},
            {"value": "weekly", "label": "每周定时"},
            {"value": "once", "label": "定时一次"},
        ],
        "selection_modes": [
            {"value": "all", "label": "全部（跳过已搬运过的）"},
            {"value": "first_n", "label": "前 N 个（按合集顺序）"},
            {"value": "latest", "label": "最新 N 个（适合持续更新）"},
            {"value": "range", "label": "指定序号区间"},
            {"value": "selected", "label": "固定勾选的条目"},
        ],
        "weekdays": list(schedule_service.WEEKDAY_LABELS),
        "server_time": datetime.now(timezone.utc).isoformat(),
        "server_timezone": datetime.now().astimezone().tzname() or "local",
    }


@router.get("", response_model=PlanPageOut)
async def list_plans(
    enabled: bool | None = Query(default=None),
    q: str | None = Query(default=None, description="名称/链接关键词"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> PageOut:
    stmt = select(Plan)
    count_stmt = select(func.count(Plan.id))
    if enabled is not None:
        stmt = stmt.where(Plan.enabled.is_(enabled))
        count_stmt = count_stmt.where(Plan.enabled.is_(enabled))
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(Plan.name.ilike(pattern) | Plan.source_url.ilike(pattern))
        count_stmt = count_stmt.where(Plan.name.ilike(pattern) | Plan.source_url.ilike(pattern))

    total = int((await session.execute(count_stmt)).scalar() or 0)
    rows = (
        await session.execute(
            stmt.order_by(Plan.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()
    return PlanPageOut(total=total, page=page, page_size=page_size, items=[_to_out(p) for p in rows])


@router.post("", response_model=PlanOut, status_code=201)
async def create_plan(
    payload: PlanCreateRequest,
    session: AsyncSession = Depends(get_session),
) -> PlanOut:
    plan = Plan(schedule={"type": "manual"}, selection=plans_service.selection_config({}))
    _apply_payload(plan, payload)
    if not plan.name:
        plan.name = plan.source_url[:120]
    _refresh_next_run(plan)
    session.add(plan)
    await session.commit()
    await session.refresh(plan)
    logger.info("已创建搬运计划 #%s：%s", plan.id, plan.name)
    return _to_out(plan)


@router.get("/{plan_id}", response_model=PlanOut)
async def get_plan(plan_id: int, session: AsyncSession = Depends(get_session)) -> PlanOut:
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="计划不存在")
    return _to_out(plan)


@router.put("/{plan_id}", response_model=PlanOut)
async def update_plan(
    plan_id: int,
    payload: PlanUpdateRequest,
    session: AsyncSession = Depends(get_session),
) -> PlanOut:
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="计划不存在")
    _apply_payload(plan, payload)
    if not plan.name:
        plan.name = plan.source_url[:120]
    _refresh_next_run(plan)
    await session.commit()
    await session.refresh(plan)
    return _to_out(plan)


@router.post("/{plan_id}/toggle", response_model=PlanOut)
async def toggle_plan(
    plan_id: int,
    enabled: bool = Query(description="true 启用 / false 暂停"),
    session: AsyncSession = Depends(get_session),
) -> PlanOut:
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="计划不存在")
    plan.enabled = enabled
    plan.last_message = "已启用" if enabled else "已暂停（正在执行的任务不受影响）"
    _refresh_next_run(plan)
    if enabled and plan.next_run_at is None and str((plan.schedule or {}).get("type")) == "once":
        # 一次性计划的执行时间已经过去：提示用户改时间，而不是静默地永不执行
        plan.last_message = "已启用，但执行时间已过，请重新设置时间"
    await session.commit()
    await session.refresh(plan)
    return _to_out(plan)


@router.delete("/{plan_id}", response_model=MessageOut)
async def delete_plan(plan_id: int, session: AsyncSession = Depends(get_session)) -> MessageOut:
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="计划不存在")
    # 计划产出的任务不跟着删除：那是已经发生的搬运记录，删掉会让用户丢历史
    await session.execute(delete(Plan).where(Plan.id == plan_id))
    await session.commit()
    return MessageOut(message="计划已删除（它创建过的任务与文件都保留）")


@router.get("/{plan_id}/history", response_model=PageOut)
async def plan_history(
    plan_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> PageOut:
    """该计划创建过的任务（也就是「这个计划都干了什么」）。"""
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="计划不存在")

    count_stmt = select(func.count(Task.id)).where(Task.plan_id == plan_id)
    total = int((await session.execute(count_stmt)).scalar() or 0)
    rows = (
        await session.execute(
            select(Task)
            .where(Task.plan_id == plan_id)
            .order_by(Task.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return PageOut(total=total, page=page, page_size=page_size, items=[TaskOut.model_validate(r) for r in rows])


@router.post("/{plan_id}/run", response_model=PlanRunResult)
async def run_plan_now(
    plan_id: int,
    payload: PlanRunRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> PlanRunResult:
    """手动跑一次计划。

    可以先探测链接让用户勾选条目（`probe_only=true` 只返回候选，不建任务），
    也可以带 `selected_video_ids` 只搬其中几个。
    """
    plan = await session.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="计划不存在")

    request = payload or PlanRunRequest()
    selection = dict(plan.selection or {})

    if request.selected_video_ids:
        selection = {**selection, "mode": "selected", "selected_video_ids": list(request.selected_video_ids)}
    elif request.limit:
        selection = {**selection, "mode": "first_n", "count": max(1, int(request.limit))}
    if request.ignore_uploaded is not None:
        selection = {**selection, "ignore_uploaded": bool(request.ignore_uploaded)}

    try:
        probe = await plans_service.probe_source(session, plan.source_url)
    except Exception as exc:  # noqa: BLE001
        plan.last_error = str(exc)[:4000]
        plan.last_message = f"手动执行失败：{str(exc)[:200]}"
        plan.status = "error"
        await session.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    normalized = plans_service.selection_config(selection)
    taken = (
        await plans_service.taken_video_ids(session, [entry.video_id for entry in probe.entries])
        if normalized.get("ignore_uploaded")
        else set()
    )
    picked = plans_service.pick_entries(probe, normalized, taken=taken)

    if request.probe_only:
        return PlanRunResult(
            ok=True,
            created=0,
            task_id=None,
            message=f"已解析 {probe.source_type}，共 {len(probe.entries)} 个条目，当前会搬运 {len(picked)} 个",
            candidates=[
                {
                    "video_id": entry.video_id,
                    "title": entry.title,
                    "duration": entry.duration,
                    "upload_date": entry.upload_date,
                    "taken": entry.video_id in taken,
                }
                for entry in probe.entries[:200]
            ],
        )

    if not picked:
        plan.last_message = "没有新的可搬运视频（都已在之前的任务里）"
        plan.status = "idle"
        plan.last_run_at = datetime.now(timezone.utc)
        await session.commit()
        return PlanRunResult(ok=True, created=0, task_id=None, message=plan.last_message)

    note = f"由搬运计划 #{plan.id} 手动触发：挑出 {len(picked)} 个视频"
    task = await plans_service.create_task(
        session,
        entries=picked,
        probe=probe,
        url=plan.source_url,
        title=plan.name or None,
        options=dict(plan.options or {}),
        auto_start=plan.auto_start,
        plan_id=plan.id,
        source_note=note,
    )
    plan.status = "idle"
    plan.last_error = ""
    plan.last_message = f"手动执行：已创建任务 #{task.id}（{len(picked)} 个视频）"
    plan.last_task_id = task.id
    plan.last_run_at = datetime.now(timezone.utc)
    plan.run_count += 1
    # 手动执行不应打乱已有排期；但如果因为历史数据导致 next_run_at 为空，
    # 这里补排一次——否则「手动跑过一次」会让这个计划再也自动不触发
    if plan.enabled and plan.next_run_at is None:
        plan.next_run_at = schedule_service.next_run_at(dict(plan.schedule or {}))
    await session.commit()

    if plan.auto_start:
        await plans_service.start_task(task.id)

    return PlanRunResult(
        ok=True,
        created=len(picked),
        task_id=task.id,
        skipped=len(taken),
        message=plan.last_message,
    )
