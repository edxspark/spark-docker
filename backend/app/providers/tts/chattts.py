"""ChatTTS 语音合成提供者（本地/内网部署）。

ChatTTS 是对话式中文语音模型，听感比传统 TTS 自然得多，但它是**本地服务**：
需要先在机器上跑一个 ChatTTS HTTP 服务，本提供者只负责按它的 HTTP 接口合成音频，
不负责启动模型。

接口契约以 ChatTTS 官方仓库的 `examples/web/webui/app.py` 为准（已核对源码）：

    POST {base_url}{api_path}          # 默认 /tts
        text          待合成文本
        prompt        风格提示词（如 [oral_2][laugh_0]）
        custom_voice  说话人编号，>0 生效（0 表示不用）
        voice         说话人种子（custom_voice<=0 时用它；服务端默认 "2222"）
        temperature / top_p / top_k
        speed         语速档位 1~9（服务端拼成 [speed_N] 提示词，默认 5）
        skip_refine   1 跳过文本润色
    响应：wav 音频二进制（失败时返回 JSON 错误）

社区里的 ChatTTS-ui 等封装用的是 /api/say + voice 当编号，因此这里做了兼容：
路径可配置，并在 /tts 404 时自动回退到 /api/say；回退时会同时带上
custom_voice / seed / voice 三种命名，让不同封装的字段名都能命中。

音色一致性是这里最容易踩的坑：流水线是**逐条字幕**调用 synthesize 的，若每次随机
抽音色，同一支视频会出现几十种声音。因此固定用配置里的 voice_seed 作为说话人种子
（上游做法即 `torch.manual_seed(voice)`），同一配置下每条字幕音色相同。

失败时给出可操作的诊断（服务没启动、路径不对、返回 JSON 报错等），
因为「本地服务没起来」是最常见的情况。

长文本的两处改造（参考 ChatTTS-LongAudio 的做法）
-------------------------------------------------
1. **文本归一化**：中文里混着的英文缩写、百分号、单位符号、中文冒号，
   ChatTTS 经常读错甚至吞字（「AI创业」读成「哎创业」、「15.5%」读成「十五点五」）。
   合成前先按规则表归一（见 services/tts_text.py），**只影响送进模型的文本**，
   字幕与标题保持原样。
2. **分片合成**：ChatTTS 单次推理的 token 上限有限，长句一次性合成容易在结尾
   出现杂音/含糊。超过 `tts_chunk_chars`（默认 80 字）就切成多片分别合成，
   每片先裁掉首尾静音再拼接——否则片与片之间会凭空多出近 1 秒死寂。

另外兼容了「返回 JSON + 音频 URL」的服务端（LongAudio 的 /tts 就是这么返回的）：
先把 URL 里的音频下载回来再落盘。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
from pathlib import Path

import httpx

from app.providers.base import BaseTTS, ProviderError, SynthesisResult
from app.services import tts_text
from app.services.settings_store import TTSConfig
from app.utils import ffmpeg as ffmpeg_utils
from app.utils.text import split_for_tts

logger = logging.getLogger(__name__)

# 响应体里可能的 base64 音频字段（不同 ChatTTS 封装命名不一）
_AUDIO_KEYS = ("audio", "data", "audio_base64", "base64", "wav", "mp3", "file", "output")
# 判定「这确实是音频」的文件头
_AUDIO_MAGIC = (
    (b"RIFF", "wav"),
    (b"ID3", "mp3"),
    (b"\xff\xfb", "mp3"),
    (b"\xff\xf3", "mp3"),
    (b"OggS", "ogg"),
    (b"fLaC", "flac"),
)
# 提示词里已自带速度标记时不再重复追加
_SPEED_TOKEN = re.compile(r"\[speed_\d+\]")


def _looks_like_audio(blob: bytes) -> bool:
    return any(blob.startswith(magic) for magic, _ in _AUDIO_MAGIC)


def _suffix_for(blob: bytes) -> str:
    """按文件头推断真实容器后缀，供「扩展名与实际内容不符时转码」使用。"""
    for magic, kind in _AUDIO_MAGIC:
        if blob.startswith(magic):
            return f".{kind}"
    return ""


class ChatTTS(BaseTTS):
    """通过本地 ChatTTS HTTP 服务合成语音。"""

    name = "chattts"

    def __init__(self, config: TTSConfig) -> None:
        self.config = config
        self.base_url = (config.chattts_base_url or "http://127.0.0.1:9966").rstrip("/")
        path = (config.chattts_api_path or "/tts").strip()
        self.api_path = path if path.startswith("/") else "/" + path
        self.endpoint = f"{self.base_url}{self.api_path}"
        # 另一套常见封装用的路径，作为 404 时的回退候选
        self.fallback_path = "/api/say" if self.api_path != "/api/say" else "/tts"
        # 单次调用的说话人覆盖（性别探测用）
        self.speaker_override = 0
        # 按性别选定的说话人（任务级：由性别探测结果决定，全片复用）
        self.gender_speaker = 0
        self.gender_pick_note = ""
        self._catalog = None  # 惰性加载的性别音色库

    def catalog_file(self) -> Path:
        """性别音色库缓存位置：数据目录下（换机器重新探测，同机复用）。"""
        from app.core.config import settings

        return settings.data_dir / "logs" / "chattts_voice_catalog.json"

    def _catalog_path(self) -> Path:
        return self.catalog_file()

    async def prepare_gender(self, gender: str) -> str:
        """按目标性别选定音色（任务开始时调用一次，之后全片复用）。

        返回一句人类可读的说明，用于写进任务日志；选不到时说明原因。
        """
        from app.services import voice_catalog

        if gender not in {"male", "female"}:
            return ""

        catalog = self._catalog or voice_catalog.load_cache(self._catalog_path())
        if catalog is None:
            # 缓存没有：实测各个音色的性别并落盘（约 10~40 秒）
            catalog = await voice_catalog.probe_chattts(
                self,
                self._catalog_path().parent / "chattts_probe",
                pool_size=int(self.config.gender_pool_size or 10),
            )
            if catalog.speakers:
                voice_catalog.save_cache(self._catalog_path(), catalog)
        self._catalog = catalog

        picked = catalog.pick(gender, seed=int(self.config.voice_seed or 0))
        counts = catalog.counts()
        if picked is None:
            self.gender_pick_note = (
                f"音色库里没有可用的{('男' if gender == 'male' else '女')}声"
                f"（已标注 {counts or '无'}），本次沿用原有音色"
            )
            return self.gender_pick_note

        self.gender_speaker = picked.seed
        self.gender_pick_note = (
            f"按性别选音色：{('男' if gender == 'male' else '女')}声 → 说话人种子 {picked.seed}"
            f"（实测基频 {picked.f0:.0f} Hz，置信 {picked.confidence:.2f}；"
            f"音色库男女分布 {counts.get('male', 0)}/{counts.get('female', 0)}）"
        )
        return self.gender_pick_note

    # ------------------------------------------------------------------ 对外接口

    async def synthesize(self, text: str, out_path: Path, *, voice: str | None = None) -> SynthesisResult:
        text = (text or "").strip()
        if not text:
            raise ProviderError("待合成文本为空")

        # voice 传数字时视为该次合成的说话人种子（ChatTTS 上游语义）：
        # 性别探测与「按性别选音色」都依赖这个能力。
        override = self._parse_speaker(voice)
        previous = self.speaker_override
        if override > 0:
            self.speaker_override = override
        try:
            return await self._synthesize_inner(text, out_path)
        finally:
            self.speaker_override = previous

    def _prepare_text(self, text: str) -> str:
        """合成前的文本处理：归一化（可关）→ 分片（由调用方按片长切）。

        归一化只作用于「送给模型的文本」，不影响字幕内容。
        """
        if not bool(getattr(self.config, "normalize_text", True)):
            return text
        rules = tts_text.parse_term_rules(getattr(self.config, "term_rules", "") or "")
        # 默认规则 + 用户自定义规则（自定义优先，长词优先）
        merged = [*rules, *tts_text.DEFAULT_TERM_RULES]
        merged.sort(key=lambda item: len(item[0]), reverse=True)
        normalized = tts_text.normalize_for_tts(text, term_rules=merged)
        if normalized and normalized != text:
            logger.debug("ChatTTS 文本归一：%r → %r", text[:40], normalized[:40])
        return normalized or text

    def chunk_limit(self) -> int:
        """单次请求的字数上限。

        ChatTTS 用 `tts_chunk_chars`（默认 80）：长句一次推理容易在结尾糊掉。
        其它封装（如只做整段推理的服务端）用公共的 `max_chars_per_request`。
        """
        return int(getattr(self.config, "tts_chunk_chars", 0) or self.config.max_chars_per_request)

    async def _synthesize_inner(self, text: str, out_path: Path) -> SynthesisResult:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        prepared = self._prepare_text(text)
        chunks = split_for_tts(prepared, self.chunk_limit())
        if not chunks:
            raise ProviderError("待合成文本为空")

        if len(chunks) == 1:
            await self._request(chunks[0], out_path)
        else:
            # 超长文本：分段合成后拼接。
            #
            # 关键：每片拼接前必须先裁掉首尾静音。ChatTTS 每次生成都会在句首留
            # 0~0.9 秒空白、句尾也常带一截，两片直接拼接就会在句子中间凭空多出
            # 近 1 秒死寂——这正是长文本听起来「断断续续」的原因。
            # （句尾那截由 fit_segment 兜底裁，但拼完就再也分不出边界了，
            #   所以必须在拼接之前逐片处理。）
            temp_dir = out_path.parent / f".{out_path.stem}_parts"
            temp_dir.mkdir(parents=True, exist_ok=True)
            raw_parts: list[Path] = []
            parts: list[Path] = []
            try:
                for i, chunk in enumerate(chunks):
                    raw = temp_dir / f"raw_{i:03d}.wav"
                    await self._request(chunk, raw)
                    raw_parts.append(raw)
                    clean = temp_dir / f"part_{i:03d}.mp3"
                    await ffmpeg_utils.trim_silence(raw, clean)
                    parts.append(clean)
                await ffmpeg_utils.concat_audio(parts, out_path)
            finally:
                for part in (*raw_parts, *parts):
                    part.unlink(missing_ok=True)
                if temp_dir.exists() and not any(temp_dir.iterdir()):
                    temp_dir.rmdir()

        duration = await ffmpeg_utils.audio_duration(out_path)
        return SynthesisResult(path=out_path, duration=duration, characters=len(text))

    async def describe(self) -> str:
        """供「系统配置 → 语音合成 → 测试」展示：探活 + 当前音色/参数。"""
        reachable, note = await self.probe()
        speaker = self._speaker_id()
        detail = (
            f"地址 {self.endpoint}；"
            f"说话人 {'种子 ' + str(speaker) + '（固定，全片同音色）' if speaker else '由服务端默认'}；"
            f"speed={self.config.speed} temperature={self.config.temperature} "
            f"top_p={self.config.top_p} top_k={self.config.top_k}；"
            f"单次上限 {self.chunk_limit()} 字"
            f"（{'已开启' if bool(getattr(self.config, 'normalize_text', True)) else '未开启'}文本归一化）"
        )
        return f"{'服务可达' if reachable else '服务不可达'}（{note}）；{detail}"

    # ------------------------------------------------------------------ 内部实现

    @staticmethod
    def _parse_speaker(value: str | None) -> int:
        try:
            return max(0, int(str(value).strip()))
        except (TypeError, ValueError):
            return 0

    def _speaker_id(self) -> int:
        """返回 >0 的说话人编号；0 表示交给服务端默认音色。

        优先级：单次调用覆盖 > 按性别选出的音色 > 配置里的显式音色/种子。
        """
        if self.speaker_override > 0:
            return self.speaker_override
        if self.gender_speaker > 0:
            return self.gender_speaker
        explicit = (self.config.voice or "").strip()
        if explicit and explicit.lower() not in {"random", "auto", "-1"}:
            try:
                value = int(explicit)
                if value > 0:
                    return value
            except ValueError:
                # 老配置沿用了阿里云的发音人名（如 xiaoxian），此处忽略
                logger.info("ChatTTS 忽略非数字音色配置：%s（改用默认种子）", explicit)
        return int(self.config.voice_seed or 0)

    def _prompt(self) -> str:
        """风格提示词 + 语速档位（上游用 [speed_N] 控制语速）。"""
        prompt = (self.config.chattts_prompt or "").strip()
        if _SPEED_TOKEN.search(prompt):
            return prompt
        token = f"[speed_{int(self.config.speed)}]"
        return f"{prompt}{token}" if prompt else token

    def _payload(self, text: str) -> dict[str, object]:
        speaker = self._speaker_id()
        payload: dict[str, object] = {
            "text": text,
            # custom_voice 与 voice 同时下发：上游用 custom_voice（>0 生效），
            # 部分封装读取 voice，两者填同一个值可兼容
            "custom_voice": speaker,
            "voice": str(speaker) if speaker > 0 else str(self.config.voice_seed or 0),
            "prompt": self._prompt(),
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
            "top_k": self.config.top_k,
            "speed": int(self.config.speed),
            # 字幕本身已是成品文案，跳过润色可显著提速
            "skip_refine": 1,
        }
        return payload

    async def probe(self) -> tuple[bool, str]:
        """探活：不合成，只确认服务在监听。"""
        try:
            async with httpx.AsyncClient(timeout=min(10.0, self.config.timeout)) as client:
                response = await client.get(self.base_url, follow_redirects=True)
            return True, f"HTTP {response.status_code}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    async def _post(self, path: str, payload: dict[str, object]) -> httpx.Response:
        async with httpx.AsyncClient(timeout=self.config.timeout) as client:
            return await client.post(
                f"{self.base_url}{path}",
                data=payload,
                headers={"Accept": "audio/*, application/json"},
            )

    async def _request(self, text: str, out_path: Path) -> None:
        payload = self._payload(text)
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await self._post_with_fallback(payload)
                if response.status_code >= 400:
                    raise ProviderError(self._http_error(response))
                await self._save(response, out_path)
                return
            except ProviderError:
                raise
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                raise ProviderError(
                    f"连不上 ChatTTS 服务（{self.endpoint}）。请先在本机启动 ChatTTS 服务"
                    f"（官方 webui 默认 http://127.0.0.1:9966/tts），"
                    f"或在「系统配置 → 语音合成」里改地址。原始错误：{type(exc).__name__}: {exc}"
                ) from exc
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(1 + attempt)
                    continue
        raise ProviderError(f"ChatTTS 合成失败：{last_error}")

    @property
    def _candidate_paths(self) -> list[str]:
        """优先用户配置的路径，其次社区封装常用的 /api/say（去重）。"""
        paths = [self.api_path]
        if self.fallback_path and self.fallback_path not in paths:
            paths.append(self.fallback_path)
        return paths

    async def _post_with_fallback(self, payload: dict[str, object]) -> httpx.Response:
        """按候选路径依次尝试；只有 404（路径不存在）才换下一个。

        其它状态码说明路径是对的、是服务端自己的问题（如 500），
        再换路径只会掩盖真实错误，因此直接返回让上层报出来。
        """
        paths = self._candidate_paths
        response: httpx.Response | None = None
        for index, path in enumerate(paths):
            response = await self._post(path, payload)
            if response.status_code != 404 or index == len(paths) - 1:
                return response
            logger.info("ChatTTS %s 返回 404，改用 %s 重试", path, paths[index + 1])
        assert response is not None
        return response

    def _http_error(self, response: httpx.Response) -> str:
        snippet = response.text[:200] or "无响应体"
        if response.status_code == 404:
            tried = "、".join(self._candidate_paths)
            return (
                f"ChatTTS 接口路径不存在（已依次尝试 {tried}，都是 404）。"
                f"请在「系统配置 → 语音合成 → 合成接口路径」里填你那份服务的真实路径"
                f"（官方 webui 为 /tts，ChatTTS-ui 为 /api/say）。原始响应：{snippet}"
            )
        return f"ChatTTS 返回 {response.status_code}：{snippet}"

    async def _download_audio_url(self, url: str) -> bytes | None:
        """从服务端返回的 URL 取回音频（LongAudio 的 /tts 返回 JSON + url）。

        相对路径按 base_url 补全；下载失败返回 None，由调用方给出可读错误。
        """
        target = url.strip()
        if not target:
            return None
        if target.startswith("/"):
            target = f"{self.base_url}{target}"
        elif not target.startswith(("http://", "https://")):
            target = f"{self.base_url}/{target.lstrip('./')}"
        try:
            async with httpx.AsyncClient(timeout=self.config.timeout, follow_redirects=True) as client:
                blob = await client.get(target)
            if blob.status_code >= 400:
                logger.warning("下载 ChatTTS 返回的音频失败：HTTP %s（%s）", blob.status_code, target)
                return None
            return blob.content
        except Exception as exc:  # noqa: BLE001 - 取不回音频时走原有报错路径
            logger.warning("下载 ChatTTS 返回的音频出错：%s（%s）", exc, target)
            return None

    @staticmethod
    def _extract_payload_error(body: bytes) -> str | None:
        """识别 {"code": 1, "msg": "..."} 这类业务错误。"""
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        code = data.get("code")
        if code in (None, 0, "0"):
            return None
        return str(data.get("msg") or data.get("message") or f"服务端返回 code={code}")

    async def _save(self, response: httpx.Response, out_path: Path) -> None:
        """把响应落盘：既支持直接返回音频，也支持 JSON 里带 base64。

        不能只看 Content-Type：有的封装用 application/octet-stream 返回 wav，
        所以优先按「文件头是否像音频」判断。
        """
        content_type = (response.headers.get("Content-Type") or "").lower()
        body = response.content

        if body and _looks_like_audio(body):
            await self._write_audio(body, out_path, hinted_suffix=_suffix_for(body))
            return

        if "json" in content_type or body[:1] in (b"{", b"["):
            # 业务错误优先：{"code": 1, "msg": "..."} 比「不是音频」有信息量得多
            payload_error = self._extract_payload_error(body)
            if payload_error:
                raise ProviderError(f"ChatTTS 服务端返回错误：{payload_error}")

            # 有的服务端（如 ChatTTS-LongAudio）返回 JSON + 音频 URL，需要再取一次
            for url in self._extract_urls_from_json(body):
                blob = await self._download_audio_url(url)
                if blob and _looks_like_audio(blob):
                    await self._write_audio(blob, out_path, hinted_suffix=_suffix_for(blob))
                    return

            blob = self._extract_audio_from_json(body)
            if blob is not None:
                await self._write_audio(blob, out_path, hinted_suffix=_suffix_for(blob))
                return
            snippet = body[:300].decode("utf-8", "replace")
            raise ProviderError(
                f"ChatTTS 返回的不是音频（Content-Type={content_type or '空'}）：{snippet}。"
                "常见原因是模型尚未加载完成、或接口路径/参数名与该封装不一致"
            )

        if not body:
            raise ProviderError("ChatTTS 返回了空响应体")

        # 兜底：先落盘再让 ffmpeg 判定，能给出可读错误
        out_path.write_bytes(body)
        try:
            await ffmpeg_utils.audio_duration(out_path)
        except Exception as exc:  # noqa: BLE001
            out_path.unlink(missing_ok=True)
            raise ProviderError(
                f"ChatTTS 返回的内容不是可解析的音频（Content-Type={content_type or '空'}）：{exc}"
            ) from exc

    @staticmethod
    async def _write_audio(body: bytes, out_path: Path, *, hinted_suffix: str) -> None:
        """按调用方要求的容器落盘，容器不符时转码。

        ChatTTS 官方 webui 固定返回 24kHz 单声道 wav，而调用方给的路径是 .mp3
        （分段缓存就固定叫 seg_0000.mp3）。直接把 wav 字节写进 .mp3 虽然能被
        ffmpeg 按内容嗅探认出来，但会让扩展名说谎：外部工具、浏览器预览、
        concat demuxer 的按扩展名分流都会踩坑，wav 体积还比 mp3 大三倍。
        所以这里显式转成调用方要的容器。
        """
        want = (out_path.suffix or "").lower()
        if not want or want == hinted_suffix:
            out_path.write_bytes(body)
            return

        tmp = out_path.with_name(f"{out_path.stem}.src{hinted_suffix}")
        tmp.write_bytes(body)
        try:
            await ffmpeg_utils.run_ffmpeg([
                "-i", str(tmp),
                *ffmpeg_utils.audio_codec_args(out_path),
                str(out_path),
            ])
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _extract_urls_from_json(body: bytes) -> list[str]:
        """取出响应里可能的音频 URL（LongAudio：{"audio_files":[{"url": ...}]}）。"""
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return []

        urls: list[str] = []
        keys = {"url", "audio_url", "file_url", "path", "filename"}

        def walk(node: object) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if isinstance(value, str) and key.lower() in keys and value.strip():
                        urls.append(value.strip())
                    else:
                        walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(data)
        return urls

    @staticmethod
    def _extract_audio_from_json(body: bytes) -> bytes | None:
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return None

        candidates: list[str] = []

        def walk(node: object) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if isinstance(value, str) and key.lower() in _AUDIO_KEYS and len(value) > 200:
                        candidates.append(value)
                    else:
                        walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(data)
        for candidate in candidates:
            text = candidate.strip()
            if text.startswith("data:"):  # data:audio/wav;base64,xxxx
                text = text.split(",", 1)[-1]
            try:
                blob = base64.b64decode(text, validate=False)
            except (binascii.Error, ValueError):
                continue
            if _looks_like_audio(blob):
                return blob
        return None
