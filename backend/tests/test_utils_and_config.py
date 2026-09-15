"""文本工具与配置存储的单元测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.crypto import decrypt, encrypt, is_encrypted, mask
from app.services.settings_store import (
    DEFAULT_CONFIG,
    TranslatorConfig,
    TTSConfig,
    _mask_section,
    _normalize,
)
from app.utils.text import (
    estimate_tokens,
    extract_tags,
    normalize_punct,
    sanitize_filename,
    split_for_tts,
    truncate,
)


class TestCrypto:
    def test_encrypt_decrypt_roundtrip(self):
        plain = "sk-abcdef1234567890"
        stored = encrypt(plain)
        assert is_encrypted(stored)
        assert plain not in stored
        assert decrypt(stored) == plain

    def test_encrypt_is_idempotent_for_ciphertext(self):
        once = encrypt("secret")
        assert encrypt(once) == once

    def test_decrypt_plaintext_passthrough(self):
        # 兼容手工写入的明文配置
        assert decrypt("legacy-plain") == "legacy-plain"
        assert decrypt("") == ""

    def test_mask(self):
        assert mask("sk-1234567890abcdef") == "sk-1********cdef"
        assert mask("short") == "*****"
        assert mask("") == ""

    def test_mask_section_marks_presence(self):
        masked = _mask_section("translator", {"api_key": "sk-real-key-value"})
        assert masked["api_key__set"] is True
        assert "sk-real-key-value" not in masked["api_key"]

        empty = _mask_section("translator", {"api_key": ""})
        assert empty["api_key__set"] is False


class TestConfigNormalize:
    def test_normalize_fills_defaults(self):
        data = _normalize("translator", {"provider": "deepseek", "api_key": "x"})
        assert data["provider"] == "deepseek"
        assert data["api_key"] == "x"
        assert data["model"] == DEFAULT_CONFIG["translator"]["model"]

    def test_normalize_rejects_invalid_enum(self):
        with pytest.raises(ValidationError):
            _normalize("tts", {"provider": "not-a-provider"})

    def test_subtitle_langs_accepts_comma_string(self):
        from app.services.settings_store import DownloadConfig

        config = DownloadConfig(subtitle_langs="en, en-US ,zh")
        assert config.subtitle_langs == ["en", "en-US", "zh"]

    def test_tags_accept_string_with_hash(self):
        from app.services.settings_store import PublishConfig

        config = PublishConfig(default_tags="#搬运, 科普 ，vlog")
        assert config.default_tags == ["搬运", "科普", "vlog"]

    def test_tts_ranges_enforced(self):
        with pytest.raises(ValidationError):
            TTSConfig(speech_rate=999)
        with pytest.raises(ValidationError):
            TranslatorConfig(temperature=5)


class TestTextUtils:
    def test_normalize_punct_converts_to_fullwidth(self):
        assert normalize_punct("你好,世界!") == "你好，世界！"

    def test_normalize_punct_keeps_decimal_point(self):
        assert normalize_punct("圆周率是 3.14") == "圆周率是 3.14"

    def test_normalize_punct_collapses_repeats(self):
        assert normalize_punct("真的吗？？？") == "真的吗？"

    def test_split_for_tts_short_text_untouched(self):
        assert split_for_tts("短句子。", 280) == ["短句子。"]

    def test_split_for_tts_respects_limit(self):
        text = "这是一句测试文本。" * 60  # 540 字符
        pieces = split_for_tts(text, 100)
        assert len(pieces) > 1
        assert all(len(piece) <= 100 for piece in pieces)
        assert "".join(pieces) == text

    def test_split_for_tts_hard_splits_long_token(self):
        text = "a" * 250
        pieces = split_for_tts(text, 100)
        assert [len(p) for p in pieces] == [100, 100, 50]

    def test_truncate(self):
        assert truncate("abcdefghij", 5) == "abcd…"
        assert truncate("abc", 5) == "abc"

    def test_sanitize_filename(self):
        assert sanitize_filename('a/b:c*d?"e') == "a_b_c_d_e"
        assert sanitize_filename("   ") == "untitled"
        assert len(sanitize_filename("x" * 200)) <= 80

    def test_extract_tags_prefers_explicit_hashtags(self):
        tags = extract_tags("learn python #编程 #教程", limit=2)
        assert tags == ["编程", "教程"]

    def test_extract_tags_falls_back_to_keywords(self):
        tags = extract_tags("Python python tutorial about python", limit=3)
        assert "python" in tags

    def test_estimate_tokens_counts_chinese_per_char(self):
        assert estimate_tokens("中文四字") == 4
        assert estimate_tokens("abcdefgh") == 2


class TestSubtitleFont:
    """字幕字体解析：避免 libass 静默回退到无中文字形的字体。"""

    def test_resolves_away_from_broken_pingfang(self):
        from app.utils.ffmpeg import available_fonts, resolve_subtitle_font

        fonts = available_fonts()
        resolved = resolve_subtitle_font("PingFang SC")
        assert resolved, "必须解析出一个字体名"
        if fonts and "PingFang SC" not in fonts:
            # macOS 上 PingFang SC 会被解析到 libass 打不开的 .ttc，必须换掉
            assert resolved != "PingFang SC"

    def test_keeps_explicit_font_when_available(self):
        from app.utils.ffmpeg import available_fonts, resolve_subtitle_font

        fonts = available_fonts()
        if "Heiti SC" in fonts:
            assert resolve_subtitle_font("Heiti SC") == "Heiti SC"

    def test_empty_prefers_known_candidate(self):
        from app.utils.ffmpeg import available_fonts, resolve_subtitle_font

        if not available_fonts():
            pytest.skip("系统未安装 fontconfig，无法探测字体")
        assert resolve_subtitle_font("") in available_fonts()

    def test_style_includes_resolved_font(self):
        from app.utils.ffmpeg import build_subtitle_style, resolve_subtitle_font

        style = build_subtitle_style(22, "", 80)
        assert f"FontName={resolve_subtitle_font('')}" in style
        assert "FontSize=22" in style and "MarginV=80" in style
        assert "Alignment=2" in style
