"""取消运行中的任务：必须真正停下来，且不能被记成「条目失败」。

回归背景（用户报的「取消无效」）：
    取消原来只置位标志 + 在阶段之间检查，并且 _run_items 会「等待在跑的条目自然结束」。
    实测一个被取消的任务仍会继续跑十几秒到几十分钟（取决于阶段内是 ffmpeg 渲染
    还是 yt-dlp 下载），前端一直显示「运行中」，用户体感就是取消无效。
    现在：取消信号 → 杀掉该任务登记的子进程 → 中断在跑的条目 → 任务落到「已取消」。
"""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import Task, TaskItem, TaskStatus
from app.pipeline.runner import task_runner
from app.utils import procs


async def _make_task(items: int = 1) -> int:
    await init_db()
    async with SessionLocal() as session:
        task = Task(
            title="取消测试",
            source_url="https://www.youtube.com/watch?v=cancel",
            source_type="video" if items == 1 else "playlist",
            status=TaskStatus.PENDING.value,
            total_items=items,
        )
        session.add(task)
        await session.flush()
        for index in range(items):
            session.add(
                TaskItem(
                    task_id=task.id,
                    idx=index,
                    video_id=f"v{index}",
                    url=task.source_url,
                    title=f"条目{index}",
                    status=TaskStatus.PENDING.value,
                )
            )
        await session.commit()
        return task.id


async def _wait_settled(task_id: int, timeout: float = 5.0) -> str:
    """等任务落到终态，返回状态。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with SessionLocal() as session:
            task = await session.get(Task, task_id)
            if task and task.status in (
                TaskStatus.CANCELED.value,
                TaskStatus.FAILED.value,
                TaskStatus.SUCCEEDED.value,
            ):
                return task.status
        await asyncio.sleep(0.05)
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        return task.status if task else "missing"


@pytest.fixture
def offline_stages(monkeypatch):
    """把所有阶段换成「可取消的慢阶段」，避免测试依赖网络与 ffmpeg。"""
    import app.pipeline.runner as runner_module

    started = asyncio.Event()

    async def slow_stage(ctx, state):
        ctx.reporter.raise_if_canceled()
        started.set()
        await asyncio.sleep(30)
        ctx.reporter.raise_if_canceled()

    monkeypatch.setattr(runner_module, "STAGES", [(name, slow_stage) for name, _ in runner_module.STAGES])
    return started


async def test_cancel_interrupts_running_task_quickly(offline_stages):
    """取消后应在毫秒级（远小于阶段本身的 30 秒）落到「已取消」。"""
    task_id = await _make_task()
    await task_runner.submit(task_id)
    await asyncio.wait_for(offline_stages.wait(), timeout=3)

    started = time.perf_counter()
    assert await task_runner.cancel(task_id) is True
    status = await _wait_settled(task_id)
    elapsed = time.perf_counter() - started

    assert status == TaskStatus.CANCELED.value, f"取消后状态为 {status}"
    assert elapsed < 2.0, f"取消后 {elapsed:.2f}s 才停下，说明仍在等待阶段自然结束"

    async with SessionLocal() as session:
        items = (
            await session.execute(select(TaskItem).where(TaskItem.task_id == task_id))
        ).scalars().all()
    # 关键：取消不能被记成「条目失败」，否则任务详情里会多出一堆红点
    assert all(item.status == TaskStatus.CANCELED.value for item in items), [
        (i.status, i.message) for i in items
    ]


async def test_cancel_sets_message_immediately(offline_stages):
    """发出取消后立刻改文案，前端能马上看到「正在取消…」而不是继续显示运行中。"""
    task_id = await _make_task()
    await task_runner.submit(task_id)
    await asyncio.wait_for(offline_stages.wait(), timeout=3)

    await task_runner.cancel(task_id)
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        assert "取消" in (task.message or "")

    await _wait_settled(task_id)


async def test_cancel_kills_registered_subprocess(monkeypatch):
    """任务里的子进程必须能被取消杀掉（ffmpeg 渲染/yt-dlp 下载都走这条路）。"""
    await init_db()
    token = "test-kill"
    procs.bind(token)
    try:
        proc_task = asyncio.create_task(procs.run_process(["sleep", "30"], timeout=60))
        # 等子进程起来
        for _ in range(50):
            if procs.running_count(token) > 0:
                break
            await asyncio.sleep(0.02)
        assert procs.running_count(token) == 1

        killed = await procs.kill_task_processes(token)
        assert killed == 1

        code, out, err = await asyncio.wait_for(proc_task, timeout=5)
        assert code != 0, "被 kill 的子进程不应返回 0"
    finally:
        procs.unbind(procs._current_token.get() and procs._current_token.set(None))  # noqa: SLF001


async def test_cancel_unknown_task_returns_false():
    await init_db()
    assert await task_runner.cancel(999_999) is False


async def test_plan_created_task_can_be_canceled(monkeypatch):
    """计划建出的任务同样能秒级取消（用户报的场景：计划跑起来的任务取消不掉）。

    计划触发 → 建任务 → 自动开始执行，这条链路上的任务与手工建的任务走同一个
    TaskRunner，因此取消链路必须一致。
    """
    from app.models import Plan
    from app.providers.base import ProbeResult, VideoInfo
    from app.services import plans as plans_service
    from app.services.settings_store import settings_store

    await init_db()
    async with SessionLocal() as session:
        await settings_store.update(
            session,
            {
                "translator": {"provider": "mock"},
                "tts": {"provider": "mock"},
                "publish": {"provider": "mock", "auto_publish": False},
                "general": {"max_concurrent_tasks": 1, "max_concurrent_items": 1},
            },
        )
        plan = Plan(
            name="取消用计划",
            source_url="https://www.youtube.com/watch?v=plan-cancel",
            enabled=True,
            auto_start=True,
            selection={"mode": "latest", "count": 1},
            schedule={"type": "interval", "minutes": 60},
            options={},
        )
        session.add(plan)
        await session.commit()
        plan_id = plan.id
        entry = VideoInfo(
            video_id="plancancel",
            url="https://www.youtube.com/watch?v=plancancel",
            webpage_url="https://www.youtube.com/watch?v=plancancel",
            title="计划条目",
            duration=12.0,
        )

    async def fake_probe(_session, _url):
        return ProbeResult(source_type="video", title="计划取消", source_id="plancancel", entries=[entry])

    monkeypatch.setattr(plans_service, "probe_source", fake_probe)

    import app.pipeline.runner as runner_module

    started = asyncio.Event()

    async def slow_stage(ctx, state):
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(runner_module, "STAGES", [(name, slow_stage) for name, _ in runner_module.STAGES])

    async with SessionLocal() as session:
        fresh = await session.get(Plan, plan_id)
        outcome = await plans_service.run_plan(session, fresh, trigger="测试")
    task_id = outcome["task_id"]
    assert task_id, outcome

    await asyncio.wait_for(started.wait(), timeout=3)
    t0 = time.perf_counter()
    assert await task_runner.cancel(task_id) is True
    status = await _wait_settled(task_id)
    elapsed = time.perf_counter() - t0

    assert status == TaskStatus.CANCELED.value, f"计划任务取消后状态为 {status}"
    assert elapsed < 2.0, f"计划任务取消用了 {elapsed:.2f}s"
