"""搬运计划的定时调度器（进程内后台任务）。

设计取舍
--------
- **进程内 asyncio 循环**：本项目是单进程部署（`task_runner` 也是进程内），
  引入 APScheduler 或外部队列只会多一份状态。循环每 20 秒扫一次库，
  对「按分钟级排期」完全够用。
- **先认领再执行**：触发前先把 `next_run_at` 推到下一个周期并 commit，
  这样即使执行过程中进程被杀，也不会在重启后对同一个时间点重复触发。
- **串行执行**：同一时刻只跑一个计划的触发动作，避免一次性建出几十个任务；
  任务本身的并发仍由 `task_runner` 的槽位控制。
- **多进程保护**：认领使用「条件更新」（UPDATE ... WHERE next_run_at = 旧值），
  affected rows 为 0 说明别的进程已经抢到，直接跳过。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update

from app.db import SessionLocal
from app.models import Plan
from app.services import plans as plans_service
from app.services import schedule as schedule_service

logger = logging.getLogger(__name__)

# 扫描间隔：够快（分钟级排期最多晚 20 秒触发），也不会给数据库添负担
TICK_SECONDS = 20

class PlanScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False
        self._busy = False
        self._last_tick: datetime | None = None

    # -- 生命周期 -----------------------------------------------------------

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="plan-scheduler")
        logger.info("搬运计划调度器已启动（每 %s 秒检查一次）", TICK_SECONDS)

    async def stop(self) -> None:
        self._running = False
        task = self._task
        self._task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done())

    def status(self) -> dict:
        return {
            "running": self.running,
            "busy": self._busy,
            "tick_seconds": TICK_SECONDS,
            "last_tick": self._last_tick.isoformat() if self._last_tick else None,
        }

    # -- 主循环 -------------------------------------------------------------

    async def _loop(self) -> None:
        # 启动后先等一个 tick：避免与「服务启动时的任务恢复」抢数据库
        await asyncio.sleep(2)
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 调度器不能因为单次异常退出
                logger.exception("计划调度器本轮执行出错")
            await asyncio.sleep(TICK_SECONDS)

    async def tick(self, *, now_utc: datetime | None = None) -> dict:
        """扫描一次到点的计划并执行。返回本轮摘要（便于测试与接口展示）。"""
        self._last_tick = datetime.now(timezone.utc)
        if self._busy:
            return {"skipped": "busy", "executed": []}

        current = now_utc or datetime.now(timezone.utc)
        async with SessionLocal() as session:
            stmt = (
                select(Plan)
                .where(Plan.enabled.is_(True), Plan.next_run_at.is_not(None), Plan.next_run_at <= current.replace(tzinfo=None))
                .order_by(Plan.next_run_at)
            )
            due = list((await session.execute(stmt)).scalars().all())

        executed: list[dict] = []
        for plan in due:
            claimed = await self._claim(plan.id, plan.next_run_at, current)
            if not claimed:
                # 别的进程/上一轮已经认领
                continue
            executed.append(await self._run_one(plan.id))
        return {"scanned": len(due), "executed": executed}

    async def _claim(self, plan_id: int, previous: datetime | None, now_utc: datetime) -> bool:
        """把 next_run_at 推到下一个周期，抢到才返回 True。"""
        async with SessionLocal() as session:
            plan = await session.get(Plan, plan_id)
            if plan is None or plan.next_run_at is None or plan.next_run_at != previous:
                return False

            spec = dict(plan.schedule or {})
            if str(spec.get("type")) == "once":
                next_run = None
            else:
                # 用「刚触发的时间点」往后推，避免长任务把周期越推越晚
                next_run = schedule_service.next_run_at(spec, now_utc=previous.replace(tzinfo=timezone.utc))
                if next_run is None:
                    next_run = schedule_service.next_run_at(spec, now_utc=now_utc)

            result = await session.execute(
                update(Plan)
                .where(Plan.id == plan_id, Plan.next_run_at == previous)
                .values(next_run_at=next_run, status="running", last_message="已触发，正在准备任务…")
            )
            await session.commit()
            return bool(result.rowcount)

    async def _run_one(self, plan_id: int) -> dict:
        self._busy = True
        try:
            async with SessionLocal() as session:
                plan = await session.get(Plan, plan_id)
                if plan is None:
                    return {"plan_id": plan_id, "ok": False, "message": "计划不存在"}
                try:
                    outcome = await plans_service.run_plan(session, plan, trigger="定时")
                except Exception as exc:  # noqa: BLE001 - 单个计划失败不影响其他计划
                    logger.exception("计划 #%s 执行失败", plan_id)
                    await session.rollback()
                    plan = await session.get(Plan, plan_id)
                    if plan is not None:
                        plan.status = "error"
                        plan.last_error = str(exc)[:4000]
                        plan.last_message = f"执行失败：{str(exc)[:200]}"
                        plan.last_run_at = datetime.now(timezone.utc)
                        plan.run_count += 1
                        spec = dict(plan.schedule or {})
                        if str(spec.get("type")) == "once":
                            plan.enabled = False
                        await session.commit()
                    return {"plan_id": plan_id, "ok": False, "message": str(exc)[:300]}
                return {"plan_id": plan_id, **outcome}
        finally:
            self._busy = False


plan_scheduler = PlanScheduler()
