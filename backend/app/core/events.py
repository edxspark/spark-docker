"""进度事件总线：流水线 → WebSocket 推送。

每个任务维护一组订阅者队列，并保留最近 N 条事件用于「后订阅者补播」，
这样详情页刷新后仍能看到当前进度与最近日志。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

MAX_REPLAY = 200


@dataclass
class Event:
    type: str                      # task.updated / item.updated / log / done
    task_id: int
    payload: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[int, set[asyncio.Queue[Event]]] = {}
        self._history: dict[int, deque[Event]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, task_id: int) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=1000)
        async with self._lock:
            self._subscribers.setdefault(task_id, set()).add(queue)
            for event in self._history.get(task_id, ()):
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(event)
        return queue

    async def unsubscribe(self, task_id: int, queue: asyncio.Queue[Event]) -> None:
        async with self._lock:
            subs = self._subscribers.get(task_id)
            if subs:
                subs.discard(queue)
                if not subs:
                    self._subscribers.pop(task_id, None)

    async def publish(self, event: Event) -> None:
        self._history.setdefault(event.task_id, deque(maxlen=MAX_REPLAY)).append(event)
        for queue in list(self._subscribers.get(event.task_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # 消费端过慢：丢弃最旧的一条，保证进度最新
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(event)

    async def emit(self, task_id: int, type_: str, /, **payload: Any) -> None:
        """便捷发送。前两个参数声明为位置参数，避免 payload 中出现同名键（如 task_id）时冲突。"""
        await self.publish(Event(type=type_, task_id=task_id, payload=payload))

    def clear(self, task_id: int) -> None:
        self._history.pop(task_id, None)


event_bus = EventBus()
