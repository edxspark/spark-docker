"""任务执行器：调度条目、串联阶段、落地进度、支持取消与断点续跑。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import replace
import time
import traceback
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.config import settings
from app.core.events import event_bus
from app.db import SessionLocal
from app.models import STAGE_LABELS, Task, TaskItem, TaskStatus, utcnow
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
from app.pipeline.stages import localize as localize_service
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
from app.services import intro as intro_service
from app.services.settings_store import merge_tts_config, settings_store
from app.services.subtitles import normalize_cues, parse_subtitle_file, reindex
from app.utils import procs

logger = logging.getLogger(__name__)

StageFn = Callable[[StageContext, ItemState], Awaitable[None]]

# 超过这个秒数就算「偏慢」，日志里标出来——这样扫一眼日志就知道该优化哪一步。
_SLOW_STAGE_SECONDS = 120.0


def _human_duration(seconds: float) -> str:
    """把秒数写成日志里好读的形式：12.4 秒 / 5 分 11 秒 / 1 小时 3 分。"""
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    if seconds < 3600:
        minutes, rest = divmod(int(round(seconds)), 60)
        return f"{minutes} 分 {rest} 秒"
    hours, rest = divmod(int(round(seconds)), 3600)
    return f"{hours} 小时 {rest // 60} 分"


def summarize_timings(timings: dict[str, Any]) -> str:
    """把各阶段耗时汇总成一行，按耗时从大到小排——优化的优先级一眼可见。"""
    rows: list[tuple[str, float]] = []
    for stage_name, value in (timings or {}).items():
        seconds = float((value or {}).get("seconds") or 0)
        if seconds <= 0:
            continue
        rows.append((stage_name, seconds))
    if not rows:
        return ""
    rows.sort(key=lambda row: row[1], reverse=True)
    parts = [f"{STAGE_LABELS.get(name, name)} {_human_duration(sec)}" for name, sec in rows]
    return "，".join(parts)


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
        # 取消时要立刻给用户反馈，因此句柄持有 reporter
        self.reporter: Reporter | None = None

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
        """取消任务：置位取消标志，并立刻杀掉该任务已启动的子进程。

        只置位是不够的——阶段内部的 ffmpeg 渲染/下载可能还要跑几分钟，
        用户看到的现象就是「取消了但还在跑」。杀掉子进程后，阶段会立刻
        以 CancelledError 退出，任务状态随即落到已取消。
        """
        handle = self._handles.get(task_id)
        if not handle or not handle.running:
            return False
        handle.cancel()
        try:
            killed = await procs.kill_task_processes(str(task_id))
        except Exception:  # noqa: BLE001 - 清理失败不应影响取消本身
            logger.exception("终止任务 %s 的子进程失败", task_id)
            killed = 0
        with contextlib.suppress(Exception):
            await handle.reporter.task_update(message="正在取消…")
        logger.info("任务 %s 已置取消标志（终止子进程 %s 个）", task_id, killed)
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
            # 公共项 + 当前通道专属参数（库里分三组存，流水线只认这一份）
            tts=merge_tts_config(config),
            asr=config.get("asr", {}),
            download=config["download"],
            publish=config["publish"],
            video=config["video"],
            intro=config.get("intro", {}),
            options=dict(task.options or {}),
        )
        reporter = Reporter(task_id, SessionLocal, handle.cancel_event)
        handle.reporter = reporter
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
        # 把该任务的子进程登记到 task_id 名下：取消时按任务杀进程
        bind_token = procs.bind(str(task_id))
        try:
            async with slot:
                await self._run_items(ctx, items, handle)
            if handle.cancel_event.is_set():
                raise TaskCanceled("任务已取消")
        finally:
            procs.unbind(bind_token)
            await procs.kill_task_processes(str(task_id), reason="任务收尾清理")

        await self._finalize_from_items(task_id, reporter)

    async def _run_items(self, ctx: StageContext, items: list[TaskItem], handle: TaskHandle) -> None:
        # 条目并发：系统配置优先（用户可调），未配置时用进程默认值
        configured = ctx.config.general.get("max_concurrent_items") if ctx.config else None
        item_concurrency = max(1, int(configured or settings.max_concurrent_items))
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

        workers = [asyncio.create_task(run_one(item), name=f"item-{item.id}") for item in items]

        async def watch_cancel() -> None:
            """取消时立刻中断在跑的条目。

            之前是「等条目自然结束」：如果正卡在一个 30 分钟的 ffmpeg 渲染上，
            用户点了取消却要等半小时——这正是「取消无效」的主因。
            现在改为真正 cancel 掉 worker：阶段内 await 的位置会立刻抛
            CancelledError，登记的子进程也已由 TaskRunner.cancel 杀掉。
            """
            await handle.cancel_event.wait()
            for worker in workers:
                if not worker.done():
                    worker.cancel()

        watcher = asyncio.create_task(watch_cancel(), name="cancel-watcher")
        try:
            results = await asyncio.gather(*workers, return_exceptions=True)
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watcher

        # 取消时不再向外抛异常：交由 _run 判定后统一走「已取消」收尾，
        # 避免聚合出的异常变成未处理异常（进程退出时打印一堆噪音）。
        if handle.cancel_event.is_set():
            return
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, TaskCanceled):
                raise result

    async def _run_item(self, ctx: StageContext, item: TaskItem, bump) -> None:
        state = await self._build_state(ctx, item)
        item_id = item.id
        failed_stage = ""

        await ctx.reporter.item_update(item_id, status=TaskStatus.RUNNING.value, error="")
        item_started = time.perf_counter()
        try:
            for stage_name, stage_fn in STAGES:
                ctx.reporter.raise_if_canceled()
                if self._already_done(stage_name, state, item, ctx):
                    # 断点续跑跳过的阶段没有真实耗时。若直接记 0，
                    # 会把它算进平均值里，把真正该优化的阶段稀释掉。
                    self._mark_skipped(state, stage_name)
                    await bump(item_id, ctx.overall_for(stage_name, 100))
                    continue
                failed_stage = stage_name
                await self._run_stage(ctx, stage_name, stage_fn, state)
                # 显式记录阶段已完成。
                # 有些阶段（例如 probe）没有任何外部产物，重跑时有 store=None 的
                # provider，一旦被重新执行就会直接报错；这里留下确定性标记，
                # 让 _already_done 不必依赖「猜」。
                self._mark_completed(state, stage_name)
                await bump(item_id, ctx.overall_for(stage_name, 100))

            await ctx.reporter.item_update(
                item_id, status=TaskStatus.SUCCEEDED.value, progress=100.0, message="完成", error=""
            )
            await bump(item_id, 100.0)
            await self._log_item_summary(ctx, state, time.perf_counter() - item_started)
            await ctx.reporter.log(f"视频处理完成：{state.item.title_zh or state.item.title}", item_id=item_id)

        except (TaskCanceled, asyncio.CancelledError):
            # 子进程被杀掉时阶段会抛 CancelledError；这里统一按「已取消」处理，
            # 绝不写成「条目失败」——否则用户会看到一堆取消导致的红点。
            with contextlib.suppress(Exception):
                await ctx.reporter.item_update(item_id, status=TaskStatus.CANCELED.value, message="已取消")
            raise TaskCanceled("任务已取消")
        except Exception as exc:  # noqa: BLE001 - 单条失败不影响同任务其他条目
            message = str(exc)
            logger.exception("条目 %s 处理失败", item_id)
            await ctx.reporter.item_update(
                item_id,
                status=TaskStatus.FAILED.value,
                error=message[-4000:],
                message=f"失败于「{failed_stage}」",
            )
            # 失败时也要给汇总：卡在哪一步、那一步花了多久，正是排查要的信息
            with contextlib.suppress(Exception):
                await self._log_item_summary(
                    ctx, state, time.perf_counter() - item_started, failed_stage=failed_stage
                )
            await ctx.reporter.log(
                f"处理失败（阶段：{failed_stage}）：{message}", level="error", stage=failed_stage, item_id=item_id
            )
            await bump(item_id, 100.0)

    def _mark_completed(self, state: ItemState, stage_name: str) -> None:
        """记录本阶段已实跑完成（供下次续跑直接跳过）。"""
        done = list(state.stats.get("stages_completed") or [])
        if stage_name not in done:
            done.append(stage_name)
            state.stats["stages_completed"] = done

    def _mark_skipped(self, state: ItemState, stage_name: str) -> None:
        """标记该阶段本轮被跳过（已有产物）。

        已经测到真实耗时的阶段保留原值——那是上一次实跑的数据，
        比「跳过」有信息量得多。
        """
        timings = dict(state.stats.get("timings") or {})
        if stage_name in timings:
            return
        timings[stage_name] = {"seconds": 0.0, "skipped": True}
        state.stats["timings"] = timings

    async def _log_item_summary(
        self, ctx: StageContext, state: ItemState, elapsed: float, *, failed_stage: str = ""
    ) -> None:
        """条目跑完/失败后汇总各阶段耗时，按耗时降序排列。"""
        state.stats["total_seconds"] = round(elapsed, 1)
        detail = summarize_timings(state.stats.get("timings") or {})
        headline = f"本条耗时合计 {_human_duration(elapsed)}"
        if detail:
            headline += f"（按耗时排序：{detail}）"
        if failed_stage:
            headline += f"；失败于「{STAGE_LABELS.get(failed_stage, failed_stage)}」"
        await ctx.reporter.log(
            headline, level="warning" if failed_stage else "info", item_id=state.item.id
        )
        await ctx.reporter.item_update(
            state.item.id, stats={**(state.item.stats or {}), **state.stats}
        )

    async def _run_stage(
        self,
        ctx: StageContext,
        stage_name: str,
        stage_fn: StageFn,
        state: ItemState,
    ) -> None:
        label = STAGE_LABELS.get(stage_name, stage_name)
        await ctx.reporter.task_update(stage=stage_name, message=f"[{state.item.title[:40]}] {label}")
        await ctx.reporter.log(f"进入阶段：{label}", stage=stage_name, item_id=state.item.id)
        started = time.perf_counter()
        try:
            await stage_fn(ctx, state)
        except ProviderError as exc:
            elapsed = time.perf_counter() - started
            await self._record_timing(ctx, state, stage_name, elapsed, failed=True)
            raise ProviderError(f"{label}：{exc}") from exc
        except BaseException:
            # 取消/超时也要留下耗时：否则最需要优化的「卡住的那一步」恰好没有数据
            elapsed = time.perf_counter() - started
            await self._record_timing(ctx, state, stage_name, elapsed, failed=True)
            raise
        await self._record_timing(ctx, state, stage_name, time.perf_counter() - started)

    async def _record_timing(
        self,
        ctx: StageContext,
        state: ItemState,
        stage_name: str,
        elapsed: float,
        *,
        failed: bool = False,
    ) -> None:
        """记录单个阶段的耗时：写进 stats 供后续分析，同时打一条人能读的日志。

        为什么要单独记：一次搬运里各阶段耗时差着数量级（下载几十秒、语音合成几分钟、
        发布几分钟），只说「任务用了 18 分钟」根本看不出该优化哪里。
        """
        label = STAGE_LABELS.get(stage_name, stage_name)
        entry: dict[str, Any] = {
            # 保留到 0.01 秒：0.1 秒的粒度会把很快的阶段记成 0.0，
            # 和「本轮跳过、没有耗时」混成同一个样子
            "seconds": round(elapsed, 2),
            "at": utcnow().isoformat(timespec="seconds"),
        }
        if failed:
            entry["failed"] = True
        timings = dict(state.stats.get("timings") or {})
        timings[stage_name] = entry
        state.stats["timings"] = timings

        level = "warning" if (failed or elapsed >= _SLOW_STAGE_SECONDS) else "info"
        note = "（失败）" if failed else ("（偏慢）" if elapsed >= _SLOW_STAGE_SECONDS else "")
        try:
            await ctx.reporter.log(
                f"阶段耗时｜{label}：{_human_duration(elapsed)}{note}",
                level=level,
                stage=stage_name,
                item_id=state.item.id,
            )
            # 与各阶段自己的写法保持一致：把累积的 stats 合并落库
            await ctx.reporter.item_update(
                state.item.id, stats={**(state.item.stats or {}), **state.stats}
            )
        except Exception:  # noqa: BLE001 - 打点失败绝不能影响流水线本身
            logger.debug("记录阶段耗时失败：%s", stage_name, exc_info=True)

    def _already_done(self, stage_name: str, state: ItemState, item: TaskItem, ctx: StageContext) -> bool:
        """断点续跑：已有产物则跳过已完成的阶段，避免重复下载/重复计费。"""
        paths = state.paths
        if stage_name == "probe":
            # probe 没有外部产物，只能看条目元信息；实跑过的标记是最可靠的依据
            if stage_name in (state.stats.get("stages_completed") or []):
                return True
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
            # 注意判断条件保留 subtitle_zh 的存在性：后面写文件用的是这个路径
            if not (paths.subtitle_zh.exists() and state.cues_zh):
                return False
            # 开头语配置变了必须再进一次这个阶段（它就是在这里插入的）
            if localize_service.intro_needs_redo(ctx, state):
                return False
            # 换了翻译服务/模型后，旧译文（可能是 mock 占位）必须重做
            if (state.stats.get("translate") or {}).get("config_key") != translate_config_key(ctx):
                return False
            # 统一开头语是在这个阶段插进去的：文案/停顿/开关变了必须再进一次，
            # 否则改完设置重跑，字幕里还是上一轮的旧开场白（配音指纹变了、
            # 会重新合成，但字幕不会更新——这种「声画不一致」很难排查）
            return False if localize_service.intro_needs_redo(ctx, state) else True
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

        # 统一开头语：标记写进 SRT 就会丢失，因此持久化的事实是 stats["intro"]。
        # 这里只在「恢复出来的字幕看起来带开头语、而 stats 又没有记录」时补一条，
        # 让下一次运行能识别并撤掉它（配置改了要重做，配置没变则不重复插入）。
        intro_cfg = intro_service.resolve(ctx.config.merged("intro"))
        intro_settings = intro_service.intro_settings(intro_cfg)
        intro_stats = state.stats.get("intro") or {}
        if (
            intro_settings["enabled"]
            and state.cues_zh
            and state.cues_zh[0].start < 0.35
            and (state.cues_zh[0].text or "").strip() == str(intro_stats.get("text") or state.cues_zh[0].text).strip()
        ):
            # 续跑：SRT 里已经带着上一轮插入的开头语。
            # 这里必须把标记补回 Cue 上 —— SRT 不保存标记，而后续阶段要靠标记
            # 来判断「这条是开头语、别当正片处理」；缺了它，translate 阶段重建字幕
            # 时开头语会被当成普通句子丢掉（真实踩过：续跑后 6 句变 5 句）。
            first = state.cues_zh[0]
            state.cues_zh[0] = replace(first, source_indexes=[*first.source_indexes, intro_service.INTRO_MARKER])
            if not intro_stats.get("offset"):
                state.stats["intro"] = {
                    "mode": "restored",
                    "config_key": "",
                    "text": first.text,
                    "gap_seconds": intro_settings["gap_seconds"],
                    "show_in_subtitle": intro_settings["show_in_subtitle"],
                    "offset": round(first.end, 3),
                }

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
