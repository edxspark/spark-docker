"""本地语音识别（faster-whisper）。

为什么默认用它而不是云端 ASR：
    实测同一段 19 秒英文音频（内容已知），词准确率——
      阿里云一句话识别 ≈ 60%（"long trunks" → "warm funds"，"that's cool" → "that's course"）
      whisper base.en    ≈ 84%
      whisper small.en   ≈ 92%
    识别错的内容会被原样翻译成错误的中文，用户体感就是「字幕和说话内容对不上」。

另一项关键优势是**词级时间戳**：可以用真实时间轴把词合并成字幕条目，
而不是按字符数比例估算。云端接口只返回整段文本，长音频里误差可达数秒。

代价是首次使用需要下载模型（base.en 约 140MB，small.en 约 480MB），
以及纯 CPU 推理速度约为音频时长的 1/3~1/6（small 档实测 19 秒音频 3.3 秒）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from app.providers.base import BaseASR, ProviderError
from app.services.settings_store import ASRConfig
from app.services.subtitles import Cue, reindex
from app.utils import ffmpeg as ffmpeg_utils

logger = logging.getLogger(__name__)

# HuggingFace 在部分网络环境下不可达；失败时自动改用国内镜像重试一次
_HF_MIRROR = "https://hf-mirror.com"

# 句末标点：用于把词序列切成字幕条目
_SENTENCE_END = re.compile(r"[.!?。！？…]['\")\]]?$")
_CLAUSE_END = re.compile(r"[,;:，；：]$")

# 模型只在进程内加载一次：加载耗时远大于识别本身
_model_cache: dict[tuple[str, str, str], object] = {}
_model_lock = asyncio.Lock()


@dataclass
class _Word:
    start: float
    end: float
    text: str


def _load_model_sync(model_size: str, device: str, compute_type: str):
    from faster_whisper import WhisperModel

    try:
        return WhisperModel(model_size, device=device, compute_type=compute_type)
    except Exception as exc:  # noqa: BLE001
        # 常见于国内网络访问 huggingface.co 超时；换镜像重试一次
        message = str(exc).lower()
        if "connection" not in message and "timed out" not in message and "resolve" not in message:
            raise
        logger.warning("模型下载失败（%s），改用镜像 %s 重试", str(exc)[:120], _HF_MIRROR)
        os.environ["HF_ENDPOINT"] = _HF_MIRROR
        return WhisperModel(model_size, device=device, compute_type=compute_type)


async def _get_model(model_size: str, device: str, compute_type: str):
    key = (model_size, device, compute_type)
    async with _model_lock:
        if key not in _model_cache:
            logger.info("加载 Whisper 模型 %s（device=%s, compute_type=%s）…", model_size, device, compute_type)
            _model_cache[key] = await asyncio.to_thread(
                _load_model_sync, model_size, device, compute_type
            )
            logger.info("Whisper 模型就绪：%s", model_size)
        return _model_cache[key]


class WhisperASR(BaseASR):
    name = "whisper"
    cues_are_timed = True

    def __init__(self, config: ASRConfig) -> None:
        self.config = config

    async def transcribe(
        self,
        media: Path,
        *,
        on_progress=None,
        total_duration: float | None = None,
        cache_dir: Path | None = None,
    ) -> list[Cue]:
        media = Path(media)
        if not media.exists():
            raise ProviderError(f"待识别媒体不存在：{media}")

        try:
            import faster_whisper  # noqa: F401
        except ImportError as exc:
            raise ProviderError(
                "未安装本地语音识别依赖。请在 backend 目录执行："
                "uv pip install faster-whisper，然后重启服务"
            ) from exc

        duration = total_duration or (await ffmpeg_utils.probe(media)).duration
        limit = int(self.config.max_duration_minutes or 0)
        if limit and duration > limit * 60:
            raise ProviderError(
                f"视频时长 {duration / 60:.0f} 分钟超过语音识别上限 {limit} 分钟。"
                "可在「系统配置 → 语音识别」调整，或改用云端识别。"
            )

        # Whisper 对 16k 单声道最稳；直接抽成 wav 交给它
        audio = Path(cache_dir) / "input.wav" if cache_dir else media.with_suffix(".whisper.wav")
        if cache_dir:
            Path(cache_dir).mkdir(parents=True, exist_ok=True)
        await ffmpeg_utils.extract_audio_track(media, audio, sample_rate=16000)

        if on_progress:
            on_progress(0.05, f"加载识别模型（{self.config.whisper_model}）…")
        model = await _get_model(
            self.config.whisper_model,
            self.config.whisper_device,
            self.config.whisper_compute_type,
        )

        if on_progress:
            on_progress(0.1, "正在识别语音（本地模型，请稍候）…")

        def run() -> list[_Word]:
            segments, _info = model.transcribe(  # type: ignore[attr-defined]
                str(audio),
                language=self.config.language or None,
                word_timestamps=True,
                vad_filter=True,
                beam_size=5,
                condition_on_previous_text=False,
            )
            words: list[_Word] = []
            for segment in segments:
                for word in getattr(segment, "words", None) or []:
                    words.append(_Word(float(word.start), float(word.end), str(word.word)))
            return words

        try:
            words = await asyncio.to_thread(run)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"本地语音识别失败：{exc}") from exc

        if not words:
            raise ProviderError("本地语音识别没有返回任何内容，请确认视频包含可识别的语音")

        if on_progress:
            on_progress(0.9, f"识别完成，正在按时间轴整理 {len(words)} 个词…")

        cues = self._words_to_cues(words)
        if not cues:
            raise ProviderError("语音识别结果无法整理为字幕")
        return reindex(cues)

    # -- 词 → 字幕条目 ---------------------------------------------------------

    def _words_to_cues(self, words: list[_Word]) -> list[Cue]:
        """按词级时间戳合并成字幕条目。

        闭合条件：出现句末标点，或达到字数/时长上限，或遇到明显停顿。
        相比「按字符数比例估算」，这里每条的起止都来自真实识别时间。
        """
        max_chars = max(30, int(self.config.whisper_max_cue_chars))
        max_duration = max(2.0, float(self.config.whisper_max_cue_duration))
        gap_threshold = 0.6  # 词间停顿超过该秒数即视为可断句

        cues: list[Cue] = []
        buffer: list[_Word] = []
        text_length = 0

        def flush() -> None:
            nonlocal buffer, text_length
            if not buffer:
                return
            text = "".join(w.text for w in buffer).strip()
            if text:
                cues.append(Cue(start=buffer[0].start, end=buffer[-1].end, text=text))
            buffer = []
            text_length = 0

        for word in words:
            if buffer:
                gap = word.start - buffer[-1].end
                span = word.end - buffer[0].start
                if gap >= gap_threshold or span > max_duration or text_length + len(word.text) > max_chars:
                    flush()
            buffer.append(word)
            text_length += len(word.text)
            stripped = word.text.strip()
            if _SENTENCE_END.search(stripped):
                flush()
            elif _CLAUSE_END.search(stripped) and text_length >= max_chars * 0.6:
                flush()
        flush()

        # 收尾清理：过短且紧邻的条目合并，避免一闪而过。
        # 关键约束：不能跨越明显停顿——否则会把刚按停顿正确断开的句子又粘回去
        # （实测「Hello / 停顿 2.1s / world」会被重新合成一条）。
        merged: list[Cue] = []
        for cue in cues:
            gap = cue.start - merged[-1].end if merged else 0.0
            # 上一条已经是完整句子时不再粘回去——否则按句末标点断好的条目会被重新合并
            previous_is_sentence = bool(merged) and bool(_SENTENCE_END.search(merged[-1].text.strip()))
            if (
                merged
                and not previous_is_sentence
                and gap < gap_threshold
                and cue.end - cue.start < 1.0
                and cue.end - merged[-1].start <= max_duration
                and len(merged[-1].text) + len(cue.text) + 1 <= max_chars
            ):
                previous = merged[-1]
                previous.text = f"{previous.text} {cue.text}".strip()
                previous.end = cue.end
            else:
                merged.append(cue)
        return merged
