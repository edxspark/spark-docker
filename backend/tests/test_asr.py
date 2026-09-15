"""语音识别（无字幕兜底）的单元测试。

不访问阿里云：用假的 HTTP 客户端复现接口行为（成功、鉴权失败、限流、空结果）。
"""

from __future__ import annotations

import asyncio
import types

import pytest

from app.providers.asr.aliyun import AliyunASR, MockASR
from app.providers.base import NotConfiguredError, ProviderError
from app.services.settings_store import ASRConfig, TTSConfig
from app.utils import ffmpeg as ffmpeg_utils


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


class FakeAsyncClient:
    """按调用顺序返回预设响应，并记录请求。"""

    responses: list[dict] = []
    calls: list[dict] = []
    last: dict | None = None

    def __init__(self, *_, **kwargs) -> None:
        self.kwargs = kwargs

    async def __aenter__(self) -> FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def post(self, url, *, params=None, headers=None, content=None):
        FakeAsyncClient.calls.append(
            {"url": url, "params": dict(params or {}), "headers": dict(headers or {}), "size": len(content or b"")}
        )
        # 列表用完后重复最后一个响应：确定性错误（如鉴权失败）在真实服务端
        # 也是稳定重现的，这样才能测出「错误没有被后续重试掩盖」。
        if FakeAsyncClient.responses:
            FakeAsyncClient.last = FakeAsyncClient.responses.pop(0)
        payload = FakeAsyncClient.last if FakeAsyncClient.last is not None else {
            "status": 20000000,
            "result": "",
        }
        return FakeResponse(payload)


@pytest.fixture
def fake_httpx(monkeypatch):
    """替换被测模块里已经绑定的 httpx 名字。

    注意：只改 sys.modules 是无效的——app.providers.asr.aliyun 在导入时就把
    httpx 绑定到了自己的命名空间，必须直接替换那个属性，否则测试会真的
    发请求到阿里云（曾因此让一个用例「通过」但实际打了真实接口）。
    """
    module = types.ModuleType("httpx")
    module.AsyncClient = FakeAsyncClient  # type: ignore[attr-defined]
    FakeAsyncClient.responses = []
    FakeAsyncClient.calls = []
    FakeAsyncClient.last = None
    monkeypatch.setattr("app.providers.asr.aliyun.httpx", module)
    return FakeAsyncClient


@pytest.fixture
def credentials() -> TTSConfig:
    return TTSConfig(provider="aliyun", app_key="testAppKey", token="testToken", region="cn-shanghai")


def _asr(credentials: TTSConfig, **overrides) -> AliyunASR:
    return AliyunASR(ASRConfig(provider="aliyun", **overrides), credentials)


class TestCredentials:
    def test_requires_app_key(self):
        with pytest.raises(NotConfiguredError) as info:
            AliyunASR(ASRConfig(), TTSConfig(app_key=""))
        assert "AppKey" in str(info.value)

    def test_reuses_tts_project_credentials(self, credentials):
        asr = _asr(credentials)
        assert asr.credentials.app_key == "testAppKey"
        assert "cn-shanghai" in asr.endpoint
        assert asr.endpoint.endswith("/stream/v1/asr")

    def test_region_follows_credentials(self):
        asr = _asr(TTSConfig(app_key="k", region="cn-beijing"))
        assert "nls-gateway-cn-beijing" in asr.endpoint


class TestRequestShape:
    """接口契约：参数名、请求头、PCM 字节体。文档要求严格一致。"""

    def test_request_matches_documented_contract(self, fake_httpx, credentials, sample_video):
        fake_httpx.responses = [{"status": 20000000, "result": "Hello world."}]
        asr = _asr(credentials, max_chunk_seconds=10)
        asyncio.run(asr.transcribe(sample_video, total_duration=12.0))

        assert fake_httpx.calls, "没有发出任何识别请求"
        call = fake_httpx.calls[0]
        assert call["url"].endswith("/stream/v1/asr")
        assert call["params"]["appkey"] == "testAppKey"
        assert call["params"]["format"] == "pcm"
        assert call["params"]["sample_rate"] == "16000"
        assert call["params"]["enable_punctuation_prediction"] == "true"
        assert call["headers"]["X-NLS-Token"] == "testToken"
        assert call["headers"]["Content-Type"] == "application/octet-stream"
        assert call["size"] > 0, "请求体里没有音频数据"


class TestPcmIntegrity:
    """回归：PCM 是二进制，曾因按 UTF-8 解码而丢失 66% 的字节。"""

    def test_extract_pcm_returns_exact_byte_count(self, sample_video):
        async def run():
            return await ffmpeg_utils.extract_pcm(sample_video, start=0.0, duration=3.0, sample_rate=16000)

        pcm = asyncio.run(run())
        expected = 16000 * 2 * 3  # 16kHz, 16bit, 单声道, 3 秒
        assert abs(len(pcm) - expected) <= 16000 * 2 * 0.05, (
            f"PCM 字节数异常：得到 {len(pcm)}，期望约 {expected}（可能被按文本解码截断了）"
        )

    def test_extract_pcm_rejects_video_without_audio(self, tmp_path):
        import subprocess

        from app.utils.binaries import resolve_binary

        silent = tmp_path / "silent.mp4"
        subprocess.run(
            [
                resolve_binary("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(silent),
            ],
            check=True,
        )
        with pytest.raises(ffmpeg_utils.FFmpegError):
            asyncio.run(ffmpeg_utils.extract_pcm(silent, start=0.0, duration=1.0))


class TestSpeechSegmentation:
    def test_detects_speech_and_groups(self, sample_video):
        async def run():
            segments = await ffmpeg_utils.detect_speech_segments(sample_video)
            return segments, ffmpeg_utils.group_segments(segments, max_duration=5.0)

        segments, chunks = asyncio.run(run())
        assert segments, "应至少检测到一段语音"
        assert chunks, "应至少合并出一个识别块"
        for start, end in chunks:
            assert end > start
            assert end - start <= 5.0 + 0.001, "分块超过了上限"

    def test_group_segments_respects_limit(self):
        segments = [(0.0, 3.0), (5.0, 9.0), (12.0, 20.0), (21.0, 24.0)]
        chunks = ffmpeg_utils.group_segments(segments, max_duration=10.0)
        for start, end in chunks:
            assert end - start <= 10.0
        # 相邻块之间不应丢失内容
        assert chunks[0][0] == 0.0
        assert chunks[-1][1] == 24.0

    def test_group_segments_handles_empty(self):
        assert ffmpeg_utils.group_segments([], max_duration=10.0) == []


class TestTimestampDistribution:
    """接口不返回词级时间戳，块内按字符数比例分配。"""

    def test_single_sentence_spans_whole_chunk(self, credentials):
        asr = _asr(credentials)
        cues = asr._distribute("Hello world.", 10.0, 20.0)
        assert len(cues) == 1
        assert cues[0].start == 10.0 and cues[0].end == 20.0

    def test_sentences_are_ordered_and_cover_span(self, credentials):
        asr = _asr(credentials)
        cues = asr._distribute("First one. Second one is longer. Third.", 0.0, 30.0)
        assert len(cues) == 3
        assert all(cues[i].end <= cues[i + 1].start + 0.001 for i in range(len(cues) - 1))
        assert cues[0].start == 0.0
        assert cues[-1].end == 30.0
        # 更长的句子应分到更长的时间
        assert (cues[1].end - cues[1].start) > (cues[0].end - cues[0].start)

    def test_splits_chinese_punctuation(self, credentials):
        asr = _asr(credentials)
        cues = asr._distribute("第一句。第二句！第三句？", 0.0, 9.0)
        assert [c.text for c in cues] == ["第一句。", "第二句！", "第三句？"]

    def test_empty_text_yields_nothing(self, credentials):
        asr = _asr(credentials)
        assert asr._distribute("", 0.0, 10.0) == []
        assert asr._distribute("   ", 0.0, 10.0) == []


class TestErrorHandling:
    def test_auth_failure_is_reported(self, fake_httpx, credentials, sample_video, monkeypatch):
        fake_httpx.responses = [{"status": 40000001, "message": "The token is invalid"}]

        async def fake_token(_config):
            return "bad-token"

        monkeypatch.setattr("app.providers.asr.aliyun.AliyunTokenManager.get", staticmethod(fake_token))
        asr = _asr(credentials, max_chunk_seconds=10)
        with pytest.raises(ProviderError) as info:
            asyncio.run(asr.transcribe(sample_video, total_duration=12.0))
        assert "鉴权" in str(info.value)

    def test_empty_result_gives_actionable_hint(self, fake_httpx, credentials, sample_video, monkeypatch):
        fake_httpx.responses = [{"status": 20000000, "result": ""}] * 4

        async def fake_token(_config):
            return "t"

        monkeypatch.setattr("app.providers.asr.aliyun.AliyunTokenManager.get", staticmethod(fake_token))
        asr = _asr(credentials, max_chunk_seconds=10)
        with pytest.raises(ProviderError) as info:
            asyncio.run(asr.transcribe(sample_video, total_duration=12.0))
        assert "模型" in str(info.value), "应提示检查控制台的识别模型语种"

    def test_duration_limit_is_enforced(self, fake_httpx, credentials, sample_video):
        asr = _asr(credentials, max_duration_minutes=1)
        with pytest.raises(ProviderError) as info:
            asyncio.run(asr.transcribe(sample_video, total_duration=3600.0))
        assert "上限" in str(info.value)

    def test_missing_media(self, fake_httpx, credentials, tmp_path):
        asr = _asr(credentials)
        with pytest.raises(ProviderError):
            asyncio.run(asr.transcribe(tmp_path / "nope.mp4"))


class TestMockASR:
    def test_produces_timestamped_cues(self, sample_video):
        asr = MockASR(ASRConfig(provider="mock"))
        cues = asyncio.run(asr.transcribe(sample_video, total_duration=12.0))
        assert cues
        assert all(cue.text for cue in cues)
        assert cues[0].start >= 0
        assert all(cues[i].end <= cues[i + 1].start + 0.001 for i in range(len(cues) - 1))

    def test_rejects_missing_media(self, tmp_path):
        asr = MockASR(ASRConfig(provider="mock"))
        with pytest.raises(ProviderError):
            asyncio.run(asr.transcribe(tmp_path / "missing.mp4"))


class TestGroupSegmentsSplitting:
    """回归：连续语音（检测不到静音）时整段音频必须被切开。

    否则 2 小时的无静音视频会变成一个巨型请求，被接口以「音频过长」拒绝。
    """

    def test_splits_single_long_segment(self):
        chunks = ffmpeg_utils.group_segments([(0.0, 7200.0)], max_duration=40.0)
        assert len(chunks) == 180, f"2 小时应切成 180 块，实际 {len(chunks)}"
        assert all(end - start <= 40.0 + 1e-6 for start, end in chunks)
        assert chunks[0][0] == 0.0
        assert abs(chunks[-1][1] - 7200.0) < 1e-6

    def test_chunks_are_contiguous_and_ordered(self):
        chunks = ffmpeg_utils.group_segments([(0.0, 100.0)], max_duration=30.0)
        assert all(chunks[i][1] <= chunks[i + 1][0] + 1e-6 for i in range(len(chunks) - 1))
        assert all(start < end for start, end in chunks)

    def test_merges_short_neighbours_within_limit(self):
        chunks = ffmpeg_utils.group_segments([(0.0, 5.0), (6.0, 12.0), (13.0, 18.0)], max_duration=40.0)
        assert len(chunks) == 1, "相邻短段应被合并"
        assert chunks[0] == (0.0, 18.0)

    def test_mixed_long_and_short(self):
        chunks = ffmpeg_utils.group_segments([(0.0, 50.0), (51.0, 55.0)], max_duration=20.0)
        assert all(end - start <= 20.0 + 1e-6 for start, end in chunks)
        assert chunks[-1][1] == 55.0

    def test_ignores_invalid_segments(self):
        assert ffmpeg_utils.group_segments([(5.0, 5.0), (9.0, 3.0)], max_duration=10.0) == []


class TestNoRealNetworkCalls:
    """确保测试永远不会真的访问外部服务。"""

    def test_httpx_is_replaced_on_module(self, fake_httpx):
        import app.providers.asr.aliyun as module

        assert module.httpx.AsyncClient is FakeAsyncClient, (
            "被测模块里的 httpx 未被替换，测试会真的请求阿里云接口"
        )

    def test_token_not_fetched_when_manually_configured(self, fake_httpx, credentials, sample_video):
        """配置了手工 Token 时不应再去签发接口取 Token。"""
        fake_httpx.responses = [{"status": 20000000, "result": "Hi."}]
        asr = _asr(credentials, max_chunk_seconds=10)
        asyncio.run(asr.transcribe(sample_video, total_duration=12.0))
        assert all("CreateToken" not in call["url"] for call in fake_httpx.calls)
        assert all("nls-meta" not in call["url"] for call in fake_httpx.calls)


class TestChunkCaching:
    """语音识别又慢又贵：分块结果必须落盘，中断重试时复用。"""

    def test_cache_roundtrip(self, tmp_path):
        cache = tmp_path / "asr"
        path = AliyunASR._cache_path(cache, 3, 40.0, 80.0)
        assert path is not None and "00003" in path.name
        AliyunASR._write_cache(cache, 3, 40.0, 80.0, "Hello there.")
        assert AliyunASR._read_cache(cache, 3, 40.0, 80.0) == "Hello there."

    def test_cache_miss_returns_none(self, tmp_path):
        assert AliyunASR._read_cache(tmp_path / "asr", 9, 0.0, 10.0) is None

    def test_empty_result_is_not_cached(self, tmp_path):
        """空结果可能是瞬时故障，不应被缓存成永久结果。"""
        cache = tmp_path / "asr"
        AliyunASR._write_cache(cache, 0, 0.0, 10.0, "")
        assert AliyunASR._read_cache(cache, 0, 0.0, 10.0) is None

    def test_no_cache_dir_is_silent(self):
        assert AliyunASR._cache_path(None, 0, 0.0, 1.0) is None
        assert AliyunASR._read_cache(None, 0, 0.0, 1.0) is None
        AliyunASR._write_cache(None, 0, 0.0, 1.0, "x")  # 不应抛异常

    def test_second_run_reuses_cache_without_requests(self, fake_httpx, credentials, sample_video, tmp_path, monkeypatch):
        """第二次识别应完全命中缓存，不再发出任何请求。"""
        async def fake_token(_config):
            return "t"

        monkeypatch.setattr("app.providers.asr.aliyun.AliyunTokenManager.get", staticmethod(fake_token))
        cache = tmp_path / "asr"

        fake_httpx.responses = [{"status": 20000000, "result": "First chunk."}] * 20
        asr = _asr(credentials, max_chunk_seconds=10)
        first = asyncio.run(asr.transcribe(sample_video, total_duration=12.0, cache_dir=cache))
        calls_after_first = len(fake_httpx.calls)
        assert calls_after_first > 0 and first

        fake_httpx.calls.clear()
        second = asyncio.run(asr.transcribe(sample_video, total_duration=12.0, cache_dir=cache))
        assert fake_httpx.calls == [], "第二次识别仍发出了请求，缓存没有生效"
        assert [c.text for c in second] == [c.text for c in first]
