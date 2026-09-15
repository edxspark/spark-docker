"""阿里云智能语音交互 —— 语音识别（ASR）。

用途：视频没有（人工/自动）字幕时，用语音识别把原声转成英文字幕，
再走「翻译 → 配音 → 替换音轨」的常规流程。

接口（RESTful，一次请求一问一答）：
    POST https://nls-gateway-<region>.aliyuncs.com/stream/v1/asr
        ?appkey=<AppKey>&format=pcm&sample_rate=16000
        &enable_punctuation_prediction=true&enable_inverse_text_normalization=true
    Header: X-NLS-Token: <Token>   Content-Type: application/octet-stream
    Body:   单声道 16bit PCM 原始字节

限制：单次音频不超过 60 秒（且体积不宜过大）。长视频的处理策略：
    1. 用 ffmpeg silencedetect 找出语音区间，按静音边界合并成 <= max_chunk_seconds 的块
       —— 按静音切可以避免把单词切一半，且每块起始时间就是真实语音起点；
    2. 逐块送识别，拿到该块的文本；
    3. 块内按字符数比例把句子摊到该块的时间跨度上（接口不返回词级时间戳）。

注意：AppKey 必须来自「项目功能配置」中已选择对应语种识别模型的项目，
否则识别结果为空或直接报错。文档：https://help.aliyun.com/zh/isi/developer-reference/restful-api-2
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import httpx

from app.providers.base import BaseASR, NotConfiguredError, ProviderError
from app.providers.tts.aliyun import AliyunTokenManager
from app.services.settings_store import ASRConfig, TTSConfig
from app.services.subtitles import Cue, reindex
from app.utils import ffmpeg as ffmpeg_utils

logger = logging.getLogger(__name__)

# 切句：中英文句末标点
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？；;])\s*")
# 去掉识别结果里的口语填充词残留（接口的 disfluency 已处理大部分）
_NOISE_RE = re.compile(r"^\s*[\[\(（【].{0,20}[\]\)）】]\s*$")


class AliyunASR(BaseASR):
    name = "aliyun"

    def __init__(self, asr_config: ASRConfig, tts_config: TTSConfig) -> None:
        self.config = asr_config
        # 复用语音合成的项目凭证：同一个阿里云项目下 AppKey 与 Token 是通用的
        self.credentials = tts_config
        if not tts_config.app_key:
            raise NotConfiguredError(
                "语音识别需要阿里云项目 AppKey（与语音合成共用），请在「系统配置 → 语音合成」中填写"
            )

    @property
    def endpoint(self) -> str:
        region = self.credentials.region or "cn-shanghai"
        return f"https://nls-gateway-{region}.aliyuncs.com/stream/v1/asr"

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
        if cache_dir:
            Path(cache_dir).mkdir(parents=True, exist_ok=True)

        duration = total_duration or (await ffmpeg_utils.probe(media)).duration
        limit_minutes = int(self.config.max_duration_minutes or 0)
        if limit_minutes and duration > limit_minutes * 60:
            raise ProviderError(
                f"视频时长 {duration / 60:.0f} 分钟超过语音识别上限 {limit_minutes} 分钟。"
                "可在「系统配置 → 语音识别」中调整上限，或手动提供字幕。"
            )

        if on_progress:
            on_progress(1.0, "分析音频中的语音区间…")
        speech = await ffmpeg_utils.detect_speech_segments(
            media,
            noise_db=self.config.silence_threshold_db,
            min_silence=self.config.min_silence_seconds,
            total_duration=duration,
        )
        chunks = ffmpeg_utils.group_segments(speech, max_duration=float(self.config.max_chunk_seconds))
        if not chunks:
            raise ProviderError("未能从音轨中检测到任何语音内容")

        logger.info("语音识别：%s 秒音频切成 %s 块", round(duration), len(chunks))
        semaphore = asyncio.Semaphore(max(1, self.config.concurrency))
        results: list[tuple[float, float, str] | None] = [None] * len(chunks)
        done = 0

        cache_root = Path(cache_dir) if cache_dir else None
        cached_count = 0

        async def worker(index: int, start: float, end: float) -> None:
            nonlocal done, cached_count
            async with semaphore:
                cached = self._read_cache(cache_root, index, start, end)
                if cached is not None:
                    text = cached
                    cached_count += 1
                else:
                    text = await self._transcribe_chunk(media, start, end)
                    self._write_cache(cache_root, index, start, end, text)
                results[index] = (start, end, text)
                done += 1
                if on_progress:
                    mark = "（缓存）" if cached is not None else ""
                    on_progress(
                        done / len(chunks),
                        f"语音识别 {done}/{len(chunks)} 段{mark}：{text[:20] or '未识别到内容'}",
                    )

        await asyncio.gather(*(worker(i, s, e) for i, (s, e) in enumerate(chunks)))

        cues: list[Cue] = []
        for item in results:
            if not item:
                continue
            start, end, text = item
            cues.extend(self._distribute(text, start, end))
        if not cues:
            raise ProviderError(
                "语音识别没有返回任何文本。请确认阿里云该项目的识别模型语种与音频一致"
                "（英文视频需要在控制台选择英文模型）"
            )
        if cached_count:
            logger.info("语音识别复用了 %s/%s 块缓存", cached_count, len(chunks))
        return reindex(cues)

    # -- 分块结果缓存 ----------------------------------------------------------

    @staticmethod
    def _cache_path(cache_root: Path | None, index: int, start: float, end: float) -> Path | None:
        if cache_root is None:
            return None
        return cache_root / f"{index:05d}_{start:.2f}_{end:.2f}.txt"

    @classmethod
    def _read_cache(cls, cache_root: Path | None, index: int, start: float, end: float) -> str | None:
        path = cls._cache_path(cache_root, index, start, end)
        if path is None or not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    @classmethod
    def _write_cache(cls, cache_root: Path | None, index: int, start: float, end: float, text: str) -> None:
        path = cls._cache_path(cache_root, index, start, end)
        if path is None or not text:
            # 空结果不缓存：可能是瞬时故障，重试时值得再试一次
            return
        try:
            # 自行确保目录存在，不依赖调用方先建目录
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            # 缓存只是优化手段，写不进去不应影响识别主流程
            logger.warning("语音识别缓存写入失败：%s", exc)

    # -- 单块识别 --------------------------------------------------------------

    async def _transcribe_chunk(self, media: Path, start: float, end: float) -> str:
        """识别一块音频。

        重试策略按状态码分流，避免「重试后拿到空结果」把真实错误掩盖掉
        （曾出现：鉴权失败 -> 重试 -> 返回空文本 -> 用户看到的是「未识别到内容」）。
        """
        pcm = await ffmpeg_utils.extract_pcm(media, start=start, duration=end - start, sample_rate=16000)
        token = await AliyunTokenManager.get(self.credentials)
        params = {
            "appkey": self.credentials.app_key,
            "format": "pcm",
            "sample_rate": "16000",
            "enable_punctuation_prediction": "true",
            "enable_inverse_text_normalization": "true",
            # 去掉「嗯」「呃」这类填充词，译文更干净
            "disfluency": "true" if self.config.remove_fillers else "false",
        }
        last_error: str = ""

        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    response = await client.post(
                        self.endpoint,
                        params=params,
                        headers={
                            "X-NLS-Token": token,
                            "Content-Type": "application/octet-stream",
                        },
                        content=pcm,
                    )
                data = response.json()
            except Exception as exc:  # noqa: BLE001 - 网络类异常可以重试
                last_error = f"请求失败：{exc}"
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise ProviderError(f"语音识别{last_error}") from exc

            status = data.get("status")
            message = str(data.get("message") or response.text[:200])

            if status == 20000000:
                return str(data.get("result") or "").strip()

            # Token 过期/无效：清缓存换新 Token，且只重试一次
            if status == 40000001:
                if attempt == 0:
                    AliyunTokenManager._cache.clear()
                    token = await AliyunTokenManager.get(self.credentials)
                    last_error = f"鉴权失败：{message}"
                    continue
                raise ProviderError(
                    f"语音识别鉴权失败（40000001）：{message}。"
                    "请检查「系统配置 → 语音合成」中的 AccessKey / 手工 Token 是否有效"
                )

            # 服务端繁忙 / 内部错误：可重试
            if status in (40000005, 50000000, 50000001):
                last_error = f"服务端繁忙（{status}）：{message}"
                if attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise ProviderError(f"语音识别{last_error}")

            # 其余为确定性错误，立即抛出，不做无意义重试
            raise ProviderError(f"语音识别失败（{status}）：{message}")

        raise ProviderError(f"语音识别失败：{last_error or '未知错误'}")

    # -- 时间轴分配 ------------------------------------------------------------

    def _distribute(self, text: str, start: float, end: float) -> list[Cue]:
        """把一块文本按字符数比例摊到 [start, end] 上。

        识别接口不返回词级时间戳，块的起止来自 silencedetect，因此每块的时间跨度是真实的；
        块内按长度比例分配即可把误差控制在块内（块长本身被限制在几十秒）。
        """
        text = (text or "").strip()
        if not text:
            return []
        span = max(0.4, end - start)
        sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
        sentences = [s for s in sentences if not _NOISE_RE.match(s)]
        if not sentences:
            return []

        total_chars = sum(len(s) for s in sentences) or 1
        cues: list[Cue] = []
        cursor = start
        for index, sentence in enumerate(sentences):
            if index == len(sentences) - 1:
                cue_end = end
            else:
                share = span * (len(sentence) / total_chars)
                cue_end = cursor + share
            if cue_end - cursor < 0.3:
                cue_end = cursor + 0.3
            cues.append(Cue(start=round(cursor, 3), end=round(min(cue_end, end), 3), text=sentence))
            cursor = cue_end
        return cues


class MockASR(BaseASR):
    """离线占位：不调用任何外部服务，按静音段生成等长的占位英文句子。

    目的是让「无字幕 → ASR → 翻译 → 配音」这条链路在离线环境也能被测试与演示。
    """

    name = "mock"

    def __init__(self, asr_config: ASRConfig | None = None, **_: object) -> None:
        self.config = asr_config

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
        duration = total_duration or (await ffmpeg_utils.probe(media)).duration
        if duration <= 0:
            raise ProviderError("无法获取媒体时长")

        chunks = ffmpeg_utils.group_segments(
            await ffmpeg_utils.detect_speech_segments(media, total_duration=duration),
            max_duration=float((self.config.max_chunk_seconds if self.config else 40) or 40),
        )
        if not chunks:
            chunks = [(0.0, duration)]

        # 每 6 秒生成一句占位文本，便于验证后续翻译/配音/对轴
        cues: list[Cue] = []
        index = 0
        for start, end in chunks:
            cursor = start
            while cursor < end - 0.2:
                piece_end = min(end, cursor + 6.0)
                index += 1
                cues.append(
                    Cue(
                        start=round(cursor, 3),
                        end=round(piece_end, 3),
                        text=f"This is a mock transcription sentence number {index}.",
                    )
                )
                cursor = piece_end
                if on_progress:
                    on_progress(min(1.0, cursor / max(duration, 0.1)), f"[Mock] 转写 {cursor:.0f}s / {duration:.0f}s")
        if not cues:
            raise ProviderError("Mock 语音识别未生成任何内容")
        return reindex(cues)
