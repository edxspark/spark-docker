"""阿里云智能语音交互（ISI）语音合成提供者。

实现两条链路：
1. Token 签发：通过 POP RPC 接口 CreateToken（HMAC-SHA1 签名），24 小时有效，自动缓存续期。
   也可在配置里直接填手工 Token，便于没有 AK/SK 的场景。
2. 语音合成：POST https://nls-gateway-<region>.aliyuncs.com/stream/v1/tts
   单请求文本上限 300 字符，超长文本按标点切分后逐段合成再用 ffmpeg 拼接。

文档：https://help.aliyun.com/zh/isi/developer-reference/restful-api-3
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

from app.providers.base import BaseTTS, NotConfiguredError, ProviderError, SynthesisResult
from app.services.settings_store import TTSConfig
from app.utils import ffmpeg as ffmpeg_utils
from app.utils.text import split_for_tts

logger = logging.getLogger(__name__)

TOKEN_ENDPOINT = "https://nls-meta.cn-shanghai.aliyuncs.com/"
TOKEN_API_VERSION = "2019-02-28"


def _percent_encode(value: str) -> str:
    """阿里云 RPC 签名要求的 RFC3986 编码。"""
    return quote(str(value), safe="~").replace("+", "%20").replace("*", "%2A").replace("%7E", "~")


@dataclass
class _Token:
    value: str
    expire_at: float  # unix 秒

    @property
    def valid(self) -> bool:
        # 提前 5 分钟失效，避免边界过期
        return self.value and time.time() < self.expire_at - 300


class AliyunTokenManager:
    """进程内 Token 缓存；多实例部署时可换成 Redis。"""

    _cache: dict[str, _Token] = {}
    _lock = asyncio.Lock()

    @classmethod
    async def get(cls, config: TTSConfig) -> str:
        # 手工 Token 优先
        if config.token:
            return config.token
        if not config.access_key_id or not config.access_key_secret:
            raise NotConfiguredError(
                "未配置阿里云语音合成凭证：请在「系统配置 → 语音合成」填写 AccessKey ID/Secret 与项目 AppKey，"
                "或直接填写手工 Token"
            )
        key = f"{config.access_key_id}:{config.access_key_secret[:6]}:{config.region}"
        async with cls._lock:
            cached = cls._cache.get(key)
            if cached and cached.valid:
                return cached.value
            token = await cls._create(config)
            cls._cache[key] = token
            logger.info("阿里云语音 Token 已刷新，有效期至 %s", datetime.fromtimestamp(token.expire_at, timezone.utc))
            return token.value

    @classmethod
    async def _create(cls, config: TTSConfig) -> _Token:
        params = {
            "AccessKeyId": config.access_key_id,
            "Action": "CreateToken",
            "Format": "JSON",
            "RegionId": config.region,
            "SignatureMethod": "HMAC-SHA1",
            "SignatureNonce": uuid.uuid4().hex,
            "SignatureVersion": "1.0",
            "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "Version": TOKEN_API_VERSION,
        }
        canonicalized = "&".join(
            f"{_percent_encode(k)}={_percent_encode(v)}" for k, v in sorted(params.items())
        )
        string_to_sign = f"GET&{_percent_encode('/')}&{_percent_encode(canonicalized)}"
        signature = base64.b64encode(
            hmac.new(
                f"{config.access_key_secret}&".encode(),
                string_to_sign.encode(),
                hashlib.sha1,
            ).digest()
        ).decode()

        query = canonicalized + f"&Signature={_percent_encode(signature)}"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(f"{TOKEN_ENDPOINT}?{query}")
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"阿里云 Token 签发请求失败：{exc}") from exc

        token_data = data.get("Token") or {}
        if response.status_code != 200 or not token_data.get("Id"):
            message = data.get("Message") or data.get("Code") or response.text[:300]
            raise ProviderError(f"阿里云 Token 签发失败：{message}")

        return _Token(value=str(token_data["Id"]), expire_at=float(token_data.get("ExpireTime") or time.time() + 3600))


class AliyunTTS(BaseTTS):
    name = "aliyun"

    def __init__(self, config: TTSConfig) -> None:
        self.config = config
        # 按性别选定的发音人（任务级；空表示未启用性别匹配）
        self.gender_voice = ""
        if not config.app_key:
            raise NotConfiguredError("未配置阿里云项目 AppKey，请在「系统配置 → 语音合成」中填写")

    @property
    def endpoint(self) -> str:
        return f"https://nls-gateway-{self.config.region}.aliyuncs.com/stream/v1/tts"

    async def prepare_gender(self, gender: str) -> str:
        """按性别切换发音人：男声用配置/清单里的男声，女声同理。

        ALIYUN_VOICES 里每个发音人都带 gender 标注，因此这里不需要额外探测。
        """
        if gender not in {"male", "female"}:
            return ""
        configured = (
            self.config.voice_male if gender == "male" else self.config.voice_female
        ).strip()
        chosen = configured
        if not chosen:
            for item in ALIYUN_VOICES:
                if item.get("gender") == gender:
                    chosen = str(item["id"])
                    break
        if not chosen:
            return f"内置发音人清单里没有{gender}声，沿用当前发音人 {self.config.voice}"
        self.gender_voice = chosen
        label = "男声" if gender == "male" else "女声"
        name = next((v["name"] for v in ALIYUN_VOICES if v["id"] == chosen), chosen)
        return f"按性别选音色：{label} → 发音人 {name}（{chosen}）"

    async def synthesize(self, text: str, out_path: Path, *, voice: str | None = None) -> SynthesisResult:
        # 优先级：调用方指定 > 按性别选定 > 配置默认
        chosen_voice = voice or self.gender_voice or self.config.voice
        text = (text or "").strip()
        if not text:
            raise ProviderError("待合成文本为空")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        chunks = split_for_tts(text, self.config.max_chars_per_request)
        if not chunks:
            raise ProviderError("待合成文本为空")

        if len(chunks) == 1:
            await self._request(chunks[0], chosen_voice, out_path)
        else:
            # 超长文本：逐段合成到临时文件后拼接
            temp_dir = out_path.parent / f".{out_path.stem}_parts"
            temp_dir.mkdir(parents=True, exist_ok=True)
            parts: list[Path] = []
            try:
                for i, chunk in enumerate(chunks):
                    part = temp_dir / f"part_{i:03d}{out_path.suffix}"
                    await self._request(chunk, chosen_voice, part)
                    parts.append(part)
                await ffmpeg_utils.concat_audio(parts, out_path)
            finally:
                for part in parts:
                    part.unlink(missing_ok=True)
                if temp_dir.exists() and not any(temp_dir.iterdir()):
                    temp_dir.rmdir()

        duration = await ffmpeg_utils.audio_duration(out_path)
        return SynthesisResult(path=out_path, duration=duration, characters=len(text))

    async def _request(self, text: str, voice: str, out_path: Path) -> None:
        token = await AliyunTokenManager.get(self.config)
        payload = {
            "appkey": self.config.app_key,
            "token": token,
            "text": text,
            "format": self.config.format,
            "sample_rate": self.config.sample_rate,
            "voice": voice,
            "volume": self.config.volume,
            "speech_rate": self.config.speech_rate,
            "pitch_rate": self.config.pitch_rate,
        }
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    response = await client.post(
                        self.endpoint,
                        headers={"Content-Type": "application/json"},
                        json=payload,
                    )
                content_type = (response.headers.get("Content-Type") or "").lower()
                if content_type.startswith("audio/"):
                    out_path.write_bytes(response.content)
                    return
                # 失败时返回 JSON 错误体
                try:
                    detail = response.json()
                    message = detail.get("Message") or detail.get("message") or response.text[:300]
                    status = detail.get("Status") or detail.get("status")
                except Exception:  # noqa: BLE001
                    message, status = response.text[:300], response.status_code
                # Token 过期 / 无效：清缓存后重试
                if str(status) in {"40000001", "40000002", "40000003"} or response.status_code == 401:
                    AliyunTokenManager._cache.clear()
                    raise ProviderError(f"阿里云语音合成鉴权失败（{status}）：{message}")
                raise ProviderError(f"阿里云语音合成失败（{status}）：{message}")
            except ProviderError as exc:
                last_error = exc
                if "鉴权失败" in str(exc) and attempt < 2:
                    await asyncio.sleep(1)
                    continue
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                await asyncio.sleep(1.5 * (attempt + 1))
        raise ProviderError(f"阿里云语音合成失败：{last_error}")

    async def list_voices(self) -> list[dict[str, str]]:
        """返回当前常用发音人（阿里云发音人列表随控制台开通情况变化）。"""
        return ALIYUN_VOICES


class MockTTS(BaseTTS):
    """离线演示：生成与文本长度成正比的静音/提示音轨，不调用任何外部服务。"""

    name = "mock"

    def __init__(self, config: TTSConfig | None = None) -> None:
        self.config = config

    async def synthesize(self, text: str, out_path: Path, *, voice: str | None = None) -> SynthesisResult:
        text = (text or "").strip()
        if not text:
            raise ProviderError("待合成文本为空")
        # 中文语速约 4.5 字/秒，用它估算时长，保证后续对轴逻辑可被真实验证
        duration = max(0.6, len(text) / 4.5)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        sample_rate = (self.config.sample_rate if self.config else 48000) or 48000
        await ffmpeg_utils.make_silence(duration, out_path, sample_rate=sample_rate)
        await asyncio.sleep(0.02)
        return SynthesisResult(path=out_path, duration=duration, characters=len(text), cached=False)


# 阿里云 ISI 常用发音人（按时长/音色计费，具体以控制台开通为准）
ALIYUN_VOICES: list[dict[str, str]] = [
    {"id": "xiaoxian", "name": "小仙 · 温柔女声", "gender": "female", "scene": "通用/解说"},
    {"id": "xiaoyun", "name": "小云 · 标准女声", "gender": "female", "scene": "通用"},
    {"id": "xiaogang", "name": "小刚 · 沉稳男声", "gender": "male", "scene": "解说/科普"},
    {"id": "ruoxi", "name": "若兮 · 亲和女声", "gender": "female", "scene": "短视频"},
    {"id": "sitong", "name": "思彤 · 活泼女声", "gender": "female", "scene": "带货/娱乐"},
    {"id": "sijia", "name": "思佳 · 知性女声", "gender": "female", "scene": "知识科普"},
    {"id": "aixia", "name": "艾夏 · 元气女声", "gender": "female", "scene": "短视频"},
    {"id": "aimei", "name": "艾美 · 甜美女声", "gender": "female", "scene": "娱乐"},
    {"id": "aijia", "name": "艾佳 · 方言女声", "gender": "female", "scene": "方言"},
    {"id": "zhiqi", "name": "知琪 · 温柔女声", "gender": "female", "scene": "情感"},
    {"id": "zhigui", "name": "知柜 · 沉稳男声", "gender": "male", "scene": "解说"},
    {"id": "zhimiao", "name": "知妙 · 童声", "gender": "female", "scene": "儿童"},
]
