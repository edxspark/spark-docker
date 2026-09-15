"""提供者工厂：根据配置选择真实实现或 Mock 实现。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.providers.asr.aliyun import AliyunASR, MockASR
from app.providers.asr.whisper import WhisperASR
from app.providers.base import (
    BaseASR,
    BasePublisher,
    BaseTranslator,
    BaseTTS,
    NotConfiguredError,
    ProbeResult,
)
from app.providers.downloader.ytdlp import YtDlpDownloader
from app.providers.publisher.douyin import DouyinPublisher, MockPublisher
from app.providers.translator.deepseek import DeepSeekTranslator, MockTranslator
from app.providers.tts.aliyun import AliyunTTS, MockTTS
from app.services.settings_store import (
    ASRConfig,
    DownloadConfig,
    PublishConfig,
    TranslatorConfig,
    TTSConfig,
)

__all__ = [
    "BaseASR",
    "BasePublisher",
    "BaseTranslator",
    "BaseTTS",
    "NotConfiguredError",
    "ProbeResult",
    "build_asr",
    "build_downloader",
    "build_publisher",
    "build_translator",
    "build_tts",
]


def build_translator(config: TranslatorConfig) -> BaseTranslator:
    if config.provider == "mock":
        return MockTranslator(config)
    return DeepSeekTranslator(config)


def build_tts(config: TTSConfig) -> BaseTTS:
    if config.provider == "mock":
        return MockTTS(config)
    return AliyunTTS(config)


def build_asr(config: ASRConfig, tts_config: TTSConfig) -> BaseASR:
    """语音识别复用语音合成的项目凭证（同一阿里云项目）。"""
    if config.provider == "mock":
        return MockASR(config)
    if config.provider == "whisper":
        return WhisperASR(config)
    return AliyunASR(config, tts_config)


def build_downloader(config: DownloadConfig) -> YtDlpDownloader:
    return YtDlpDownloader(config)


def build_publisher(config: PublishConfig, account_file: Path) -> BasePublisher:
    if config.provider == "mock":
        return MockPublisher(config)
    return DouyinPublisher(config, account_file)


def build_all(config: dict[str, Any], account_file: Path) -> dict[str, Any]:
    """一次性构建全部提供者，供任务执行器使用。"""
    tts_config = TTSConfig(**config["tts"])
    asr_config = ASRConfig(**config.get("asr", {}))

    def asr_factory() -> BaseASR:
        return build_asr(asr_config, tts_config)

    return {
        "downloader": build_downloader(DownloadConfig(**config["download"])),
        "translator": build_translator(TranslatorConfig(**config["translator"])),
        "tts": build_tts(tts_config),
        # 语音识别是「无字幕时」的兜底能力，惰性构造：
        # 它缺凭证绝不能影响有字幕视频的正常流水线。
        "asr_factory": asr_factory,
        "asr_config": asr_config,
        "publisher": build_publisher(PublishConfig(**config["publish"]), account_file),
    }
