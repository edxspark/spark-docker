"""ASS 字幕生成与字号映射的单元测试。

核心回归：libass 解析 SRT 时会套用 PlayResY=288 的默认坐标系，
FontSize 与 MarginV 被放大「画布高/288」倍（实测 1920 高画布上 FontSize=20
渲染出 123px）。改为显式生成带 PlayRes 的 ASS 后，字号必须等于真实像素。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from app.services import ass_subtitles as A
from app.services.subtitles import Cue


class TestTimestamp:
    def test_formats_centiseconds(self):
        assert A._ass_timestamp(0) == "0:00:00.00"
        assert A._ass_timestamp(1.5) == "0:00:01.50"
        assert A._ass_timestamp(62.25) == "0:01:02.25"
        assert A._ass_timestamp(3723.004) == "1:02:03.00"

    def test_rounds_up_without_overflow(self):
        # 0.999 -> 进位成 1.00，不能出现 .100
        assert A._ass_timestamp(0.999) == "0:00:01.00"
        assert A._ass_timestamp(59.999) == "0:01:00.00"


class TestEscaping:
    def test_newlines_become_literal_placeholder(self):
        assert A.escape_ass_text("a\nb") == "a b"

    def test_braces_are_escaped(self):
        escaped = A.escape_ass_text("{not a tag}")
        assert "\\{" in escaped and "\\}" in escaped
        # 不能残留会被 libass 当作覆盖标签解析的裸花括号
        assert not re.search(r"(?<!\\)[{}]", escaped)

    def test_plain_text_untouched(self):
        assert A.escape_ass_text("  Hello 世界  ") == "Hello 世界"


class TestResolutionMapping:
    def test_original_keeps_source_size(self):
        assert A.output_resolution(1920, 1080, "original") == (1920, 1080)
        assert A.output_resolution(640, 360, "original") == (640, 360)

    def test_aspect_targets_fixed_sizes(self):
        assert A.output_resolution(1920, 1080, "9:16") == (1080, 1920)
        assert A.output_resolution(640, 360, "16:9") == (1920, 1080)


class TestFontSizeMapping:
    """字号以 1080p 为基准等比缩放，数字必须等于真实像素。"""

    def test_1080p_is_identity(self):
        style = A.SubtitleStyle(font_size=14, margin_v=40)
        assert style.scaled_font_size(1080) == 14
        assert style.scaled_margin(1080) == 40

    def test_vertical_scales_up(self):
        style = A.SubtitleStyle(font_size=14, margin_v=40)
        assert style.scaled_font_size(1920) == 25  # 14 * 1920/1080
        assert style.scaled_margin(1920) == 71

    def test_small_frame_scales_down_with_floor(self):
        style = A.SubtitleStyle(font_size=14, margin_v=40)
        assert style.scaled_font_size(360) == 8  # 下限 8px，避免完全看不清
        assert style.scaled_margin(360) == 13

    def test_zero_margin_allowed(self):
        assert A.SubtitleStyle(font_size=14, margin_v=0).scaled_margin(1080) == 0


class TestBuildAss:
    def _cues(self):
        return [
            Cue(0.2, 2.2, "大家好，欢迎回来。"),
            Cue(2.4, 5.0, "今天我们来学点有用的东西。"),
        ]

    def test_header_pins_playres_to_frame(self):
        ass = A.build_ass(self._cues(), width=1920, height=1080, style=A.SubtitleStyle())
        assert "PlayResX: 1920" in ass
        assert "PlayResY: 1080" in ass
        assert "[V4+ Styles]" in ass and "[Events]" in ass

    def test_font_size_written_as_real_pixels(self):
        style = A.SubtitleStyle(font_size=14, margin_v=40)
        ass = A.build_ass(self._cues(), width=1920, height=1080, style=style)
        style_line = next(line for line in ass.splitlines() if line.startswith("Style: Default,"))
        fields = style_line.split(",")
        assert fields[2] == "14", f"字号不是真实像素：{fields[2]}"
        assert fields[20] == "40", f"MarginV 不是真实像素：{fields[20]}"
        assert fields[18] == "2", "底部居中应为 Alignment=2"

    def test_vertical_video_scales_font(self):
        style = A.SubtitleStyle(font_size=14, margin_v=40)
        ass = A.build_ass(self._cues(), width=1080, height=1920, style=style)
        fields = next(
            line for line in ass.splitlines() if line.startswith("Style: Default,")
        ).split(",")
        assert fields[2] == "25"

    def test_alignment_options(self):
        for name, expected in (("bottom", 2), ("middle", 5), ("top", 8)):
            ass = A.build_ass(self._cues(), width=1920, height=1080, style=A.SubtitleStyle(alignment=name))
            fields = next(line for line in ass.splitlines() if line.startswith("Style: Default,")).split(",")
            assert fields[18] == str(expected), f"{name} 应对应 Alignment={expected}"

    def test_bilingual_puts_zh_then_en(self):
        en = [Cue(0.2, 2.2, "Hello everyone."), Cue(2.4, 5.0, "Today we learn.")]
        ass = A.build_ass(self._cues(), width=1920, height=1080, style=A.SubtitleStyle(), secondary_cues=en)
        dialogues = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
        assert len(dialogues) == 2
        assert "大家好，欢迎回来。\\NHello everyone." in dialogues[0]
        assert "今天我们来学点有用的东西。\\NToday we learn." in dialogues[1]

    def test_without_secondary_is_single_line(self):
        ass = A.build_ass(self._cues(), width=1920, height=1080, style=A.SubtitleStyle())
        dialogues = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
        assert "\\N" not in dialogues[0]

    def test_unmatched_secondary_falls_back_to_plain(self):
        """时间轴对不上时不应错位拼接，宁可只显示主字幕。"""
        en = [Cue(100.0, 105.0, "Totally different timing.")]
        ass = A.build_ass(self._cues(), width=1920, height=1080, style=A.SubtitleStyle(), secondary_cues=en)
        dialogues = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
        assert "\\N" not in dialogues[0]

    def test_empty_cues_produce_valid_header_only(self):
        ass = A.build_ass([], width=1920, height=1080, style=A.SubtitleStyle())
        assert "PlayResY: 1080" in ass
        assert not [line for line in ass.splitlines() if line.startswith("Dialogue:")]

    def test_blank_cue_text_skipped(self):
        cues = [Cue(0.0, 1.0, "   "), Cue(1.0, 2.0, "有内容")]
        ass = A.build_ass(cues, width=1920, height=1080, style=A.SubtitleStyle())
        assert len([line for line in ass.splitlines() if line.startswith("Dialogue:")]) == 1


class TestActualRenderedSize:
    """端到端验证：渲染出来的字必须真的接近设定像素。"""

    @staticmethod
    def _measure(ass_path: Path, width: int, height: int) -> tuple[int, int]:
        from app.utils.binaries import resolve_binary

        ff = resolve_binary("ffmpeg")
        out = ass_path.with_suffix(".probe.png")
        subprocess.run(
            [
                ff, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:d=1",
                "-vf", f"subtitles={ass_path}", "-frames:v", "1", str(out),
            ],
            check=True,
        )
        raw = subprocess.run(
            [ff, "-hide_banner", "-loglevel", "error", "-i", str(out), "-vf", "format=gray", "-f", "rawvideo", "-"],
            capture_output=True,
        ).stdout
        rows = [y for y in range(height) if any(raw[y * width + x] > 40 for x in range(0, width, 2))]
        if not rows:
            return 0, 0
        return rows[-1] - rows[0] + 1, (height - 1 - rows[-1])

    def test_rendered_height_matches_setting(self, tmp_path):
        from app.utils.binaries import resolve_binary

        if not resolve_binary("ffmpeg"):
            pytest.skip("未安装 ffmpeg")

        target = 14
        style = A.SubtitleStyle(
            font_name="Heiti SC", font_size=target, margin_v=40, alignment="bottom", outline=1
        )
        ass = A.build_ass(
            [Cue(0.0, 5.0, "Hello 中文字幕")], width=1920, height=1080, style=style
        )
        path = tmp_path / "size.ass"
        path.write_text(ass, encoding="utf-8")

        height_px, bottom_gap = self._measure(path, 1920, 1080)
        assert height_px > 0, "没有渲染出任何字幕"
        # 字号是 em 尺寸，实际墨迹高度约为其 0.8~1.0 倍
        assert target * 0.7 <= height_px <= target * 1.4, (
            f"设定 {target}px，实际渲染 {height_px}px —— 字号没有按真实像素生效"
        )
        # 底部边距同样应是真实像素（允许行高带来的额外空隙）
        assert 20 <= bottom_gap <= 90, f"底部留白 {bottom_gap}px 与设定的 40px 差距过大"
