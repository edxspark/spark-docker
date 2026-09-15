"""流水线运行时上下文：把配置、提供者、进度上报、路径规则打包给各阶段。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.core.events import event_bus
from app.models import Task, TaskItem, TaskLog, TaskStatus, utcnow
from app.providers.base import VideoInfo
from app.services.subtitles import Cue

logger = logging.getLogger(__name__)


class TaskCanceled(RuntimeError):
    """任务被用户取消。"""


@dataclass
class PipelineConfig:
    general: dict[str, Any]
    translator: dict[str, Any]
    tts: dict[str, Any]
    download: dict[str, Any]
    publish: dict[str, Any]
    video: dict[str, Any]
    options: dict[str, Any] = field(default_factory=dict)

    def merged(self, section: str, **overrides: Any) -> dict[str, Any]:
        """取某分组配置，并用任务级选项覆盖（任务选项优先）。"""
        base = dict(getattr(self, section))
        for key, value in overrides.items():
            if value is not None:
                base[key] = value
        for key in list(base):
            if key in self.options and self.options[key] is not None:
                base[key] = self.options[key]
        return base


class Reporter:
    """把进度/日志同时写入数据库与事件总线。"""

    def __init__(
        self,
        task_id: int,
        session_factory: async_sessionmaker,
        cancel_event: asyncio.Event,
    ) -> None:
        self.task_id = task_id
        self.session_factory = session_factory
        self.cancel_event = cancel_event

    def raise_if_canceled(self) -> None:
        if self.cancel_event.is_set():
            raise TaskCanceled("任务已取消")

    async def task_update(self, **fields: Any) -> None:
        # None 表示「本次不改动该字段」，只有时间戳类字段允许显式置空
        nullable = {"started_at", "finished_at"}
        async with self.session_factory() as session:
            task = await session.get(Task, self.task_id)
            if task is None:
                return
            for key, value in fields.items():
                if value is None and key not in nullable:
                    continue
                setattr(task, key, value)
            task.updated_at = utcnow()
            await session.commit()
            payload = {
                "id": task.id,
                "status": task.status,
                "stage": task.stage,
                "progress": task.progress,
                "message": task.message,
                "total_items": task.total_items,
                "done_items": task.done_items,
                "failed_items": task.failed_items,
            }
        await event_bus.emit(self.task_id, "task.updated", **payload)

    async def item_update(self, item_id: int, **fields: Any) -> None:
        async with self.session_factory() as session:
            item = await session.get(TaskItem, item_id)
            if item is None:
                return
            for key, value in fields.items():
                setattr(item, key, value)
            item.updated_at = utcnow()
            await session.commit()
            payload = {
                "id": item.id,
                "task_id": item.task_id,
                "status": item.status,
                "stage": item.stage,
                "progress": item.progress,
                "message": item.message,
                "title_zh": item.title_zh,
                "publish_status": item.publish_status,
                "publish_url": item.publish_url,
                "output_path": item.output_path,
            }
        await event_bus.emit(self.task_id, "item.updated", **payload)

    async def log(
        self,
        message: str,
        *,
        level: str = "info",
        stage: str = "",
        item_id: int | None = None,
    ) -> None:
        async with self.session_factory() as session:
            session.add(
                TaskLog(
                    task_id=self.task_id,
                    item_id=item_id,
                    level=level,
                    stage=stage,
                    message=message[:4000],
                )
            )
            await session.commit()
        level_fn = {"error": logger.error, "warning": logger.warning}.get(level, logger.info)
        level_fn("[task=%s] %s", self.task_id, message)
        await event_bus.emit(
            self.task_id,
            "log",
            level=level,
            stage=stage,
            item_id=item_id,
            message=message,
        )

    async def item_stage(
        self,
        item_id: int,
        stage: str,
        progress: float,
        message: str = "",
        *,
        overall: float | None = None,
    ) -> None:
        """上报某条目某阶段进度；overall 为条目整体进度（0-100）。"""
        await self.item_update(
            item_id,
            stage=stage,
            progress=round(progress, 2),
            message=message[:400],
            status=TaskStatus.RUNNING.value,
        )
        if overall is not None:
            await event_bus.emit(
                self.task_id,
                "stage",
                item_id=item_id,
                stage=stage,
                progress=round(progress, 2),
                overall=round(overall, 2),
                message=message,
            )


@dataclass
class ItemPaths:
    """单个条目的产物路径。目录按 data/<类别>/<task_id>/ 归档，便于整任务清理。"""

    root: Path
    video_dir: Path
    video: Path
    subtitle_en: Path
    subtitle_zh: Path
    audio_dir: Path
    dub_audio: Path
    output: Path
    cover: Path
    work_dir: Path


def build_item_paths(task_id: int, item: TaskItem) -> ItemPaths:
    key = item.video_id or f"item{item.id}"
    video_dir = settings.downloads_dir / str(task_id) / key
    audio_dir = settings.audio_dir / str(task_id) / key
    return ItemPaths(
        root=settings.data_dir,
        video_dir=video_dir,
        video=video_dir / f"{key}.mp4",
        subtitle_en=settings.subtitles_dir / str(task_id) / f"{key}.en.srt",
        subtitle_zh=settings.subtitles_dir / str(task_id) / f"{key}.zh.srt",
        audio_dir=audio_dir,
        dub_audio=audio_dir / "dub.mp3",
        output=settings.outputs_dir / str(task_id) / f"{key}.mp4",
        cover=settings.covers_dir / str(task_id) / f"{key}.jpg",
        work_dir=audio_dir / "work",
    )


@dataclass
class ItemState:
    """阶段之间传递的中间状态。"""

    item: TaskItem
    paths: ItemPaths
    info: VideoInfo = field(default_factory=VideoInfo)
    cues_en: list[Cue] = field(default_factory=list)
    cues_zh: list[Cue] = field(default_factory=list)
    voice_segments: list[tuple[float, float, Path]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    subtitle_kind: str = ""


@dataclass
class StageContext:
    """阶段执行上下文。"""

    task: Task
    config: PipelineConfig
    providers: dict[str, Any]
    reporter: Reporter
    account_file: Path
    stage_weights: dict[str, float] = field(
        default_factory=lambda: {
            "probe": 2.0,
            "download": 30.0,
            "subtitle": 4.0,
            "translate": 16.0,
            "tts": 26.0,
            "align": 12.0,
            "metadata": 3.0,
            "publish": 7.0,
        }
    )

    def relative(self, path: Path) -> str:
        """转成相对 data 目录的路径，便于迁移与前端展示。"""
        try:
            return str(Path(path).resolve().relative_to(settings.data_dir.resolve()))
        except ValueError:
            return str(path)

    def overall_for(self, stage: str, progress: float) -> float:
        """把阶段内进度映射为条目整体进度（0-100）。"""
        weights = self.stage_weights
        total = sum(weights.values()) or 1.0
        done = 0.0
        for name, weight in weights.items():
            if name == stage:
                break
            done += weight
        else:
            return 100.0
        return min(100.0, (done + weights.get(stage, 0.0) * (progress / 100.0)) / total * 100.0)


ProgressFn = Callable[[float, str], Any]

def make_threadsafe_progress(
    loop: asyncio.AbstractEventLoop,
) -> Callable[[Awaitable[None]], None]:
    """返回一个「把协程安全地排回事件循环」的调度函数。

    背景（真实事故）：yt-dlp 的 progress_hook 在工作线程中同步执行。若在该线程里
    调用 asyncio.get_event_loop()，Python 3.10+ 会抛
    RuntimeError("There is no current event loop in thread ...")，
    而 hook 抛出的异常会让 yt-dlp 直接中止整个下载。

    因此必须在协程中先取到 loop，再通过 call_soon_threadsafe 排回事件循环；
    且上报本身的任何失败都只能被吞掉——绝不能让「显示进度」弄坏「下载」。
    """

    def schedule(coro: Awaitable[None]) -> None:
        try:
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(coro))
        except RuntimeError:
            # 事件循环已关闭（任务取消 / 服务停机）：关闭协程避免 un-awaited 警告
            close = getattr(coro, "close", None)
            if callable(close):
                close()

    return schedule
