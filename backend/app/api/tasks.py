"""任务相关接口：创建、探测、列表、详情、日志、取消、重试、删除、手动发布、WebSocket 进度。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.events import event_bus
from app.db import SessionLocal, get_session
from app.models import Task, TaskItem, TaskLog, TaskStatus
from app.pipeline.runner import task_runner
from app.providers import build_downloader, build_publisher
from app.schemas import (
    PIPELINE_META,
    CreateTaskRequest,
    MessageOut,
    PageOut,
    PipelineMeta,
    ProbeEntry,
    ProbeResponse,
    TaskDetailOut,
    TaskItemOut,
    TaskLogOut,
    TaskOut,
)
from app.services.settings_store import (
    DownloadConfig,
    PublishConfig,
    settings_store,
)
from app.utils.text import truncate

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("/meta", response_model=PipelineMeta)
async def pipeline_meta() -> PipelineMeta:
    return PIPELINE_META


@router.post("/probe", response_model=ProbeResponse)
async def probe_url(
    payload: dict,
    session: AsyncSession = Depends(get_session),
) -> ProbeResponse:
    """解析链接，返回视频或合集条目列表（不落库）。"""
    url = str(payload.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="请提供视频或合集链接")

    config = await settings_store.get_section(session, "download")
    downloader = build_downloader(DownloadConfig(**config))
    try:
        result = await downloader.probe(url)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return ProbeResponse(
        source_type=result.source_type,
        title=result.title,
        author=result.author,
        source_id=result.source_id,
        total=len(result.entries),
        entries=[
            ProbeEntry(
                video_id=e.video_id,
                url=e.url or e.webpage_url,
                title=e.title,
                author=e.author,
                duration=e.duration,
                thumbnail=e.thumbnail,
            )
            for e in result.entries
        ],
    )


@router.post("", response_model=TaskDetailOut, status_code=201)
async def create_task(
    payload: CreateTaskRequest,
    session: AsyncSession = Depends(get_session),
) -> TaskDetailOut:
    """创建搬运任务：解析链接 → 建立条目 → 入队执行。"""
    config = await settings_store.load_all(session)
    downloader = build_downloader(DownloadConfig(**config["download"]))
    try:
        probe = await downloader.probe(payload.url)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not probe.entries:
        raise HTTPException(status_code=400, detail="未解析到任何视频，请检查链接")

    entries = probe.entries
    start = max(0, payload.start_index - 1)
    entries = entries[start:]
    if payload.max_items:
        entries = entries[: payload.max_items]

    task = Task(
        title=(payload.title or truncate(probe.title or "未命名搬运任务", 200)),
        source_url=payload.url,
        source_type=probe.source_type,
        source_id=probe.source_id,
        author=probe.author,
        status=TaskStatus.PENDING.value,
        total_items=len(entries),
        options=payload.options.model_dump(exclude_none=True),
        message="已创建，等待执行",
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
            message=f"已解析 {probe.source_type}，共 {len(entries)} 个视频",
        )
    )
    await session.commit()
    await session.refresh(task)
    await event_bus.emit(task.id, "task.created", id=task.id, total_items=task.total_items)

    if payload.auto_start:
        await task_runner.submit(task.id)

    return await _load_detail(session, task.id)


@router.get("", response_model=PageOut)
async def list_tasks(
    status: str | None = Query(default=None, description="按状态过滤，多个用逗号分隔"),
    q: str | None = Query(default=None, description="标题/链接关键词"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> PageOut:
    stmt = select(Task)
    count_stmt = select(func.count(Task.id))

    if status:
        wanted = [s.strip() for s in status.split(",") if s.strip()]
        stmt = stmt.where(Task.status.in_(wanted))
        count_stmt = count_stmt.where(Task.status.in_(wanted))
    if q:
        pattern = f"%{q}%"
        condition = or_(Task.title.ilike(pattern), Task.source_url.ilike(pattern), Task.author.ilike(pattern))
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)

    total = int((await session.execute(count_stmt)).scalar() or 0)
    rows = (
        await session.execute(
            stmt.order_by(Task.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()

    return PageOut(
        total=total,
        page=page,
        page_size=page_size,
        items=[TaskOut.model_validate(row) for row in rows],
    )


@router.get("/{task_id}", response_model=TaskDetailOut)
async def get_task(task_id: int, session: AsyncSession = Depends(get_session)) -> TaskDetailOut:
    return await _load_detail(session, task_id)


@router.get("/{task_id}/logs", response_model=list[TaskLogOut])
async def get_logs(
    task_id: int,
    item_id: int | None = Query(default=None),
    limit: int = Query(default=300, ge=1, le=2000),
    session: AsyncSession = Depends(get_session),
) -> list[TaskLogOut]:
    stmt = select(TaskLog).where(TaskLog.task_id == task_id)
    if item_id is not None:
        stmt = stmt.where(TaskLog.item_id == item_id)
    rows = (
        await session.execute(stmt.order_by(TaskLog.id.desc()).limit(limit))
    ).scalars().all()
    return [TaskLogOut.model_validate(row) for row in reversed(rows)]


@router.post("/{task_id}/cancel", response_model=MessageOut)
async def cancel_task(task_id: int, session: AsyncSession = Depends(get_session)) -> MessageOut:
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    canceled = await task_runner.cancel(task_id)
    if not canceled and task.status not in (TaskStatus.PENDING.value, TaskStatus.RUNNING.value):
        raise HTTPException(status_code=400, detail=f"任务当前状态为 {task.status}，无需取消")
    if not canceled:
        # 未在执行（排队中）：任务与尚未开始的条目一起标记为已取消，
        # 否则条目会停留在 pending，导致「重试失败项」无对象可选。
        task.status = TaskStatus.CANCELED.value
        task.message = "已取消（未开始执行）"
        task.finished_at = datetime.now()
        items = (
            await session.execute(
                select(TaskItem).where(
                    TaskItem.task_id == task_id,
                    TaskItem.status.in_([TaskStatus.PENDING.value, TaskStatus.RUNNING.value]),
                )
            )
        ).scalars().all()
        for item in items:
            item.status = TaskStatus.CANCELED.value
            item.message = "已取消"
        await session.commit()
        await event_bus.emit(task_id, "task.finished", status=task.status, message=task.message)
    return MessageOut(message="已发送取消指令，正在等待当前阶段结束…" if canceled else "任务已取消")


@router.post("/{task_id}/retry", response_model=TaskDetailOut)
async def retry_task(
    task_id: int,
    only_failed: bool = Query(default=True),
    session: AsyncSession = Depends(get_session),
) -> TaskDetailOut:
    """重试任务。默认只重跑失败/取消的条目，已完成的条目会被跳过（含已下载文件与已合成语音）。"""
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task_runner.is_running(task_id):
        raise HTTPException(status_code=409, detail="任务正在执行中")

    if not only_failed:
        # 全量重跑：清空条目状态，但保留磁盘产物以便阶段级跳过
        items = (
            await session.execute(select(TaskItem).where(TaskItem.task_id == task_id))
        ).scalars().all()
        for item in items:
            item.status = TaskStatus.PENDING.value
            item.stage = ""
            item.progress = 0.0
            item.error = ""
            item.message = "等待重跑"
            item.publish_status = ""
    else:
        items = (
            await session.execute(
                select(TaskItem).where(
                    TaskItem.task_id == task_id,
                    TaskItem.status.in_([TaskStatus.FAILED.value, TaskStatus.CANCELED.value]),
                )
            )
        ).scalars().all()
        if not items:
            raise HTTPException(status_code=400, detail="没有失败或取消的条目需要重试")
        for item in items:
            item.status = TaskStatus.PENDING.value
            item.stage = ""
            item.error = ""
            item.message = "等待重试"

    task.status = TaskStatus.PENDING.value
    task.message = "已重新入队"
    task.error = ""
    task.progress = 0.0
    task.finished_at = None
    await session.commit()
    await session.refresh(task)

    await event_bus.emit(task_id, "task.requeued", message=task.message)
    await task_runner.submit(task_id)
    return await _load_detail(session, task_id)


@router.delete("/{task_id}", response_model=MessageOut)
async def delete_task(
    task_id: int,
    remove_files: bool = Query(default=False, description="同时删除磁盘上的下载/成片文件"),
    session: AsyncSession = Depends(get_session),
) -> MessageOut:
    task = await session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task_runner.is_running(task_id):
        raise HTTPException(status_code=409, detail="任务正在执行中，请先取消")

    await session.execute(delete(TaskLog).where(TaskLog.task_id == task_id))
    await session.execute(delete(TaskItem).where(TaskItem.task_id == task_id))
    await session.execute(delete(Task).where(Task.id == task_id))
    await session.commit()

    if remove_files:
        for base in (
            settings.downloads_dir, settings.subtitles_dir, settings.audio_dir,
            settings.outputs_dir, settings.covers_dir,
        ):
            target = base / str(task_id)
            if target.exists():
                await asyncio.to_thread(_rmtree, target)

    event_bus.clear(task_id)
    return MessageOut(message="任务已删除" + ("（含文件）" if remove_files else ""))


@router.post("/{task_id}/items/{item_id}/publish", response_model=TaskItemOut)
async def publish_item(
    task_id: int,
    item_id: int,
    session: AsyncSession = Depends(get_session),
) -> TaskItemOut:
    """手动发布某个已产出的成片（用于关闭自动发布后的人工确认）。"""
    item = await session.get(TaskItem, item_id)
    if item is None or item.task_id != task_id:
        raise HTTPException(status_code=404, detail="条目不存在")
    if not item.output_path:
        raise HTTPException(status_code=400, detail="该条目尚未产出成片")

    output = settings.data_dir / item.output_path
    if not output.exists():
        raise HTTPException(status_code=400, detail=f"成片文件不存在：{output}")

    config = await settings_store.load_all(session)
    publisher = build_publisher(PublishConfig(**config["publish"]), settings.auth_dir / "douyin_default.json")
    from app.providers.base import PublishRequest

    task = await session.get(Task, task_id)
    options = dict(task.options or {}) if task else {}
    from app.pipeline.stages.deliver import resolve_schedule

    schedule_at = resolve_schedule(options, config["publish"])

    cover = settings.data_dir / item.cover_path if item.cover_path else None
    try:
        result = await publisher.publish(
            PublishRequest(
                video_path=output,
                title=item.title_zh or item.title or f"搬运视频 {item.id}",
                tags=list(item.tags or config["publish"].get("default_tags") or []),
                cover_path=cover if cover and cover.exists() else None,
                description=str(options.get("description") or ""),
                schedule_at=schedule_at,
            )
        )
    except Exception as exc:  # noqa: BLE001
        item.publish_status = "failed"
        item.publish_error = str(exc)
        await session.commit()
        raise HTTPException(status_code=502, detail=f"发布失败：{exc}") from exc

    item.publish_status = "published" if result.success else "failed"
    item.publish_url = result.work_url
    item.publish_error = ""
    item.published_at = datetime.now() if result.success else None
    await session.commit()
    await session.refresh(item)
    await event_bus.emit(task_id, "item.updated", id=item.id, task_id=task_id, publish_status=item.publish_status)
    return TaskItemOut.model_validate(item)


@router.get("/{task_id}/items/{item_id}/file")
async def download_item_file(
    task_id: int,
    item_id: int,
    kind: str = Query(default="output", pattern="^(output|cover|subtitle_zh|subtitle_source|video|audio)$"),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    """下载条目产物（成片/封面/字幕等）。"""
    item = await session.get(TaskItem, item_id)
    if item is None or item.task_id != task_id:
        raise HTTPException(status_code=404, detail="条目不存在")

    mapping = {
        "output": item.output_path,
        "cover": item.cover_path,
        "subtitle_zh": item.subtitle_zh_path,
        "subtitle_source": item.subtitle_source_path,
        "video": item.video_path,
        "audio": item.dubbed_audio_path,
    }
    relative = mapping.get(kind) or ""
    if not relative:
        raise HTTPException(status_code=404, detail="该产物不存在")

    path = settings.data_dir / relative
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"文件已被清理：{path.name}")

    return FileResponse(path, filename=path.name)


@router.websocket("/{task_id}/ws")
async def task_ws(websocket: WebSocket, task_id: int) -> None:
    """推送任务进度事件；连接建立时会先补播最近的历史事件。"""
    await websocket.accept()
    queue = await event_bus.subscribe(task_id)
    try:
        # 先推一次当前快照，前端不必额外拉一次详情
        async with SessionLocal() as session:
            detail = await _load_detail_or_none(session, task_id)
        if detail is not None:
            await websocket.send_text(
                json.dumps(
                    {"type": "snapshot", "task_id": task_id, "payload": detail.model_dump(mode="json")},
                    ensure_ascii=False,
                    default=str,
                )
            )

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=25)
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({"type": "ping", "task_id": task_id}))
                continue
            await websocket.send_text(json.dumps(event.to_dict(), ensure_ascii=False, default=str))
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("WebSocket 关闭：%s", exc)
    finally:
        await event_bus.unsubscribe(task_id, queue)
        with contextlib.suppress(Exception):
            await websocket.close()


# --------------------------------------------------------------------------------------
# 内部工具
# --------------------------------------------------------------------------------------


def _rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


async def _load_detail(session: AsyncSession, task_id: int) -> TaskDetailOut:
    detail = await _load_detail_or_none(session, task_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return detail


async def _load_detail_or_none(session: AsyncSession, task_id: int) -> TaskDetailOut | None:
    task = await session.get(Task, task_id)
    if task is None:
        return None
    items = (
        await session.execute(select(TaskItem).where(TaskItem.task_id == task_id).order_by(TaskItem.idx))
    ).scalars().all()
    detail = TaskDetailOut.model_validate(task)
    detail.items = [TaskItemOut.model_validate(item) for item in items]
    return detail
