"""「取消所有任务」的接口测试。

覆盖三种任务形态的混合场景：
- 排队中（pending）：直接标记取消，条目也要同步，否则无法重试
- 执行中（running）：只能发取消指令（异步生效）
- 已暂停（paused）：可选择性保留
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime

import httpx
import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import Task, TaskItem, TaskStatus
from app.pipeline.runner import task_runner


@pytest.fixture
async def client(tmp_root):
    await init_db()
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def _make_task(status: str, *, title: str, with_item: bool = True) -> int:
    async with SessionLocal() as session:
        task = Task(
            title=title,
            source_url="https://www.youtube.com/watch?v=x",
            source_type="video",
            status=status,
            total_items=1 if with_item else 0,
        )
        session.add(task)
        await session.flush()
        if with_item:
            session.add(
                TaskItem(
                    task_id=task.id,
                    idx=0,
                    video_id=f"v{task.id}",
                    url=task.source_url,
                    title=title,
                    status=status,
                )
            )
        await session.commit()
        return task.id


async def _cleanup() -> None:
    from sqlalchemy import delete

    async with SessionLocal() as session:
        await session.execute(delete(TaskItem))
        await session.execute(delete(Task))
        await session.commit()


async def _status(task_id: int) -> tuple[str, list[str]]:
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        items = (
            await session.execute(select(TaskItem).where(TaskItem.task_id == task_id))
        ).scalars().all()
    assert task is not None
    return task.status, [item.status for item in items]


class TestCancelAllEndpoint:
    async def test_cancels_pending_tasks_and_items(self, client):
        await _cleanup()
        try:
            first = await _make_task(TaskStatus.PENDING.value, title="排队中的任务A")
            second = await _make_task(TaskStatus.PENDING.value, title="排队中的任务B")

            response = await client.post("/api/tasks/cancel-all")
            assert response.status_code == 200
            body = response.json()

            assert body["canceled"] == 2
            assert body["running_signalled"] == 0
            assert sorted(body["task_ids"]) == sorted([first, second])
            assert "已直接取消" in body["message"]

            for task_id in (first, second):
                status, item_statuses = await _status(task_id)
                assert status == TaskStatus.CANCELED.value
                # 条目必须同步取消，否则「重试失败项」没有对象可选
                assert item_statuses == [TaskStatus.CANCELED.value]
        finally:
            await _cleanup()

    async def test_noop_when_nothing_active(self, client):
        await _cleanup()
        try:
            response = await client.post("/api/tasks/cancel-all")
            assert response.status_code == 200
            body = response.json()
            assert body["canceled"] == 0
            assert body["task_ids"] == []
            assert "没有需要取消的任务" in body["message"]
        finally:
            await _cleanup()

    async def test_terminal_tasks_are_untouched(self, client):
        await _cleanup()
        try:
            done = await _make_task(TaskStatus.SUCCEEDED.value, title="已完成")
            failed = await _make_task(TaskStatus.FAILED.value, title="已失败")
            active = await _make_task(TaskStatus.PENDING.value, title="排队中")

            body = (await client.post("/api/tasks/cancel-all")).json()
            assert body["task_ids"] == [active]

            assert (await _status(done))[0] == TaskStatus.SUCCEEDED.value
            assert (await _status(failed))[0] == TaskStatus.FAILED.value
        finally:
            await _cleanup()

    async def test_paused_can_be_excluded(self, client):
        await _cleanup()
        try:
            paused = await _make_task(TaskStatus.PAUSED.value, title="已暂停")
            pending = await _make_task(TaskStatus.PENDING.value, title="排队中")

            body = (
                await client.post("/api/tasks/cancel-all", params={"include_paused": False})
            ).json()
            assert body["task_ids"] == [pending]
            assert body["paused_excluded"] == 1
            assert (await _status(paused))[0] == TaskStatus.PAUSED.value

            # 默认则一并取消
            body = (await client.post("/api/tasks/cancel-all")).json()
            assert body["task_ids"] == [paused]
            assert (await _status(paused))[0] == TaskStatus.CANCELED.value
        finally:
            await _cleanup()

    async def test_running_task_gets_signal_not_direct_mark(self, client):
        """执行中的任务不能直接改状态：流水线还在跑，是由它自己收尾的。"""
        await _cleanup()
        try:
            task_id = await _make_task(TaskStatus.RUNNING.value, title="执行中")

            # 造一个「正在运行」的句柄，模拟真的在执行
            handle = await task_runner.submit(task_id)
            try:
                await asyncio.sleep(0.05)
                body = (await client.post("/api/tasks/cancel-all")).json()
                assert body["running_signalled"] >= 1
                assert "已发送取消指令" in body["message"]
            finally:
                handle.cancel()
                if handle.task is not None:
                    # 流水线自己会因取消失败收尾，这里只等它结束
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(handle.task, timeout=10)
        finally:
            await _cleanup()

    async def test_single_cancel_still_works(self, client):
        """抽出 _cancel_one 后，单任务取消的行为不能回归。"""
        await _cleanup()
        try:
            task_id = await _make_task(TaskStatus.PENDING.value, title="单个任务")
            response = await client.post(f"/api/tasks/{task_id}/cancel")
            assert response.status_code == 200
            assert response.json()["message"] == "任务已取消"
            status, item_statuses = await _status(task_id)
            assert status == TaskStatus.CANCELED.value
            assert item_statuses == [TaskStatus.CANCELED.value]
        finally:
            await _cleanup()

    async def test_cancel_unknown_task_is_404(self, client):
        response = await client.post("/api/tasks/999999/cancel")
        assert response.status_code == 404

    async def test_canceled_task_finished_at_is_set(self, client):
        await _cleanup()
        try:
            task_id = await _make_task(TaskStatus.PENDING.value, title="记录结束时间")
            await client.post("/api/tasks/cancel-all")
            async with SessionLocal() as session:
                task = await session.get(Task, task_id)
            assert task is not None
            assert isinstance(task.finished_at, datetime)
        finally:
            await _cleanup()
