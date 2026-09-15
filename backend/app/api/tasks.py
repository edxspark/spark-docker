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
    PublishItemRequest,
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
                upload_date=e.upload_date,
                view_count=e.view_count,
            )
            for e in result.entries
        ],
    )


def _describe_selection(probe_total: int, entries: list) -> str:
    """把「用户挑了多少条」写成日志里能看懂的一句话。"""
    if len(entries) >= probe_total:
        return f"共 {len(entries)} 个视频（全部）"
    if probe_total > 0 and len(entries) == 1:
        return f"已挑选 1 个视频（合集共 {probe_total} 个）"
    return f"已挑选 {len(entries)} 个视频（合集共 {probe_total} 个）"


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

    probe_total = len(probe.entries)
    entries = probe.entries
    if payload.selected_video_ids is not None:
        # 前端合集弹窗的勾选结果：按用户勾选的顺序建立条目（避免用户看到的列表
        # 与最终任务条目不一致）。"all" 是弹窗里「全选」的显式确认标记。
        wanted = [str(v).strip() for v in payload.selected_video_ids if str(v).strip()]
        if "all" in wanted:
            entries = list(probe.entries)
        else:
            by_id: dict[str, list] = {}
            for entry in probe.entries:
                by_id.setdefault(entry.video_id, []).append(entry)
            picked: list = []
            for video_id in wanted:
                bucket = by_id.get(video_id)
                if bucket:
                    picked.append(bucket.pop(0))
            if not picked:
                raise HTTPException(
                    status_code=400,
                    detail="勾选的视频都不在本次解析结果中，请重新解析链接后再选择",
                )
            entries = picked
    else:
        start = max(0, payload.start_index - 1)
        entries = entries[start:]
        if payload.max_items:
            entries = entries[: payload.max_items]

    selection_note = _describe_selection(probe_total, entries)

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
            message=f"已解析 {probe.source_type}：{selection_note}",
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

    signalled = await _cancel_one(session, task)
    return MessageOut(message="已发送取消指令，正在等待当前阶段结束…" if signalled else "任务已取消")


@router.post("/cancel-all")
async def cancel_all_tasks(
    include_paused: bool = Query(default=True, description="是否一并取消已暂停的任务"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """一键取消所有未结束的任务（新增/执行中/已暂停）。

    返回被影响的明细，便于前端如实告知用户「发出了几条取消指令、直接标记了几条」。
    """
    wanted = [TaskStatus.PENDING.value, TaskStatus.RUNNING.value]
    if include_paused:
        wanted.append(TaskStatus.PAUSED.value)

    tasks = (
        await session.execute(select(Task).where(Task.status.in_(wanted)).order_by(Task.id))
    ).scalars().all()

    if not tasks:
        return {
            "ok": True,
            "canceled": 0,
            "running_signalled": 0,
            "paused_excluded": 0,
            "task_ids": [],
            "message": "当前没有需要取消的任务",
        }

    signalled = 0
    for task in tasks:
        if await _cancel_one(session, task):
            signalled += 1

    paused_excluded = 0
    if not include_paused:
        paused_excluded = int(
            (
                await session.execute(
                    select(func.count(Task.id)).where(Task.status == TaskStatus.PAUSED.value)
                )
            ).scalar()
            or 0
        )

    parts = []
    if signalled:
        parts.append(f"{signalled} 个执行中的任务已发送取消指令（待当前阶段结束后停止）")
    direct = len(tasks) - signalled
    if direct:
        parts.append(f"{direct} 个排队中的任务已直接取消")
    if paused_excluded:
        parts.append(f"{paused_excluded} 个已暂停的任务未处理")

    return {
        "ok": True,
        "canceled": len(tasks),
        "running_signalled": signalled,
        "paused_excluded": paused_excluded,
        "task_ids": [task.id for task in tasks],
        "message": "；".join(parts) or "已取消",
    }


async def _cancel_one(session: AsyncSession, task: Task) -> bool:
    """取消单个任务。

    返回 True 表示「任务正在执行，已发出取消指令（异步生效）」；
    返回 False 表示「任务尚未开始，已直接标记为已取消」。
    """
    signalled = await task_runner.cancel(task.id)
    if signalled:
        return True

    if task.status not in (
        TaskStatus.PENDING.value,
        TaskStatus.RUNNING.value,
        TaskStatus.PAUSED.value,
    ):
        # 已结束的任务不该走到这里；调用方负责过滤
        return False

    # 未在执行：任务与尚未开始的条目一起标记为已取消，
    # 否则条目会停留在 pending，导致「重试失败项」无对象可选。
    task.status = TaskStatus.CANCELED.value
    task.message = "已取消"
    task.finished_at = datetime.now()
    items = (
        await session.execute(
            select(TaskItem).where(
                TaskItem.task_id == task.id,
                TaskItem.status.in_([TaskStatus.PENDING.value, TaskStatus.RUNNING.value]),
            )
        )
    ).scalars().all()
    for item in items:
        item.status = TaskStatus.CANCELED.value
        item.message = "已取消"
    await session.commit()
    await event_bus.emit(task.id, "task.finished", status=task.status, message=task.message)
    return False


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
    payload: PublishItemRequest | None = None,
    session: AsyncSession = Depends(get_session),
) -> TaskItemOut:
    """手动发布某个已产出的成片（默认关闭自动发布时的确认发布入口）。

    手动发布默认立刻上传；系统配置里的定时延迟只对自动发布生效，
    除非显式传 immediate=false。
    """
    item = await session.get(TaskItem, item_id)
    if item is None or item.task_id != task_id:
        raise HTTPException(status_code=404, detail="条目不存在")
    if not item.output_path:
        raise HTTPException(status_code=400, detail="该条目尚未产出成片")

    output = settings.data_dir / item.output_path
    if not output.exists():
        raise HTTPException(status_code=400, detail=f"成片文件不存在：{output}")

    immediate = True if payload is None else payload.immediate
    dry_run = bool(payload.dry_run) if payload is not None else False

    config = await settings_store.load_all(session)
    publisher = build_publisher(PublishConfig(**config["publish"]), settings.auth_dir / "douyin_default.json")
    from app.providers.base import PublishRequest

    task = await session.get(Task, task_id)
    options = dict(task.options or {}) if task else {}
    from app.pipeline.stages.deliver import resolve_schedule

    schedule_at = None if immediate else resolve_schedule(options, config["publish"])
    if schedule_at:
        # 延迟按配置算，但用户按的是「立即发布」，这里留痕便于排查
        logger.info("手动发布走定时：item=%s schedule_at=%s", item_id, schedule_at)

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
                dry_run=dry_run,
            )
        )
    except Exception as exc:  # noqa: BLE001
        item.publish_status = "failed"
        item.publish_error = str(exc)
        await session.commit()
        raise HTTPException(status_code=502, detail=f"发布失败：{exc}") from exc

    if dry_run:
        # 干跑只是自检：不写入「已发布」，也不记录作品链接
        item.message = f"[干跑] {result.message}"[:400]
        await session.commit()
        await session.refresh(item)
        return TaskItemOut.model_validate(item)

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
    inline: bool = Query(
        default=False,
        description="true 时以 inline 方式返回，供页面内嵌播放；false 则作为附件下载",
    ),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    """获取条目产物（成片/封面/字幕等）。

    Starlette 的 FileResponse 原生支持 Range 请求，因此 <video> 可以正常拖动进度条，
    大文件也不会被整体读进内存。
    """
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

    return FileResponse(
        path,
        filename=path.name,
        content_disposition_type="inline" if inline else "attachment",
    )


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
