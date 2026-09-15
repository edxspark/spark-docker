"""提供者层基类与公共数据结构。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services.subtitles import Cue

ProgressCallback = Callable[[float, str], None]


class ProviderError(RuntimeError):
    """提供者调用失败（网络、鉴权、配额等）。"""


class NotConfiguredError(ProviderError):
    """缺少必要配置（如 API Key）。"""


# --------------------------------------------------------------------------------------
# 下载
# --------------------------------------------------------------------------------------


@dataclass
class VideoInfo:
    video_id: str = ""
    url: str = ""
    title: str = ""
    description: str = ""
    author: str = ""
    duration: float = 0.0
    thumbnail: str = ""
    upload_date: str = ""
    view_count: int = 0
    webpage_url: str = ""

    @classmethod
    def from_ytdlp(cls, data: dict[str, Any]) -> VideoInfo:
        return cls(
            video_id=str(data.get("id") or ""),
            url=str(data.get("webpage_url") or data.get("url") or ""),
            title=str(data.get("title") or ""),
            description=str(data.get("description") or "")[:5000],
            author=str(data.get("uploader") or data.get("channel") or ""),
            duration=float(data.get("duration") or 0.0),
            thumbnail=str(data.get("thumbnail") or ""),
            upload_date=str(data.get("upload_date") or ""),
            view_count=int(data.get("view_count") or 0),
            webpage_url=str(data.get("webpage_url") or ""),
        )


@dataclass
class ProbeResult:
    source_type: str                     # video / playlist / channel
    title: str = ""
    author: str = ""
    source_id: str = ""
    entries: list[VideoInfo] = field(default_factory=list)


@dataclass
class DownloadResult:
    video_path: Path
    subtitle_path: Path | None = None
    thumbnail_path: Path | None = None
    info: VideoInfo = field(default_factory=VideoInfo)
    subtitle_kind: str = ""              # manual / auto / none


# --------------------------------------------------------------------------------------
# 翻译
# --------------------------------------------------------------------------------------


@dataclass
class TranslateResult:
    texts: list[str]
    usage: dict[str, Any] = field(default_factory=dict)


class BaseTranslator(ABC):
    name = "base"

    @abstractmethod
    async def translate(self, texts: list[str], *, hint: str = "") -> TranslateResult:
        """批量翻译，返回与输入等长的译文列表。"""

    async def translate_one(self, text: str, *, hint: str = "") -> str:
        result = await self.translate([text], hint=hint)
        return result.texts[0] if result.texts else text

    async def generate_metadata(
        self,
        *,
        title: str,
        description: str = "",
        translated_body: str = "",
        max_title_len: int = 30,
        fallback_tags: list[str] | None = None,
        tag_limit: int = 5,
    ) -> tuple[str, list[str]]:
        """生成发布用中文标题与话题标签。

        默认实现（不依赖大模型）：标题直译 + 关键词抽取。
        子类可覆盖为更「标题党」的生成策略。
        """
        from app.utils.text import extract_tags, normalize_punct, truncate

        translated = await self.translate_one(title, hint="这是一段视频标题，请翻译成中文标题")
        title_zh = truncate(normalize_punct(translated or title), max_title_len)
        tags = extract_tags(f"{title} {description}", limit=tag_limit) or list(fallback_tags or [])
        return title_zh, tags[:tag_limit]


# --------------------------------------------------------------------------------------
# 语音合成
# --------------------------------------------------------------------------------------


@dataclass
class SynthesisResult:
    path: Path
    duration: float = 0.0
    characters: int = 0
    cached: bool = False


class BaseTTS(ABC):
    name = "base"

    @abstractmethod
    async def synthesize(self, text: str, out_path: Path, *, voice: str | None = None) -> SynthesisResult:
        """把文本合成为音频文件。"""


# --------------------------------------------------------------------------------------
# 发布
# --------------------------------------------------------------------------------------


@dataclass
class PublishRequest:
    video_path: Path
    title: str
    tags: list[str] = field(default_factory=list)
    cover_path: Path | None = None
    description: str = ""
    schedule_at: str | None = None        # "YYYY-MM-DD HH:MM"
    headless: bool = False


@dataclass
class PublishResult:
    success: bool
    work_url: str = ""
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class BasePublisher(ABC):
    name = "base"

    @abstractmethod
    async def publish(self, request: PublishRequest) -> PublishResult:
        """发布一条作品。"""


# --------------------------------------------------------------------------------------
# 语音识别（无字幕时的兜底）
# --------------------------------------------------------------------------------------


class BaseASR(ABC):
    """把媒体（视频/音频）里的语音转成带时间轴的字幕。

    用于「视频没有人工/自动字幕」的场景：先识别出原文，再走翻译与配音流程，
    最终产出带中文字幕与中文配音的成片。
    """

    name = "base"

    @abstractmethod
    async def transcribe(
        self,
        media: Path,
        *,
        on_progress: Callable[[float, str], Any] | None = None,
        total_duration: float | None = None,
        cache_dir: Path | None = None,
    ) -> list[Cue]:
        """返回带时间轴的识别结果。

        cache_dir：分块识别结果的落盘目录。语音识别又慢又贵，把每块结果缓存下来，
        任务中断后重试可以直接复用，不必从头再识别一遍。
        """
