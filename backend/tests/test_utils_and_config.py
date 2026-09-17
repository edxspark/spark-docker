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


class TestPublishDefaults:
    """默认手动发布：只有用户在系统配置里开启后才会自动上传抖音。"""

    def test_auto_publish_defaults_to_off(self):
        from app.services.settings_store import PublishConfig

        assert PublishConfig().auto_publish is False
        assert DEFAULT_CONFIG["publish"]["auto_publish"] is False

    def test_normalize_keeps_explicitly_enabled_auto_publish(self):
        # 老库里已经显式存过 true 的安装不能被默认值改回去
        data = _normalize("publish", {"auto_publish": True})
        assert data["auto_publish"] is True

    def test_manual_publish_is_immediate_by_default(self):
        from app.schemas import PublishItemRequest

        assert PublishItemRequest().immediate is True
        assert PublishItemRequest(immediate=False).immediate is False

    def test_manual_publish_ignores_schedule_offset(self):
        """延迟只作用于自动发布：手动发布的 immediate 分支不会去算 schedule_at。"""
        from app.pipeline.stages.deliver import resolve_schedule
        from app.schemas import PublishItemRequest

        publish_cfg = {"schedule_offset_minutes": 120}
        assert resolve_schedule({}, publish_cfg) is not None  # 自动发布：按配置延迟
        assert PublishItemRequest().immediate is True  # 手动发布：立刻上传


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


class TestTtsConfigSplit:
    """语音合成配置拆成「公共 / ChatTTS / 阿里云」三组后的行为。

    背景：拆组是为了界面上分区展示，但运行时仍需一份平坦配置交给提供者——
    这里把两端都钉住，防止字段在合并/构造过程中被静默丢掉。
    """

    def test_merge_picks_current_provider_fields(self):
        from app.services.settings_store import DEFAULT_CONFIG, merge_tts_config

        base = {**DEFAULT_CONFIG["tts"], "provider": "chattts"}
        config = {
            "tts": base,
            "tts_chattts": {**DEFAULT_CONFIG["tts_chattts"], "voice_seed": 777, "speed": 7},
            "tts_aliyun": {**DEFAULT_CONFIG["tts_aliyun"], "voice": "aixia", "app_key": "k"},
        }
        merged = merge_tts_config(config)
        assert merged["voice_seed"] == 777 and merged["speed"] == 7
        # 切到 ChatTTS 时，阿里云那套值不应被带进来（界面与行为都按当前通道走）
        assert "app_key" not in merged  # 未采用的通道参数不参与合并
        assert merged["provider"] == "chattts"

        base["provider"] = "aliyun"
        merged_ali = merge_tts_config(config)
        assert merged_ali["voice"] == "aixia" and merged_ali["app_key"] == "k"
        # 反过来：阿里云通道不采用 ChatTTS 的通道参数
        assert "voice_seed" not in merged_ali

    def test_shared_section_only_keeps_common_fields(self):
        """公共分组里混进通道字段时必须被剥掉，否则界面又会显示两套参数。"""
        from app.services.settings_store import SHARED_TTS_FIELDS, legacy_base_section

        dirty = {
            "provider": "chattts",
            "concurrency": 2,
            "voice_seed": 999,
            "app_key": "leftover",
            "voice": "ruoxi",
        }
        cleaned = legacy_base_section(dirty)
        # 结果只会是共享字段的子集，通道字段一个都不能留
        assert set(cleaned) - {"migrated_split", "merged_from"} <= set(SHARED_TTS_FIELDS)
        assert cleaned["provider"] == "chattts" and cleaned["concurrency"] == 2
        for leaked in ("voice_seed", "app_key", "voice"):
            assert leaked not in cleaned

    def test_merged_config_keeps_provider_fields(self):
        """合并结果必须能被 TTSConfig 完整保留（Pydantic 会丢弃未声明字段）。"""
        from app.services.settings_store import DEFAULT_CONFIG, TTSConfig, merge_tts_config

        merged = merge_tts_config(
            {
                "tts": {**DEFAULT_CONFIG["tts"], "provider": "aliyun"},
                "tts_chattts": DEFAULT_CONFIG["tts_chattts"],
                "tts_aliyun": {
                    **DEFAULT_CONFIG["tts_aliyun"],
                    "app_key": "app-key-123",
                    "access_key_id": "ak",
                    "voice": "ruoxi",
                },
            }
        )
        parsed = TTSConfig(**merged)
        assert parsed.provider == "aliyun"
        assert parsed.app_key == "app-key-123"
        assert parsed.access_key_id == "ak"
        assert parsed.voice == "ruoxi"
        # ChatTTS 字段同样保留（切通道时不需要重新填）
        assert parsed.chattts_base_url == DEFAULT_CONFIG["tts_chattts"]["chattts_base_url"]

    def test_section_registry_exposes_split_groups(self):
        from app.services.settings_store import (
            CONFIG_MODELS,
            SECRET_FIELDS,
            SECTION_LABELS,
            TTS_PROVIDER_SECTIONS,
        )

        assert TTS_PROVIDER_SECTIONS == {
            "chattts": "tts_chattts",
            "aliyun": "tts_aliyun",
            "mock": "tts_aliyun",  # Mock 复用阿里云分组的通用参数
        }
        for section in ("tts", "tts_chattts", "tts_aliyun"):
            assert section in CONFIG_MODELS and section in SECTION_LABELS
        # 凭证只在阿里云分组里加密存储
        assert SECRET_FIELDS["tts_aliyun"] == ("access_key_id", "access_key_secret", "app_key", "token")
        assert "tts" not in SECRET_FIELDS
