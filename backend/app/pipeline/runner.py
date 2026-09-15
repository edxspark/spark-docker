"""任务执行器：调度条目、串联阶段、落地进度、支持取消与断点续跑。"""

from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import Awaitable, Callable
from pathlib import Path

from sqlalchemy import select

from app.core.config import settings
from app.core.events import event_bus
from app.db import SessionLocal
from app.models import Task, TaskItem, TaskStatus, utcnow
from app.pipeline.context import (
    ItemState,
    PipelineConfig,
    Reporter,
    StageContext,
    TaskCanceled,
    build_item_paths,
)
from app.pipeline.stages.acquire import stage_download, stage_probe, stage_subtitle
from app.pipeline.stages.deliver import stage_metadata, stage_publish
from app.pipeline.stages.localize import (
    render_config_key,
    stage_align,
    stage_translate,
    stage_tts,
    translate_config_key,
    tts_config_key,
)
from app.providers import build_all
from app.providers.base import ProviderError
from app.services.settings_store import settings_store
from app.services.subtitles import normalize_cues, parse_subtitle_file, reindex

logger = logging.getLogger(__name__)

StageFn = Callable[[StageContext, ItemState], Awaitable[None]]

STAGES: list[tuple[str, StageFn]] = [
    ("probe", stage_probe),
    ("download", stage_download),
    ("subtitle", stage_subtitle),
    ("translate", stage_translate),
    ("tts", stage_tts),
    ("align", stage_align),
    ("metadata", stage_metadata),
    ("publish", stage_publish),
]


def _render_inputs(state: ItemState) -> list[Path]:
    """渲染成片所依赖的输入文件（任一比成片新，成片就算过期）。"""
    paths = state.paths
    candidates = [paths.video, paths.subtitle_zh, paths.subtitle_en]
    candidates += [
        paths.audio_dir / "voice_track.mp3",
        paths.audio_dir / "mixed.m4a",
    ]
    if state.item.dubbed_audio_path:
        candidates.append(settings.data_dir / state.item.dubbed_audio_path)
    return candidates


class TaskHandle:
    """运行中的任务句柄。"""

    def __init__(self, task_id: int) -> None:
        self.task_id = task_id
        self.cancel_event = asyncio.Event()
        self.task: asyncio.Task | None = None

    def cancel(self) -> None:
        self.cancel_event.set()

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()


class TaskRunner:
    """进程内任务调度器。单进程部署足够；多进程需换成外部队列。"""

    def __init__(self) -> None:
        self._handles: dict[int, TaskHandle] = {}
        self._slot: asyncio.Semaphore | None = None
        self._slot_size = 0

    def _get_slot(self, size: int) -> asyncio.Semaphore:
        if self._slot is None or self._slot_size != size:
            self._slot = asyncio.Semaphore(max(1, size))
            self._slot_size = size
        return self._slot

    def is_running(self, task_id: int) -> bool:
        handle = self._handles.get(task_id)
        return bool(handle and handle.running)

    def running_task_ids(self) -> list[int]:
        return [tid for tid, handle in self._handles.items() if handle.running]

    async def cancel(self, task_id: int) -> bool:
        handle = self._handles.get(task_id)
        if not handle or not handle.running:
            return False
        handle.cancel()
        return True

    async def submit(self, task_id: int) -> TaskHandle:
        """提交任务；已在运行则返回原句柄。"""
        existing = self._handles.get(task_id)
        if existing and existing.running:
            return existing
        handle = TaskHandle(task_id)
        self._handles[task_id] = handle
        handle.task = asyncio.create_task(self._run_guarded(handle), name=f"task-{task_id}")
        return handle

    async def _run_guarded(self, handle: TaskHandle) -> None:
        try:
            await self._run(handle)
        except TaskCanceled:
            await self._finalize(handle.task_id, TaskStatus.CANCELED.value, "任务已取消")
        except Exception as exc:  # noqa: BLE001 - 顶层兜底，任何异常都要落到任务状态
            logger.exception("任务 %s 执行失败", handle.task_id)
            await self._finalize(
                handle.task_id,
                TaskStatus.FAILED.value,
                f"任务异常终止：{exc}",
                error=traceback.format_exc()[-4000:],
            )

    # -- 主流程 ----------------------------------------------------------------

    async def _run(self, handle: TaskHandle) -> None:
        task_id = handle.task_id
        async with SessionLocal() as session:
            config = await settings_store.load_all(session)
            task = await session.get(Task, task_id)
            if task is None:
                return
            items = (
                await session.execute(select(TaskItem).where(TaskItem.task_id == task_id).order_by(TaskItem.idx))
            ).scalars().all()

        general = config["general"]
        pipeline_config = PipelineConfig(
            general=general,
            translator=config["translator"],
            tts=config["tts"],
            asr=config.get("asr", {}),
            download=config["download"],
            publish=config["publish"],
            video=config["video"],
            options=dict(task.options or {}),
        )
        reporter = Reporter(task_id, SessionLocal, handle.cancel_event)
        account_file = settings.auth_dir / "douyin_default.json"

        try:
            providers = build_all(config, account_file)
        except Exception as exc:  # noqa: BLE001
            await reporter.log(f"提供者初始化失败：{exc}", level="error")
            raise

        ctx = StageContext(
            task=task,
            config=pipeline_config,
            providers=providers,
            reporter=reporter,
            account_file=account_file,
        )

        await reporter.task_update(
            status=TaskStatus.RUNNING.value,
            started_at=utcnow(),
            message="任务开始执行",
            progress=0.0,
        )
        await reporter.log(f"任务开始执行，共 {len(items)} 个视频")

        slot = self._get_slot(int(general.get("max_concurrent_tasks", 1)))
        async with slot:
            await self._run_items(ctx, items, handle)

        await self._finalize_from_items(task_id, reporter)

    async def _run_items(self, ctx: StageContext, items: list[TaskItem], handle: TaskHandle) -> None:
        item_concurrency = max(1, settings.max_concurrent_items)
        semaphore = asyncio.Semaphore(item_concurrency)
        progress_lock = asyncio.Lock()
        item_progress: dict[int, float] = {item.id: 0.0 for item in items}

        async def bump(item_id: int, percent: float) -> None:
            async with progress_lock:
                item_progress[item_id] = percent
                overall = sum(item_progress.values()) / max(1, len(item_progress))
            await ctx.reporter.task_update(progress=round(overall, 2))

        async def run_one(item: TaskItem) -> None:
            async with semaphore:
                if handle.cancel_event.is_set():
                    await ctx.reporter.item_update(
                        item.id, status=TaskStatus.CANCELED.value, message="已取消"
                    )
                    return
                await self._run_item(ctx, item, bump)

        await asyncio.gather(*(run_one(item) for item in items), return_exceptions=False)

    async def _run_item(self, ctx: StageContext, item: TaskItem, bump) -> None:
        state = await self._build_state(ctx, item)
        item_id = item.id
        failed_stage = ""

        await ctx.reporter.item_update(item_id, status=TaskStatus.RUNNING.value, error="")
        try:
            for stage_name, stage_fn in STAGES:
                ctx.reporter.raise_if_canceled()
                if self._already_done(stage_name, state, item, ctx):
                    await bump(item_id, ctx.overall_for(stage_name, 100))
                    continue
                failed_stage = stage_name
                await self._run_stage(ctx, stage_name, stage_fn, state)
                await bump(item_id, ctx.overall_for(stage_name, 100))

            await ctx.reporter.item_update(
                item_id, status=TaskStatus.SUCCEEDED.value, progress=100.0, message="完成", error=""
            )
            await bump(item_id, 100.0)
            await ctx.reporter.log(f"视频处理完成：{state.item.title_zh or state.item.title}", item_id=item_id)

        except TaskCanceled:
            await ctx.reporter.item_update(item_id, status=TaskStatus.CANCELED.value, message="已取消")
            raise
        except Exception as exc:  # noqa: BLE001 - 单条失败不影响同任务其他条目
            message = str(exc)
            logger.exception("条目 %s 处理失败", item_id)
            await ctx.reporter.item_update(
                item_id,
                status=TaskStatus.FAILED.value,
                error=message[-4000:],
                message=f"失败于「{failed_stage}」",
            )
            await ctx.reporter.log(
                f"处理失败（阶段：{failed_stage}）：{message}", level="error", stage=failed_stage, item_id=item_id
            )
            await bump(item_id, 100.0)

    async def _run_stage(
        self,
        ctx: StageContext,
        stage_name: str,
        stage_fn: StageFn,
        state: ItemState,
    ) -> None:
        label = dict({"probe": "解析链接", "download": "下载视频与字幕", "subtitle": "字幕清洗与断句",
                      "translate": "翻译字幕", "tts": "语音合成", "align": "时间轴对齐与合成",
                      "metadata": "生成标题与话题", "publish": "发布到抖音"}).get(stage_name, stage_name)
        await ctx.reporter.task_update(stage=stage_name, message=f"[{state.item.title[:40]}] {label}")
        await ctx.reporter.log(f"进入阶段：{label}", stage=stage_name, item_id=state.item.id)
        try:
            await stage_fn(ctx, state)
        except ProviderError as exc:
            raise ProviderError(f"{label}：{exc}") from exc

    def _already_done(self, stage_name: str, state: ItemState, item: TaskItem, ctx: StageContext) -> bool:
        """断点续跑：已有产物则跳过已完成的阶段，避免重复下载/重复计费。"""
        paths = state.paths
        if stage_name == "probe":
            return bool(item.title and item.duration)
        if stage_name == "download":
            return paths.video.exists() and (bool(state.cues_en) or bool(state.stats.get("no_subtitle")))
        if stage_name == "subtitle":
            # 注意：不能用 cues_en 判断，因为 download 阶段也会填充原始字幕。
            # 完成标志是 stats 中记录的句子数。
            if state.stats.get("sentences"):
                return True
            if state.stats.get("no_subtitle"):
                # 只有语音识别「已完成或已确定性失败」才算这个阶段做完了。
                # 中断导致的半途而废必须重试，否则任务永远拿不到中文配音。
                return bool(state.stats.get("asr_done"))
            return False
        if stage_name == "translate":
            if not (paths.subtitle_zh.exists() and state.cues_zh):
                return False
            # 换了翻译服务/模型后，旧译文（可能是 mock 占位）必须重做
            return (state.stats.get("translate") or {}).get("config_key") == translate_config_key(ctx)
        if stage_name == "tts":
            needed = len(state.cues_zh)
            if needed == 0:
                return True
            # 换了服务商/音色/语速后，旧分段音频必须重做（否则会把占位音频当真配音）
            if (state.stats.get("tts") or {}).get("config_key") != tts_config_key(ctx):
                return False
            existing = sum(
                1
                for i in range(needed)
                if (paths.audio_dir / f"seg_{i:04d}.mp3").exists()
            )
            return existing == needed
        if stage_name == "align":
            if not (paths.output.exists() and paths.output.stat().st_size > 0):
                return False
            # 渲染参数变了要重渲染
            if (state.stats.get("output") or {}).get("config_key") != render_config_key(ctx):
                return False
            # 关键：成片必须比它的所有输入都新。
            # 否则「先跑过一次产出成片、之后才生成新字幕/新配音」的情况下，
            # 会一直沿用旧成片——用户看到的就是「没有中文字幕也没有中文配音」。
            try:
                output_mtime = paths.output.stat().st_mtime
            except OSError:
                return False
            for candidate in _render_inputs(state):
                try:
                    if candidate.exists() and candidate.stat().st_mtime > output_mtime:
                        return False
                except OSError:
                    return False
            return True
        if stage_name == "publish":
            return bool(item.publish_status) and item.publish_status not in {"", "publishing", "failed"}
        return False

    async def _build_state(self, ctx: StageContext, item: TaskItem) -> ItemState:
        """从数据库与磁盘恢复条目状态，支撑断点续跑。"""
        paths = build_item_paths(item.task_id, item)
        state = ItemState(item=item, paths=paths)
        state.stats = dict(item.stats or {})
        state.subtitle_kind = state.stats.get("subtitle_kind", "")

        from app.providers.base import VideoInfo

        state.info = VideoInfo(
            video_id=item.video_id,
            url=item.url,
            title=item.title,
            description=item.description,
            author=item.author,
            duration=item.duration,
            thumbnail=item.thumbnail,
            webpage_url=item.url,
        )

        # 恢复下载后的真实媒体文件路径（扩展名可能是 mkv/webm）
        if item.video_path:
            stored = settings.data_dir / item.video_path
            if stored.exists():
                paths.video = stored
            else:
                candidates = sorted(
                    (
                        p
                        for p in paths.video_dir.glob(f"{item.video_id}.*")
                        if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}
                    ),
                    key=lambda p: p.stat().st_size,
                    reverse=True,
                )
                if candidates:
                    paths.video = candidates[0]

        if item.subtitle_source_path:
            stored = settings.data_dir / item.subtitle_source_path
            if stored.exists():
                try:
                    state.cues_en = reindex(normalize_cues(parse_subtitle_file(stored)))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("恢复英文字幕失败：%s", exc)

        if item.subtitle_zh_path:
            stored = settings.data_dir / item.subtitle_zh_path
            if stored.exists():
                try:
                    state.cues_zh = reindex(normalize_cues(parse_subtitle_file(stored)))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("恢复中文字幕失败：%s", exc)

        return state

    # -- 收尾 ------------------------------------------------------------------

    async def _finalize_from_items(self, task_id: int, reporter: Reporter) -> None:
        async with SessionLocal() as session:
            items = (
                await session.execute(select(TaskItem).where(TaskItem.task_id == task_id))
            ).scalars().all()
        total = len(items)
        done = sum(1 for i in items if i.status == TaskStatus.SUCCEEDED.value)
        failed = sum(1 for i in items if i.status == TaskStatus.FAILED.value)
        canceled = sum(1 for i in items if i.status == TaskStatus.CANCELED.value)

        if canceled and done + failed < total:
            status, message = TaskStatus.CANCELED.value, "任务已取消"
        elif failed == 0 and done == total:
            status, message = TaskStatus.SUCCEEDED.value, "全部视频处理完成"
        elif done > 0:
            status, message = TaskStatus.PARTIAL.value, f"部分成功：成功 {done}，失败 {failed}"
        else:
            status, message = TaskStatus.FAILED.value, f"全部失败（{failed} 个）"

        await reporter.task_update(
            status=status,
            progress=100.0 if done + failed == total else None,
            message=message,
            total_items=total,
            done_items=done,
            failed_items=failed,
            finished_at=utcnow(),
        )
        await reporter.log(f"任务结束：{message}")
        await event_bus.emit(task_id, "task.finished", status=status, message=message)

    async def _finalize(self, task_id: int, status: str, message: str, *, error: str = "") -> None:
        async with SessionLocal() as session:
            task = await session.get(Task, task_id)
            if task is None:
                return
            task.status = status
            task.message = message
            task.finished_at = utcnow()
            if error:
                task.error = error
            await session.commit()
        await event_bus.emit(task_id, "task.finished", status=status, message=message)


task_runner = TaskRunner()


async def recover_interrupted_tasks() -> int:
    """进程重启时把「运行中」的任务标记为中断，避免状态永远卡住。"""
    async with SessionLocal() as session:
        rows = (
            await session.execute(select(Task).where(Task.status == TaskStatus.RUNNING.value))
        ).scalars().all()
        for task in rows:
            task.status = TaskStatus.FAILED.value
            task.message = "服务重启导致任务中断，可重新执行"
            task.finished_at = utcnow()
        interrupted_items = (
            await session.execute(select(TaskItem).where(TaskItem.status == TaskStatus.RUNNING.value))
        ).scalars().all()
        for item in interrupted_items:
            item.status = TaskStatus.FAILED.value
            item.message = "服务重启导致中断，可重新执行（已完成的阶段会自动跳过）"
        if rows:
            await session.commit()
    return len(rows)
