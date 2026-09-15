"""ASS 字幕生成。

为什么要自己生成 ASS 而不是直接烧 SRT：
    ffmpeg 的 subtitles 滤镜让 libass 解析 SRT 时，会使用 ASS 的默认坐标系
    （PlayResY=288）。于是 force_style 里的 FontSize / MarginV 会被整体放大
    约「画布高度 / 288」倍——实测在 1920 高的画面上，FontSize=20 渲染出 123px，
    MarginV=60 变成 404px 的底部留白。字号设置完全不可预期。

这里显式生成带 PlayResX / PlayResY 的 ASS，让 PlayRes 等于成片分辨率，
于是 FontSize 就是真实像素、MarginV 就是真实边距，所见即所得。

同时支持双语：同一条对话里用 \\N 分成两行，中文在上更符合阅读习惯。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.subtitles import Cue

# 1080p 基准：字号按「输出高度 / 该基准」等比缩放，
# 这样同一套设置在不同分辨率的成片上观感一致。
REFERENCE_HEIGHT = 1080

ALIGNMENT_MAP = {
    "bottom": 2,   # 底部居中
    "middle": 5,   # 垂直居中
    "top": 8,      # 顶部居中
}


@dataclass
class SubtitleStyle:
    font_name: str = "Heiti SC"
    # 1080p 基准下的字号（像素）
    font_size: int = 14
    # 同基准下的底部边距（像素）
    margin_v: int = 40
    alignment: str = "bottom"
    outline: int = 1
    shadow: int = 0
    primary_colour: str = "&H00FFFFFF"   # 白字
    outline_colour: str = "&H00000000"   # 黑边
    bold: bool = False

    def scaled_font_size(self, height: int) -> int:
        return max(8, round(self.font_size * height / REFERENCE_HEIGHT))

    def scaled_margin(self, height: int) -> int:
        return max(0, round(self.margin_v * height / REFERENCE_HEIGHT))


def _ass_timestamp(seconds: float) -> str:
    """ASS 时间格式 H:MM:SS.cc（厘秒）。"""
    seconds = max(0.0, seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    whole = int(secs)
    centis = int(round((secs - whole) * 100))
    if centis >= 100:  # 四舍五入进位
        whole += 1
        centis = 0
    if whole >= 60:
        minutes += 1
        whole = 0
    return f"{int(hours)}:{int(minutes):02d}:{whole:02d}.{centis:02d}"


def escape_ass_text(text: str) -> str:
    """转义 ASS 对话文本。

    - 换行必须写成 \\N；原始换行会让 ASS 解析器把下一行当成新指令
    - { } 是样式覆盖标签的定界符，必须转义，否则字幕文本里的花括号会被吃掉
    """
    cleaned = (text or "").replace("\r", " ").replace("\n", " ")
    cleaned = cleaned.replace("\\", "\\\\")
    cleaned = cleaned.replace("{", "\\{").replace("}", "\\}")
    return cleaned.strip()


def build_ass(
    cues: list[Cue],
    *,
    width: int,
    height: int,
    style: SubtitleStyle,
    secondary_cues: list[Cue] | None = None,
    primary_first: bool = False,
) -> str:
    """生成 ASS 字幕文件内容。

    cues：主字幕（默认中文）。secondary_cues：次字幕（英文），按时间戳匹配后
    与主字幕合并到同一条对话的两行；为 None 或匹配不到时只输出主字幕。
    """
    width = max(2, int(width))
    height = max(2, int(height))
    alignment = ALIGNMENT_MAP.get(style.alignment, 2)
    font_size = style.scaled_font_size(height)
    margin_v = style.scaled_margin(height)

    header = f"""[Script Info]
; 由 Spark Video Tools 生成
; PlayRes 显式设为成片分辨率，因此 FontSize / MarginV 都是真实像素
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.601

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{style.font_name},{font_size},{style.primary_colour},&H000000FF,{style.outline_colour},&H00000000,{-1 if style.bold else 0},0,0,0,100,100,0,0,1,{style.outline},{style.shadow},{alignment},40,40,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines: list[str] = []
    for index, cue in enumerate(cues):
        primary = escape_ass_text(cue.text)
        if not primary:
            continue

        secondary = ""
        if secondary_cues:
            match = _match_secondary(cue, secondary_cues, index)
            if match:
                secondary = escape_ass_text(match)

        if secondary:
            # 中文在上、英文在下：中文是母语读者优先获取的信息
            body = f"{primary}\\N{secondary}" if primary_first else f"{primary}\\N{secondary}"
        else:
            body = primary

        lines.append(
            f"Dialogue: 0,{_ass_timestamp(cue.start)},{_ass_timestamp(cue.end)},Default,,0,0,0,,{body}"
        )

    return header + "\n".join(lines) + ("\n" if lines else "")


def _match_secondary(cue: Cue, secondary: list[Cue], index: int) -> str:
    """按时间戳给主字幕找对应的次字幕。

    正常情况下两条轨道一一对应（翻译阶段保持了时间轴），因此优先按序号取；
    若时间轴能对上则用时间匹配，避免字幕条数不一致时整体错位。
    """
    tolerance = 0.35
    best: Cue | None = None
    best_delta = float("inf")
    for other in secondary:
        delta = abs(other.start - cue.start)
        if delta <= tolerance and delta < best_delta:
            best, best_delta = other, delta
    if best is not None:
        return best.text
    if 0 <= index < len(secondary) and abs(secondary[index].start - cue.start) <= 2.0:
        return secondary[index].text
    return ""


def output_resolution(source_width: int, source_height: int, target_aspect: str) -> tuple[int, int]:
    """计算成片的实际分辨率，供生成字幕时的 PlayRes 使用。"""
    if target_aspect == "9:16":
        return 1080, 1920
    if target_aspect == "16:9":
        return 1920, 1080
    return max(2, source_width), max(2, source_height)
