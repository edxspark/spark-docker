"""发布成功后条目状态必须同步纠正。

事故：条目此前发布失败被标记为 failed（message='失败于「publish」'），
之后人工重新发布成功，只更新了 publish_status，条目状态仍是 failed。
列表「状态」列读的是 item.status，于是用户看到「发布成功但状态显示失败」。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import Task, TaskItem, TaskStatus


@pytest.fixture(autouse=True)
async def _db(tmp_root):
    await init_db()


async def test_successful_republish_clears_failed_status():
    async with SessionLocal() as session:
        task = Task(
            title="t",
            source_url="https://www.youtube.com/watch?v=x",
            status=TaskStatus.FAILED.value,
            total_items=1,
            done_items=0,
            failed_items=1,
            message="全部失败（1 个）",
        )
        session.add(task)
        await session.flush()
        item = TaskItem(
            task_id=task.id,
            idx=0,
            title="v",
            status=TaskStatus.FAILED.value,
            message="失败于「publish」",
            error="boom",
            publish_status="failed",
        )
        session.add(item)
        await session.commit()
        task_id, item_id = task.id, item.id

    # 等价于手动发布成功后的写库动作
    from app.api.tasks import _refresh_task_status

    async with SessionLocal() as session:
        item = await session.get(TaskItem, item_id)
        item.status = TaskStatus.SUCCEEDED.value
        item.message = "已发布"
        item.error = ""
        item.publish_status = "published"
        await session.flush()
        await _refresh_task_status(session, task_id)
        await session.commit()

    async with SessionLocal() as session:
        got_task = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one()
        got_item = (await session.execute(select(TaskItem).where(TaskItem.id == item_id))).scalar_one()

        assert got_item.status == "succeeded"
        assert got_item.message == "已发布"
        assert got_item.error == ""
        # 任务不能继续停留在「全部失败」
        assert got_task.status == "succeeded"
        assert got_task.failed_items == 0
        assert got_task.done_items == 1


async def test_refresh_keeps_failed_when_still_all_failed():
    """仍然全部失败时不能把任务刷成成功——不掩盖真实问题。"""
    from app.api.tasks import _refresh_task_status

    async with SessionLocal() as session:
        task = Task(
            title="t2",
            source_url="https://www.youtube.com/watch?v=y",
            status=TaskStatus.FAILED.value,
            total_items=1,
            failed_items=1,
            message="全部失败（1 个）",
        )
        session.add(task)
        await session.flush()
        session.add(
            TaskItem(task_id=task.id, idx=0, title="v2", status=TaskStatus.FAILED.value)
        )
        await session.commit()
        task_id = task.id

    async with SessionLocal() as session:
        await _refresh_task_status(session, task_id)
        await session.commit()

    async with SessionLocal() as session:
        got = (await session.execute(select(Task).where(Task.id == task_id))).scalar_one()
        assert got.status == "failed"
        assert got.failed_items == 1
