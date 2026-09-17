"""yt-dlp 下载提供者：探测链接 + 下载视频/字幕/封面。

- probe: 合集/频道用 extract_flat 快速列出条目，不做完整解析。
- download: 先尝试人工字幕，失败再回退自动字幕；统一转换为 srt。
- 进度通过 yt-dlp 的 progress_hooks 回调上报，供前端实时展示。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.providers.base import DownloadResult, ProbeResult, ProviderError, VideoInfo
from app.services.settings_store import DownloadConfig
from app.utils import binaries

logger = logging.getLogger(__name__)


class DownloadCanceled(RuntimeError):
    pass


# yt-dlp 会输出 ANSI 颜色码（例如 \x1b[0;31mERROR:\x1b[0m），
# 直接带进任务日志/界面会显示成乱码，这里统一剥掉。
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "").strip()


class _ErrorCollector:
    """收集 yt-dlp 通过 logger 上报的错误/警告。

    必要性：即便 ignoreerrors=False，仍有部分失败路径只写日志而不抛异常
    （例如分片下载失败、后处理失败）。不收集的话，用户只会看到一句无用的兜底文案。
    """

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    # yt-dlp 的 Logger 协议
    def debug(self, msg: Any) -> None:
        text = _strip_ansi(str(msg))
        # yt-dlp 把普通输出也走 debug，需要过滤掉噪音
        if text.startswith("[debug] "):
            return

    def info(self, msg: Any) -> None:
        return

    def warning(self, msg: Any) -> None:
        text = _strip_ansi(str(msg))
        if text and text not in self.warnings:
            self.warnings.append(text)

    def error(self, msg: Any) -> None:
        text = _strip_ansi(str(msg))
        if text and text not in self.errors:
            self.errors.append(text)

    def summary(self) -> str:
        if self.errors:
            return "；".join(self.errors[-3:])
        if self.warnings:
            return "；".join(self.warnings[-3:])
        return ""


@dataclass
class _Progress:
    callback: Any = None
    cancel_check: Any = None


def resolve_cookies_file(config: DownloadConfig) -> Path | None:
    """决定本次下载用哪个 cookies 文件。

    优先用户显式配置的路径；没有配置时回退到「应用自己导出」的那份
    （由「登录 YouTube 并导出 cookies」生成）。这样用户登录一次之后不必再手动填
    路径，也不会因为忘了填而继续撞风控。

    显式配置但路径无效时必须直接报错：静默忽略只会让用户面对一个看起来毫无头绪的
    「需要登录」错误，而根因其实是他把路径写错了。
    """
    if config.cookies_file:
        path = Path(config.cookies_file).expanduser()
        if not path.exists():
            raise ProviderError(
                f"配置的 cookies 文件不存在：{path}。"
                "请在「系统配置 → 下载 → Cookies 文件」里改成正确的绝对路径，"
                "或清空该字段，改用应用内置的「登录 YouTube 并导出 cookies」"
            )
        if path.stat().st_size == 0:
            raise ProviderError(f"配置的 cookies 文件是空的：{path}")
        return path

    default = settings.data_dir / "auth" / "youtube_cookies.txt"
    if default.exists() and default.stat().st_size > 0:
        return default
    return None


def _base_opts(config: DownloadConfig) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "nocheckcertificate": True,
        # 不在全局开启 ignoreerrors：单视频下载需要让错误抛出来，
        # 由流水线按条目粒度记录并展示真实原因。合集里单个视频失败不会影响其他条目，
        # 因为每个条目都是独立的一次下载调用。
        "ignoreerrors": False,
        "retries": config.retries,
        "fragment_retries": config.retries,
        "socket_timeout": 30,
        "noplaylist": False,
        "restrictfilenames": False,
        "windowsfilenames": False,
    }
    if config.proxy:
        opts["proxy"] = config.proxy
    cookies = resolve_cookies_file(config)
    if cookies:
        opts["cookiefile"] = str(cookies)
    if config.sleep_interval > 0:
        opts["sleep_interval"] = config.sleep_interval
    if config.rate_limit:
        opts["ratelimit"] = config.rate_limit

    # yt-dlp 需要自己找到 ffmpeg 才能合并音视频。GUI 启动的进程 PATH 里通常没有
    # Homebrew，会导致「ffmpeg is not installed」而合并失败，因此显式告知路径。
    ffmpeg_path = binaries.resolve_binary(settings.ffmpeg_bin)
    if ffmpeg_path:
        opts["ffmpeg_location"] = str(Path(ffmpeg_path).parent)

    # YouTube 提取需要 JavaScript 运行时解算签名；yt-dlp 默认只启用 deno，
    # 本机更常见的是 node，因此主动探测并显式启用。
    runtime = binaries.resolve_js_runtime(config.js_runtime)
    if runtime:
        name, path = runtime
        opts["js_runtimes"] = {name: {"path": path}}
    return opts


_NETWORK_HINT = (
    "网络无法访问 YouTube。若在中国大陆，请在「系统配置 → 下载」中配置代理"
    "（例如 http://127.0.0.1:7890）；若视频需要登录，请配置 cookies 文件。"
)

# 判定「瞬时网络故障」的关键词：命中才值得重试
_TRANSIENT_MARKERS = (
    "timed out",
    "timeout",
    "connection",
    "getaddrinfo",
    "tunnel",
    "ssl",
    "giving up after",
    "unable to download",
    "read error",
    "temporary failure",
)


def _looks_transient(summary: str) -> bool:
    lowered = (summary or "").lower()
    return any(marker in lowered for marker in _TRANSIENT_MARKERS)


def _friendly_error(exc: Exception, url: str = "", action: str = "解析链接") -> str:
    """把 yt-dlp 的原始报错翻译成用户能照做的提示。"""
    text = _strip_ansi(str(exc))
    lowered = text.lower()
    if any(
        key in lowered
        for key in ("timed out", "timeout", "urlopen error", "connection", "getaddrinfo", "tunnel", "ssl", "giving up after")
    ):
        return f"{action}失败：{_NETWORK_HINT}（原始错误：{text[:300]}）"
    if "sign in" in lowered or "confirm your age" in lowered or "private video" in lowered:
        return (
            f"{action}失败：该视频需要登录（YouTube 对匿名请求做了「确认你不是机器人」风控）。"
            "两种解法，任选其一："
            "① 点「系统配置 → 下载 → 登录 YouTube 并导出 cookies」，"
            "在弹出的浏览器里登录一次即可（推荐，不需要装插件或改系统权限）；"
            "或运行 ./scripts/youtube_login.sh。"
            "② 自己导出 cookies.txt，把绝对路径填到「系统配置 → 下载 → Cookies 文件」。"
            f"（原始错误：{text[:300]}）"
        )
    if "unsupported url" in lowered or "is not a valid url" in lowered:
        return f"{action}失败：不支持的链接格式：{url[:200]}"
    if "requested format is not available" in lowered or "no video formats found" in lowered:
        return (
            f"{action}失败：没有符合当前格式表达式的清晰度可用。"
            "请在「系统配置 → 下载」中把 yt-dlp 格式改回默认值或下调最大分辨率。"
            f"（原始错误：{text[:300]}）"
        )
    if "ffmpeg" in lowered:
        return f"{action}失败：需要 ffmpeg 合并音视频但未找到，请执行 brew install ffmpeg。（原始错误：{text[:300]}）"
    if "http error 403" in lowered or "forbidden" in lowered:
        return (
            f"{action}失败：被 YouTube 拒绝（403）。常见原因是 IP 被限流或需要登录，"
            f"可尝试配置代理或 cookies 文件后重试。（原始错误：{text[:300]}）"
        )
    if "impersonat" in lowered:
        return (
            f"{action}失败：缺少 impersonation 支持（TLS 指纹伪装），YouTube 会因此拒绝请求。"
            "请在 backend 目录执行：uv pip install curl-cffi，然后重启服务。"
            f"（原始错误：{text[:300]}）"
        )
    if "javascript runtime" in lowered or "js runtime" in lowered:
        return (
            f"{action}失败：未找到 JavaScript 运行时，YouTube 提取需要它。"
            "任选其一：brew install deno，或确保 node 在 PATH 中（本项目会自动探测并启用）。"
            f"（原始错误：{text[:300]}）"
        )
    if "ffmpeg is not installed" in lowered or "ffmpeg not found" in lowered:
        return (
            f"{action}失败：yt-dlp 找不到 ffmpeg，无法合并音视频。"
            "请执行 brew install ffmpeg，然后重启服务。"
            f"（原始错误：{text[:300]}）"
        )
    return f"{action}失败：{text[:400] or 'yt-dlp 未返回任何错误信息'}"


def _probe_sync(url: str, config: DownloadConfig) -> ProbeResult:
    import yt_dlp

    # 探测阶段要让用户尽快拿到反馈：收窄重试与超时，
    # 否则在网络不通（例如忘了配代理）时会挂起数分钟才报错。
    opts = _base_opts(config) | {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "retries": 1,
        "fragment_retries": 1,
        "extractor_retries": 1,
        "socket_timeout": 20,
        "ignoreerrors": False,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001
        raise ProviderError(_friendly_error(exc, url)) from exc

    if info is None:
        raise ProviderError("解析链接失败：未获取到任何信息，请检查链接是否有效或是否需要登录")

    info = dict(info)
    is_playlist = info.get("_type") in ("playlist", "multi_video") or bool(info.get("entries"))
    if not is_playlist:
        video = VideoInfo.from_ytdlp(info)
        return ProbeResult(
            source_type="video",
            title=video.title,
            author=video.author,
            source_id=video.video_id,
            entries=[video],
        )

    raw_entries = [e for e in (info.get("entries") or []) if e]
    entries: list[VideoInfo] = []
    for entry in raw_entries:
        entry = dict(entry)
        # extract_flat 下没有 webpage_url 时用 id 拼装
        if not entry.get("webpage_url") and entry.get("id"):
            entry["webpage_url"] = f"https://www.youtube.com/watch?v={entry['id']}"
        entries.append(VideoInfo.from_ytdlp(entry))

    uploader = info.get("uploader") or info.get("channel") or (entries[0].author if entries else "")
    source_type = "channel" if "/channel/" in url or "/@" in url else "playlist"
    return ProbeResult(
        source_type=source_type,
        title=str(info.get("title") or "未命名合集"),
        author=str(uploader or ""),
        source_id=str(info.get("id") or ""),
        entries=entries,
    )


def _find_video(directory: Path, video_id: str) -> Path | None:
    """在下载目录中找出已完成的视频文件。"""
    candidates = [
        p
        for p in directory.glob(f"{video_id}.*")
        if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"} and not p.name.endswith(".part")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


def _find_subtitle(directory: Path, video_id: str, langs: list[str]) -> Path | None:
    """在下载目录里找出该视频最合适的字幕文件。"""
    candidates: list[Path] = []
    for pattern in (f"{video_id}*.srt", f"{video_id}*.vtt", f"{video_id}*.json3"):
        candidates.extend(directory.glob(pattern))
    if not candidates:
        return None

    def score(path: Path) -> tuple[int, int, int]:
        name = path.name
        manual = 0 if ".auto." not in name and not name.endswith(".en-orig.srt") else 1
        lang_rank = next((i for i, lang in enumerate(langs) if f".{lang}." in name), len(langs))
        ext_rank = {".srt": 0, ".vtt": 1, ".json3": 2}.get(path.suffix.lower(), 3)
        return (manual, lang_rank, ext_rank)

    return sorted(candidates, key=score)[0]


def _download_sync(
    item: VideoInfo,
    output_dir: Path,
    config: DownloadConfig,
    *,
    progress=None,
    cancel_check=None,
) -> DownloadResult:
    import yt_dlp

    output_dir.mkdir(parents=True, exist_ok=True)
    url = item.url or item.webpage_url or f"https://www.youtube.com/watch?v={item.video_id}"

    def hook(data: dict[str, Any]) -> None:
        if cancel_check and cancel_check():
            raise DownloadCanceled("任务已取消")
        if progress is None:
            return
        status = data.get("status")
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            downloaded = data.get("downloaded_bytes") or 0
            pct = (downloaded / total * 100.0) if total else 0.0
            speed = data.get("speed") or 0
            speed_text = f"{speed / 1024 / 1024:.1f} MB/s" if speed else ""
            progress(min(pct, 100.0), f"下载中 {pct:.0f}% {speed_text}".strip())
        elif status == "finished":
            progress(100.0, "下载完成，正在合并音视频…")

    common = _base_opts(config) | {
        "progress_hooks": [hook],
        "outtmpl": str(output_dir / "%(id)s.%(ext)s"),
        "format": config.format,
        "merge_output_format": "mp4",
        "writethumbnail": config.write_thumbnail,
    }

    subtitle_langs = config.subtitle_langs or ["en"]

    def attempt(*, manual: bool) -> tuple[Path | None, str]:
        """下载视频（顺带取字幕）。返回 (字幕路径, 错误摘要)。"""
        collector = _ErrorCollector()
        opts = dict(common)
        opts["logger"] = collector
        opts["writesubtitles"] = manual
        opts["writeautomaticsub"] = not manual
        opts["subtitleslangs"] = subtitle_langs
        opts["subtitlesformat"] = "srt/best"
        opts["convertsubtitles"] = "srt"
        retcode = 0
        raised: Exception | None = None
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                retcode = ydl.download([url]) or 0
        except DownloadCanceled:
            raise
        except Exception as exc:  # noqa: BLE001
            raised = exc
        summary = collector.summary() or (str(raised) if raised else "")
        if not summary and retcode not in (0, None):
            summary = f"yt-dlp 返回非零状态码 {retcode}，但未提供具体原因"
        return _find_subtitle(output_dir, item.video_id, subtitle_langs), summary

    subtitle_path: Path | None = None
    subtitle_kind = "none"
    order = [True, False] if config.prefer_manual_subtitle else [False, True]
    errors: list[str] = []

    # 直连 YouTube 时常出现瞬时超时（探测成功、下载时又超时）。yt-dlp 内部已按
    # config.retries 重试过，这里只额外补一轮：仅在错误看起来是网络问题时重试，
    # 避免在确定性的失败（格式不可用、需要登录等）上反复等待。
    max_rounds = 2
    for round_index in range(max_rounds):
        if _find_video(output_dir, item.video_id):
            break
        for manual in order:
            try:
                subtitle_path, error_summary = attempt(manual=manual)
            except DownloadCanceled:
                raise
            if error_summary:
                errors.append(error_summary)
                logger.warning("下载尝试失败（第 %s 轮, manual=%s）：%s", round_index + 1, manual, error_summary)
            if subtitle_path:
                subtitle_kind = "manual" if manual else "auto"
                break
            # 视频已下载成功时不必再试第二种字幕模式
            if _find_video(output_dir, item.video_id):
                break
        if _find_video(output_dir, item.video_id):
            break
        if round_index >= max_rounds - 1 or not _looks_transient(errors[-1] if errors else ""):
            break
        wait = 5 * (round_index + 1)
        logger.info("下载失败疑似瞬时网络问题，%s 秒后重试（第 %s/%s 轮）", wait, round_index + 2, max_rounds)
        time.sleep(wait)

    video_path = _find_video(output_dir, item.video_id)
    if video_path is None:
        reason = errors[-1] if errors else "yt-dlp 未返回任何错误信息，可能是输出目录权限问题"
        raise ProviderError(_friendly_error(RuntimeError(reason), url, action="下载视频"))

    thumbnail_path = next(
        (p for p in output_dir.glob(f"{item.video_id}.*") if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}),
        None,
    )

    if subtitle_path is None:
        logger.info("视频 %s 没有可用英文字幕，后续将回退为静音/原声处理", item.video_id)

    return DownloadResult(
        video_path=video_path,
        subtitle_path=subtitle_path,
        thumbnail_path=thumbnail_path,
        info=item,
        subtitle_kind=subtitle_kind,
    )


class YtDlpDownloader:
    name = "yt-dlp"

    def __init__(self, config: DownloadConfig) -> None:
        self.config = config

    async def probe(self, url: str) -> ProbeResult:
        return await asyncio.to_thread(_probe_sync, url, self.config)

    async def download(
        self,
        item: VideoInfo,
        output_dir: Path,
        *,
        progress=None,
        cancel_check=None,
    ) -> DownloadResult:
        return await asyncio.to_thread(
            _download_sync,
            item,
            output_dir,
            self.config,
            progress=progress,
            cancel_check=cancel_check,
        )

    async def fetch_thumbnail(self, item: VideoInfo, out_path: Path) -> Path | None:
        """把远程缩略图落盘，作为发布封面候选。"""
        url = item.thumbnail
        if not url:
            return None
        import httpx

        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                response = await client.get(url)
            if response.status_code != 200:
                return None
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(response.content)
            return out_path
        except Exception as exc:  # noqa: BLE001
            logger.warning("缩略图下载失败：%s", exc)
            return None
