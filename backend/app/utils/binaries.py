"""可执行文件探测。

GUI 或非登录 shell 启动的进程往往拿不到 nvm / Homebrew 的 PATH，
因此统一在这里主动探测常见安装位置，避免「命令明明装了却找不到」。
"""

from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path

# 常见安装位置（按优先级）
EXTRA_BIN_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/opt/local/bin",
    "/usr/bin",
    "/bin",
)

# 用户级安装位置（glob 展开）
_USER_BIN_GLOBS = (
    "~/.nvm/versions/node/*/bin",
    "~/.deno/bin",
    "~/.bun/bin",
    "~/.local/bin",
    "~/.cargo/bin",
    "~/Library/pnpm",
)


def _iter_candidate_dirs() -> list[str]:
    dirs = list(EXTRA_BIN_DIRS)
    for pattern in _USER_BIN_GLOBS:
        expanded = os.path.expanduser(pattern)
        if "*" in expanded:
            import glob

            # 版本号目录：倒序，优先取较新版本
            dirs.extend(sorted(glob.glob(expanded), reverse=True))
        elif Path(expanded).is_dir():
            dirs.append(expanded)
    return dirs


@lru_cache(maxsize=32)
def resolve_binary(name: str) -> str | None:
    """在 PATH 与常见目录中查找可执行文件，返回绝对路径。"""
    found = shutil.which(name)
    if found:
        return found
    for directory in _iter_candidate_dirs():
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


# yt-dlp 支持的 JavaScript 运行时。deno 是 yt-dlp 的默认项，其余需显式启用。
# 顺序即优先级：deno > node > bun > quickjs
JS_RUNTIME_NAMES = ("deno", "node", "bun", "quickjs")
JS_RUNTIME_BINARIES = {
    "deno": "deno",
    "node": "node",
    "bun": "bun",
    "quickjs": "qjs",
}


@lru_cache(maxsize=1)
def resolve_js_runtime(preferred: str = "") -> tuple[str, str] | None:
    """探测可用的 JavaScript 运行时，返回 (名称, 可执行文件路径)。

    YouTube 提取现在需要 JS 运行时来解算签名；yt-dlp 默认只启用 deno，
    本机常见的是 node，因此需要主动探测并显式告知 yt-dlp。
    """
    order = [preferred] if preferred else []
    order += [name for name in JS_RUNTIME_NAMES if name != preferred]
    for name in order:
        if name not in JS_RUNTIME_BINARIES:
            continue
        path = resolve_binary(JS_RUNTIME_BINARIES[name])
        if path:
            return name, path
    return None


def clear_cache() -> None:
    """用户安装新运行时后，允许重新探测（供「运行环境自检」调用）。"""
    resolve_binary.cache_clear()
    resolve_js_runtime.cache_clear()
