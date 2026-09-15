"""字幕显示节奏的测试。

背景（用户反馈「字幕和说话内容对不上」）：
实测已完成的成片，一条字幕平均 80-98 字符却停留 5-6 秒，最长 21-23 秒。
观众读到第一句时话音已经到第三句——体感就是字幕与话音不同步。
原因是断句阶段只做「合并碎片」，从不切分过长的条目。
"""

from __future__ import annotations

import pytest

from app.services.subtitles import (
    Cue,
    merge_into_sentences,
    normalize_cues,
    split_long_cues,
)


class TestSplitLongCues:
    def test_short_cue_untouched(self):
        cues = [Cue(0.0, 2.0, "短句。")]
        assert split_long_cues(cues) == cues

    def test_long_duration_is_split(self):
        cue = Cue(0.0, 21.4, "This is a very long sentence that goes on and on for far too long indeed.")
        out = split_long_cues([cue], max_duration=5.0)
        assert len(out) > 1
        for piece in out:
            assert piece.end - piece.start <= 5.0 + 0.01, f"仍有 {piece.end - piece.start:.1f}s 的条目"

    def test_long_text_is_split_even_if_short_duration(self):
        cue = Cue(0.0, 3.0, "字" * 200)
        out = split_long_cues([cue], max_chars=84)
        assert len(out) >= 3
        for piece in out:
            assert len(piece.text) <= 84, f"仍有 {len(piece.text)} 字的条目"

    def test_pieces_are_evenly_sized(self):
        """回归：一次性按目标位置切分会导致长短悬殊（实测出现过 7.4s 与 0.4s 并存）。"""
        text = " ".join(f"word{i}" for i in range(80))
        out = split_long_cues([Cue(0.0, 30.0, text)], max_duration=5.0, max_chars=84)
        durations = [c.end - c.start for c in out]
        assert len(out) > 3
        assert max(durations) <= 5.0 + 0.01
        # 最长不应超过最短的 4 倍
        assert max(durations) <= min(durations) * 4, f"切分不均匀：{durations}"

    def test_covers_original_span(self):
        cue = Cue(10.0, 40.0, "First part here. Second part here. Third part here. Fourth part now.")
        out = split_long_cues([cue], max_duration=5.0)
        assert out[0].start == pytest.approx(10.0, abs=0.01)
        assert out[-1].end == pytest.approx(40.0, abs=0.01)

    def test_timeline_is_monotonic(self):
        cue = Cue(0.0, 40.0, "a" * 400)
        out = split_long_cues([cue], max_duration=5.0, max_chars=84)
        for previous, current in zip(out, out[1:], strict=False):
            assert previous.end <= current.start + 0.01
            assert previous.start < previous.end
            assert current.start < current.end

    def test_prefers_punctuation_breaks(self):
        cue = Cue(0.0, 20.0, "First clause, second clause, third clause, fourth clause, fifth one.")
        out = split_long_cues([cue], max_duration=5.0, max_chars=40)
        # 至少有一部分切分落在逗号/句号之后
        assert any(p.text.rstrip().endswith((",", ".", "，", "。")) for p in out)

    def test_text_is_preserved(self):
        """切分只应调整空白，不能丢字或改字。"""
        import re

        text = "Hello world, this is a test of splitting behaviour with punctuation."
        out = split_long_cues([Cue(0.0, 6.0, text)], max_duration=4.0)
        def norm(value: str) -> str:
            return re.sub(r"\s+", " ", value).strip()

        assert norm(" ".join(p.text for p in out)) == norm(text)

    def test_no_flash_fragments(self):
        """不产生一闪而过的碎片。"""
        text = " ".join(f"w{i}" for i in range(60))
        out = split_long_cues([Cue(0.0, 60.0, text)], max_duration=5.0, max_chars=84)
        for piece in out:
            assert piece.end - piece.start >= 0.35, f"碎片过短：{piece.end - piece.start:.2f}s"

    def test_never_produces_unreadable_fragments(self):
        """回归：时间与文本量不匹配时不能切出「spli」「tting」这种碎片。"""
        text = "Hello world, this is a test of splitting behaviour with punctuation."
        out = split_long_cues([Cue(0.0, 30.0, text)], max_duration=4.0)
        # 自然断点可能落在短词上（如 "Hello"、"with"），这没问题；
        # 要防的是把单词从中间切开，例如 'punctu' / 'ation.'。
        for piece in out:
            assert len(piece.text) >= 4, f"碎片过短：{piece.text!r}"
        assert not any(p.text.endswith("tion.") and len(p.text) < 8 for p in out), "单词被从中间切开"
        assert " ".join(p.text for p in out).replace("  ", " ") == text

    def test_empty_input(self):
        assert split_long_cues([]) == []


class TestMergeAndSplitTogether:
    """断句 + 切分组合后的显示节奏。"""

    def test_fragmented_captions_end_up_readable(self):
        # 模拟 YouTube 自动字幕：碎片化、无句末标点
        cues = [
            Cue(float(i), float(i) + 1.0, f"fragment number {i} without any ending punctuation")
            for i in range(30)
        ]
        merged = merge_into_sentences(cues, max_duration=5.0, max_chars=84)
        final = split_long_cues(merged, max_duration=5.0, max_chars=84)
        assert final
        durations = [c.end - c.start for c in final]
        assert max(durations) <= 5.0 + 0.01, f"最长 {max(durations):.1f}s"
        assert all(len(c.text) <= 84 for c in final)
        # 覆盖率：不应丢失时间区间
        assert final[0].start == pytest.approx(0.0, abs=0.1)

    def test_real_world_case_is_fixed(self):
        """回归用户实际遇到的那条：21.4 秒 / 98 字的中文字幕。"""
        text = (
            "它会从根本上改变初创公司的运营方式，从角色定义到产品可能性。"
            "在这一集中，我将讨论创始人应该如何考虑构建AI原生公司，"
            "他们的团队应该遵循什么规则，以及他们现在可以采用的哪些具体内部实践来加速发展。"
        )
        out = split_long_cues([Cue(0.0, 21.4, text)], max_duration=5.0, max_chars=84)
        assert len(out) >= 4, f"只切成了 {len(out)} 条"
        for piece in out:
            assert piece.end - piece.start <= 5.0 + 0.01
            assert len(piece.text) <= 90  # 中文按字符计，允许标点带来的少量超出


class TestWhisperCueBuilding:
    """词级时间戳 → 字幕条目。"""

    @staticmethod
    def _asr(**overrides):
        from app.providers.asr.whisper import WhisperASR
        from app.services.settings_store import ASRConfig

        return WhisperASR(ASRConfig(provider="whisper", **overrides))

    def _words(self, spec):
        from app.providers.asr.whisper import _Word

        return [_Word(start, end, text) for start, end, text in spec]

    def test_sentence_boundary_flushes(self):
        asr = self._asr()
        words = self._words([
            (0.0, 0.4, " Hello"), (0.4, 0.8, " world."),
            (1.0, 1.4, " Next"), (1.4, 1.8, " one."),
        ])
        cues = asr._words_to_cues(words)
        assert len(cues) == 2
        assert cues[0].text == "Hello world."
        assert cues[1].start == 1.0

    def test_pause_flushes(self):
        asr = self._asr()
        words = self._words([(0.0, 0.4, " Hello"), (2.5, 2.9, " world")])
        cues = asr._words_to_cues(words)
        assert len(cues) == 2, "词间明显停顿应断句"

    def test_char_limit_flushes(self):
        asr = self._asr(whisper_max_cue_chars=30)
        words = self._words([(i * 0.3, i * 0.3 + 0.25, " word") for i in range(40)])
        cues = asr._words_to_cues(words)
        assert len(cues) > 1
        assert all(len(c.text) <= 35 for c in cues)

    def test_uses_real_word_timestamps(self):
        """时间轴必须来自词级时间戳，而不是按字数比例估算。"""
        asr = self._asr()
        words = self._words([(3.0, 3.4, " Hello"), (3.4, 4.1, " there.")])
        cues = asr._words_to_cues(words)
        assert cues[0].start == 3.0
        assert cues[0].end == 4.1

    def test_no_cues_from_empty(self):
        assert self._asr()._words_to_cues([]) == []

    def test_declares_precise_timing(self):
        """声明时间轴精确后，流水线才会跳过「按标点重新合并」。"""
        assert self._asr().cues_are_timed is True

    def test_aliyun_and_mock_are_not_marked_precise(self):
        from app.providers.asr.aliyun import AliyunASR, MockASR
        from app.services.settings_store import ASRConfig, TTSConfig

        assert AliyunASR(ASRConfig(provider="aliyun"), TTSConfig(app_key="k")).cues_are_timed is False
        assert MockASR(ASRConfig(provider="mock")).cues_are_timed is False


class TestNormalizeStillWorks:
    def test_no_regression_on_basic_normalize(self):
        cues = [Cue(1.0, 4.0, "  first  "), Cue(3.0, 5.0, "second")]
        out = normalize_cues(cues)
        assert [c.text for c in out] == ["first", "second"]
        assert out[0].end <= out[1].start + 0.01
