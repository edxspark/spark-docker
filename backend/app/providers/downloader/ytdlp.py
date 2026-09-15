"""yt-dlp 下载提供者：探测链接 + 下载视频/字幕/封面。

- probe: 合集/频道用 extract_flat 快速列出条目，不做完整解析。
- download: 先尝试人工字幕，失败再回退自动字幕；统一转换为 srt。
- 进度通过 yt-dlp 的 progress_hooks 回调上报，供前端实时展示。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.providers.base import DownloadResult, ProbeResult, ProviderError, VideoInfo
from app.services.settings_store import DownloadConfig

logger = logging.getLogger(__name__)


class DownloadCanceled(RuntimeError):
    pass


@dataclass
class _Progress:
    callback: Any = None
    cancel_check: Any = None


def _base_opts(config: DownloadConfig) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "nocheckcertificate": True,
        "ignoreerrors": True,
        "retries": config.retries,
        "fragment_retries": config.retries,
        "socket_timeout": 30,
        "noplaylist": False,
        "restrictfilenames": False,
        "windowsfilenames": False,
    }
    if config.proxy:
        opts["proxy"] = config.proxy
    if config.cookies_file:
        opts["cookiefile"] = config.cookies_file
    if config.sleep_interval > 0:
        opts["sleep_interval"] = config.sleep_interval
    if config.rate_limit:
        opts["ratelimit"] = config.rate_limit
    return opts


_NETWORK_HINT = (
    "网络无法访问 YouTube。若在中国大陆，请在「系统配置 → 下载」中配置代理"
    "（例如 http://127.0.0.1:7890）；若视频需要登录，请配置 cookies 文件。"
)


def _friendly_error(exc: Exception, url: str = "") -> str:
    """把 yt-dlp 的原始报错翻译成用户能照做的提示。"""
    text = str(exc)
    lowered = text.lower()
    if any(key in lowered for key in ("timed out", "timeout", "urlopen error", "connection", "getaddrinfo", "tunnel", "ssl")):
        return f"解析链接失败：{_NETWORK_HINT}（原始错误：{text[:300]}）"
    if "sign in" in lowered or "confirm your age" in lowered or "private video" in lowered:
        return f"解析链接失败：该视频需要登录，请在「系统配置 → 下载」中配置 cookies 文件。（原始错误：{text[:300]}）"
    if "unsupported url" in lowered:
        return f"解析链接失败：不支持的链接格式：{url[:200]}"
    return f"解析链接失败：{text[:400]}"


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
        "postprocessors": [],
    }

    subtitle_langs = config.subtitle_langs or ["en"]

    def attempt(*, manual: bool) -> Path | None:
        opts = dict(common)
        opts["writesubtitles"] = manual
        opts["writeautomaticsub"] = not manual
        opts["subtitleslangs"] = subtitle_langs
        opts["subtitlesformat"] = "srt/best"
        opts["convertsubtitles"] = "srt"
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        return _find_subtitle(output_dir, item.video_id, subtitle_langs)

    subtitle_path: Path | None = None
    subtitle_kind = "none"
    order = [True, False] if config.prefer_manual_subtitle else [False, True]
    last_error: Exception | None = None
    for manual in order:
        try:
            subtitle_path = attempt(manual=manual)
        except DownloadCanceled:
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.warning("字幕下载失败（manual=%s）：%s", manual, exc)
            continue
        if subtitle_path:
            subtitle_kind = "manual" if manual else "auto"
            break

    video_files = sorted(
        (p for p in output_dir.glob(f"{item.video_id}.*") if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    if not video_files:
        raise ProviderError(
            f"视频下载失败：未找到输出文件（{last_error or '可能视频不可用、需要登录或存在地区限制'}）"
        )
    video_path = video_files[0]

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
