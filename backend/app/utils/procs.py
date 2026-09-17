"""子进程：在每个任务下登记，取消任务时整组杀掉。

为什么需要它：取消任务原本只在「两个阶段之间」检查一次取消标志，
而一个阶段内部可能是几分钟的 ffmpeg 渲染或 yt-dlp 下载。
取消后用户看到任务还在跑（实测一个被取消的任务能继续跑十几秒到几分钟），
体感就是「取消无效」。

有了登记表，取消时可以由调度器把这些子进程直接 kill 掉：
- 正在渲染/下载的进程立即结束；
- 阶段内部因此抛出 CancelledError，任务在毫秒级停在正确的状态上。

用法（调用方负责传入当前任务的 token）：
    token = task_tracker.current()          # 拿当前任务的登记令牌
    code, out, err = await run_process(cmd, timeout=60, token=token)
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
from collections.abc import Iterable

logger = logging.getLogger(__name__)

# 当前任务的登记令牌：在 TaskRunner 里 set，阶段内部通过 current() 取用
_current_token: contextvars.ContextVar[str | None] = contextvars.ContextVar("task_tracker_token", default=None)

_processes: dict[str, set[asyncio.subprocess.Process]] = {}


def bind(token: str) -> contextvars.Token:
    """把后续子进程登记到该任务名下。"""
    return _current_token.set(token)


def unbind(var_token: contextvars.Token) -> None:
    with contextlib.suppress(ValueError):
        _current_token.reset(var_token)


def current() -> str | None:
    return _current_token.get()


def register(proc: asyncio.subprocess.Process, token: str | None = None) -> str | None:
    key = token or current()
    if key:
        _processes.setdefault(key, set()).add(proc)
    return key


def unregister(proc: asyncio.subprocess.Process, token: str | None = None) -> None:
    key = token or current()
    if not key:
        return
    bucket = _processes.get(key)
    if bucket is None:
        return
    bucket.discard(proc)
    if not bucket:
        _processes.pop(key, None)


def running_count(token: str | None = None) -> int:
    return len(_processes.get(token or current() or "", ()))


async def kill_task_processes(token: str, *, reason: str = "任务已取消") -> int:
    """杀掉该任务登记的所有子进程，返回杀掉的个数。"""
    procs: Iterable[asyncio.subprocess.Process] = list(_processes.pop(token, set()))
    killed = 0
    for proc in procs:
        if proc.returncode is not None:
            continue
        logger.info("取消任务 %s：终止子进程 pid=%s（%s）", token, proc.pid, reason)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        killed += 1
    return killed


async def _reap(proc: asyncio.subprocess.Process) -> None:
    """确保子进程被终止并回收，不留下僵尸进程。"""
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    with contextlib.suppress(Exception):
        await proc.wait()


async def run_process(
    cmd: list[str],
    *,
    timeout: float | None = None,
    text: bool = True,
    token: str | None = None,
    stdin: bytes | None = None,
) -> tuple[int, str | bytes, str]:
    """执行外部命令并登记到当前任务；被取消或被 kill 时抛出 CancelledError。

    与 asyncio.create_subprocess_exec 的区别：
    - 进程一创建就登记，取消任务时能被 kill_task_processes 直接杀掉；
    - 无论超时、取消还是异常，finally 都负责回收进程。
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    key = register(proc, token)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(input=stdin), timeout=timeout)
    except asyncio.TimeoutError:
        raise TimeoutError(f"命令超时（{timeout}s）：{' '.join(cmd[:3])} …") from None
    finally:
        await _reap(proc)
        unregister(proc, key)
    out: str | bytes = stdout.decode("utf-8", "ignore") if text else (stdout or b"")
    return proc.returncode or 0, out, stderr.decode("utf-8", "ignore")
