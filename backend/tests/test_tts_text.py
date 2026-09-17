"""ChatTTS 长文本改造：文本归一化、分片合成、服务端兼容。

背景：ChatTTS 单次推理 token 有限，长句容易在结尾出现杂音；
中文里混着的英文缩写、百分号、单位符号也常被读错甚至吞掉。
这里覆盖三件事：
1. 归一化只改「送给模型的文本」，不改字幕原文；
2. 长文本会按字数上限切片，且切片后逐片裁静音再拼接；
3. 兼容「返回 JSON + 音频 URL」与「{"code":1,"msg":...} 业务错误」两种响应。
"""

from __future__ import annotations

import json

import pytest

from app.providers.base import ProviderError
from app.providers.tts.chattts import ChatTTS
from app.services import tts_text
from app.services.settings_store import ChatTTSConfig, TTSConfig
from app.utils.text import split_for_tts

pytestmark = pytest.mark.asyncio


def _provider(**overrides) -> ChatTTS:
    config = TTSConfig(provider="chattts", **overrides)
    return ChatTTS(config)


# ---------------------------------------------------------------- 文本归一化


async def test_acronyms_are_spelled_out():
    assert tts_text.normalize_for_tts("AI创业") == "A I创业"
    assert tts_text.normalize_for_tts("用API和URL") == "用A P I和U R L"
    # 已经写成 A I 的不再二次拆分
    assert tts_text.normalize_for_tts("A I 创业") == "A I 创业"


async def test_numbers_and_units_are_chinese_friendly():
    assert tts_text.normalize_for_tts("增长15.5%") == "增长百分之15点5"
    assert tts_text.normalize_for_tts("38.5℃") == "38点5摄氏度"
    assert tts_text.normalize_for_tts("120km/h") == "120公里每小时"
    assert tts_text.normalize_for_tts("内存16GB") == "内存16G B"


async def test_punctuation_is_normalized_for_tts():
    assert tts_text.normalize_for_tts("对比：效果如何？") == "对比。效果如何？"
    assert tts_text.normalize_for_tts("标题（附注）") == "标题，附注"
    # 连续标点收敛；句末标点会被去掉（TTS 不需要，留着反而容易多出停顿）
    assert tts_text.normalize_for_tts("真的。。。") == "真的"
    assert tts_text.normalize_for_tts("句末标点。") == "句末标点"


async def test_default_term_rules_cover_brand_and_products():
    out = tts_text.normalize_for_tts("欢迎来到EdxSpark，这里讲ChatTTS与GPT-4")
    assert "Edx Spark" in out
    assert "Chat T T S" in out
    assert "G P T 4" in out
    assert "EdxSpark" not in out


async def test_custom_term_rules_and_parse():
    rules = tts_text.parse_term_rules("抖音=抖音短视频\n小红书：小红书")
    assert ("抖音", "抖音短视频") in rules
    assert ("小红书", "小红书") in rules
    # 只给原文时按逐字母展开
    assert tts_text.parse_term_rules("ABC") == [("ABC", "A B C")]
    # 长词优先，避免短词先把长词拆开
    assert [key for key, _ in tts_text.parse_term_rules("TTS\nChatTTS")] == ["ChatTTS", "TTS"]


async def test_normalization_can_be_disabled_and_keeps_subtitle_text():
    """归一化只作用于送给模型的文本：字幕原文必须保持原样。"""
    provider = _provider(normalize_text=False)
    assert provider._prepare_text("AI创业15.5%") == "AI创业15.5%"

    enabled = _provider(normalize_text=True)
    assert enabled._prepare_text("AI创业15.5%") == "A I创业百分之15点5"

    # 原文（会被写进字幕的那份）没有被就地修改
    original = "AI创业15.5%"
    enabled._prepare_text(original)
    assert original == "AI创业15.5%"


async def test_user_term_rules_apply():
    provider = _provider(normalize_text=True, term_rules="AI=人工智能")
    assert provider._prepare_text("AI创业") == "人工智能创业"


# ---------------------------------------------------------------- 分片


async def test_chunk_limit_uses_chattts_setting():
    provider = _provider(tts_chunk_chars=60)
    assert provider.chunk_limit() == 60

    # 配置为空/为 0 时退回公共上限（旧配置里没有这个字段的情况）
    from types import SimpleNamespace

    from app.providers.tts.chattts import ChatTTS as _ChatTTS

    fallback = _ChatTTS(
        SimpleNamespace(
            tts_chunk_chars=0, max_chars_per_request=250,
            chattts_base_url="http://127.0.0.1:9966", chattts_api_path="/tts",
        )
    )
    assert fallback.chunk_limit() == 250
    assert ChatTTSConfig().tts_chunk_chars == 80

    long_text = "这是一句测试文本。" * 20  # 180 字
    chunks = split_for_tts(long_text, provider.chunk_limit())
    assert len(chunks) > 1, "长文本必须被切开"
    assert all(len(chunk) <= 60 for chunk in chunks)
    assert "".join(chunks) == long_text, "切分不应丢字"


async def test_long_text_is_chunked_and_silence_trimmed(monkeypatch, tmp_path):
    """长文本要逐片请求，并且每片拼接前裁掉首尾静音。"""
    provider = _provider(tts_chunk_chars=40)
    calls: list[str] = []
    trimmed: list[str] = []

    async def fake_request(text: str, out_path):
        calls.append(text)
        out_path.write_bytes(b"RIFF" + b"\x00" * 64)

    async def fake_trim(src, dst, *args, **kwargs):
        trimmed.append(src.name)
        dst.write_bytes(b"RIFF" + b"\x00" * 32)
        return dst

    async def fake_concat(parts, out_path):
        out_path.write_bytes(b"RIFF" + b"\x00" * 128)
        return out_path

    async def fake_duration(path):
        return 3.5

    monkeypatch.setattr(provider, "_request", fake_request)
    monkeypatch.setattr("app.providers.tts.chattts.ffmpeg_utils.trim_silence", fake_trim)
    monkeypatch.setattr("app.providers.tts.chattts.ffmpeg_utils.concat_audio", fake_concat)
    monkeypatch.setattr("app.providers.tts.chattts.ffmpeg_utils.audio_duration", fake_duration)

    text = "第一句话在这里。第二句话也在这里。第三句话还在这里。第四句话同样如此。" * 3  # 105 字
    result = await provider.synthesize(text, tmp_path / "out.mp3")

    assert len(calls) >= 3, f"长文本没有分片：{calls}"
    # 送进模型的是归一化后的文本（末尾标点会被去掉），但内容不能丢
    assert "".join(calls) == provider._prepare_text(text), "分片不能丢内容"
    assert len(trimmed) == len(calls), "每一片都必须裁静音后再拼接"
    assert result.duration == 3.5
    assert result.characters == len(text)


async def test_short_text_uses_single_request(monkeypatch, tmp_path):
    provider = _provider(tts_chunk_chars=80)
    calls: list[str] = []

    async def fake_request(text: str, out_path):
        calls.append(text)
        out_path.write_bytes(b"RIFF" + b"\x00" * 64)

    async def fake_duration(path):
        return 1.0

    monkeypatch.setattr(provider, "_request", fake_request)
    monkeypatch.setattr("app.providers.tts.chattts.ffmpeg_utils.audio_duration", fake_duration)

    await provider.synthesize("短句。", tmp_path / "out.mp3")
    assert len(calls) == 1


# ---------------------------------------------------------------- 服务端兼容


async def test_json_with_audio_url_is_downloaded(monkeypatch, tmp_path):
    """LongAudio 的 /tts 返回 JSON + url，必须把音频取回来。"""
    provider = _provider()
    body = json.dumps(
        {"code": 0, "msg": "ok", "audio_files": [{"url": "http://127.0.0.1:9966/static/wavs/a.wav"}]}
    ).encode()

    wav_bytes = b"RIFF" + b"\x00" * 64

    class FakeResponse:
        status_code = 200
        headers = {"Content-Type": "application/json"}
        content = body
        text = body.decode()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            class Downloaded:
                status_code = 200
                content = wav_bytes

            assert url.endswith("/static/wavs/a.wav")
            return Downloaded()

    monkeypatch.setattr("app.providers.tts.chattts.httpx.AsyncClient", FakeClient)

    out = tmp_path / "out.wav"
    await provider._save(FakeResponse(), out)
    assert out.read_bytes() == wav_bytes, "没有把 JSON 里的音频 URL 下载回来"


async def test_json_error_payload_is_reported(monkeypatch, tmp_path):
    provider = _provider()
    body = json.dumps({"code": 1, "msg": "保存音频失败: no space"}).encode()

    class FakeResponse:
        status_code = 200
        headers = {"Content-Type": "application/json"}
        content = body
        text = body.decode()

    with pytest.raises(ProviderError) as excinfo:
        await provider._save(FakeResponse(), tmp_path / "out.wav")
    assert "no space" in str(excinfo.value)
