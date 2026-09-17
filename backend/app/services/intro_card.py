"""统一开头语的「开头画面」：生成一张科技感标题卡（SVG → PNG）。

为什么用 SVG 而不是直接画像素：标题卡要适配各种成片分辨率（原比例 / 9:16 / 16:9），
矢量渲染能保证任何尺寸下都锐利；文字用系统真实字体渲染，中文不会缺字。

渲染器按可用性降级：
  1. resvg（brew install resvg）—— 效果最好，字体与渐变都完整支持
  2. ImageMagick（magick）—— 支持 SVG，但渐变/字距略有差异
  3. Pillow 纯像素绘制 —— 保底，任何环境都能出图（渐变用逐行插值近似）

生成结果按「分辨率 + 配置指纹」缓存到 data/covers/_intro/，避免每支视频都重画。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings
from app.utils import binaries

logger = logging.getLogger(__name__)

# 设计基准尺寸：所有字号/间距都按这个宽高设计，再等比缩放到目标分辨率
_BASE_W = 1080.0
_BASE_H = 1920.0
# 内容安全区（占宽度比例），避免文字贴边
_SAFE_RATIO = 0.84
# 文字块垂直位置：略高于正中，观感更稳（纯居中时上方留白会显得空）
_VERTICAL_BIAS = 0.43
_MAX_CARD_PX = 3840  # 单边像素上限，防止 4K 竖屏生成超大 PNG

# 配色。
#
# 用户反馈「配色不好看」，实测定位到三个具体问题：
#   1. 底色太暗：原 #070b16 → #0d1730 在成图上主色亮度只有 0.007，两级渐变几乎看不出
#      差别，整张卡读起来就是「一片黑」，光晕也因为没有基底落差而糊成一团；
#   2. 青(#5eead4) + 蓝(#3b6ef5) 是 AI/科技类视频里最烂大街的一组配色，看着廉价；
#   3. 文字 #eaf2ff / #93a4c4 都偏冷，叠在冷底上整体发灰、发闷。
# 改为：底色抬到渐变可见，强调色换成暖琥珀（品牌名 EdxSpark 的 Spark 本就偏暖），
# 冷底 + 暖强调在视频里前后层次更清楚，也不容易和满屏青色科技风撞车。
# 想换方向只改下面这几行即可，例如冷色系：_ACCENT="#60a5fa"、_ACCENT_2="#a78bfa"。
_BG_TOP = "#131e3a"
_BG_MID = "#0d1628"
_BG_BOTTOM = "#080d18"
# 底色柔光专用色：只做深浅层次，刻意与底色同族。
# 事故：一开始把暖色光晕铺在冷底上，两种色相混合后去饱和——实测右上角变成
# #292829 的中性灰、左下角变成 #301f22 的暗红褐，色相全丢了，画面发闷。
# 结论：柔光只用同族色，"暖"要靠实心色块去给，不要靠混色。
_GLOW = "#2f5bd0"
_ACCENT = "#ffb454"  # 琥珀金
_ACCENT_2 = "#ff7a59"  # 暖橙，与琥珀同族，渐变不脏
_TEXT = "#f7f9ff"
_TEXT_DIM = "#b3c0d8"

# 可用版式：hero（左对齐片头）/ frame（居中画框）/ band（竖排强调块）
LAYOUTS = ("hero", "frame", "band")
DEFAULT_LAYOUT = "hero"

_TITLE_DEFAULT = "欢迎来到 EdxSpark"
_SUBTITLE_DEFAULT = "AI 创业 · 工作 · 创新 · 学习"


@dataclass
class CardResult:
    path: Path
    width: int
    height: int
    renderer: str
    lines: list[str]
    title_size: float = 0.0
    layout: str = DEFAULT_LAYOUT


def _resolve_font(preferred: str = "") -> str:
    """挑一个系统里真实存在的中文字体（复用成片字幕的探测结果）。"""
    from app.utils import ffmpeg as ffmpeg_utils

    return ffmpeg_utils.resolve_subtitle_font(preferred)


def _char_width(ch: str, size: float) -> float:
    """粗略字宽：中日韩字符按 1em，其余按 0.55em 估算（只用于自动字号与换行）。"""
    code = ord(ch)
    wide = (
        0x1100 <= code <= 0x115F
        or 0x2E80 <= code <= 0xA4CF
        or 0xAC00 <= code <= 0xD7A3
        or 0xF900 <= code <= 0xFAFF
        or 0xFE30 <= code <= 0xFE4F
        or 0xFF00 <= code <= 0xFF60
        or 0xFFE0 <= code <= 0xFFE6
    )
    return size * (1.0 if wide else 0.56)


def text_width(text: str, size: float) -> float:
    return sum(_char_width(ch, size) for ch in text)


def _wrap(text: str, size: float, max_width: float) -> list[str]:
    """按宽度限制折行：优先在空格处断，CJK 允许逐字断。

    注意不能产出空行：空行会让「最多 2 行」的判断提前满足，标题于是不再自动缩小，
    结果就是文字溢出画面（实测长标题时出现）。
    """
    text = " ".join(str(text or "").split())
    if not text:
        return []
    lines: list[str] = []
    current = ""
    for ch in text:
        candidate = current + ch
        if current and text_width(candidate, size) > max_width:
            if ch.isalnum() and " " in current:
                # 英文单词尽量不从中间断开；若断点处没有可用内容则按字断
                head, _, tail = current.rpartition(" ")
                if head.strip():
                    lines.append(head)
                    current = (tail + ch).lstrip()
                else:
                    lines.append(current)
                    current = ch
            else:
                lines.append(current)
                current = ch
        else:
            current = candidate
    if current:
        lines.append(current)
    return [line for line in lines if line.strip()]


def _fit_size(text: str, *, max_width: float, start: float, min_size: float, max_lines: int = 2) -> tuple[float, list[str]]:
    """从 start 号字开始逐档缩小，直到能在 max_lines 行内放下。"""
    size = start
    while size > min_size:
        lines = _wrap(text, size, max_width)
        if len(lines) <= max_lines:
            return size, lines
        size -= 2
    return min_size, _wrap(text, min_size, max_width)


def title_layout(title: str, width: int) -> tuple[float, list[str]]:
    """算标题的字号与折行（自动缩小到安全区内）。"""
    return _fit_size(
        title or _TITLE_DEFAULT,
        max_width=width * _SAFE_RATIO,
        start=118 * min(1.0, width / _BASE_W) if width < _BASE_W else 118.0,
        min_size=44 * min(1.0, width / _BASE_W) if width < _BASE_W else 44.0,
        max_lines=2,
    )


# --------------------------------------------------------------------------------------
# 三种版式
#
# 都用同一套内容（主标题 / 副标题 / 品牌名），但构图完全不同，便于按品牌调性挑选：
#   hero  —— 左下重心的大标题 + 顶部强调条 + 底部进度轨（默认，最像"频道片头"）
#   frame —— 居中大标题 + 多层细线画框 + 细腻光晕（稳、贵、适合知识类）
#   band  —— 左侧竖排强调条 + 右下错落的标题块（最"设计感"，适合品牌片头）
# --------------------------------------------------------------------------------------

def _esc(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _type_scale(height: int) -> dict[str, float]:
    """按画面高度推导各级字号。

    为什么不能再用原来的 `scale = min(width/1080, height/1920)`：
    基数是竖屏 1080×1920，遇到横屏 1920×1080 时这个系数取 min(1.78, 0.5625) = 0.5625，
    **所有字号被整体压到 56%**——实测成图上左上角品牌字只有 16px、副标题 22.5px，
    分别只占画面高的 1.5% 和 2.1%，在手机上看基本读不出来。

    字号只应与**画面高度**相关（观众看到的是画面高度映射到屏幕高度），
    并且取值要比原设计更大：实测原来是连竖屏都偏小。
    上限用 1920 兜底，避免竖屏 4K 时字大到溢出安全区。
    """
    h = min(float(height), 1920.0)
    return {
        # 左上角品牌字：画面高的 3%，至少 24px
        "eyebrow": max(24.0, min(0.030 * h, 58.0)),
        # 副标题：4.8%，至少 34px（原来只有 2.1%，是用户点名的「太小」）
        "subtitle": max(34.0, min(0.048 * h, 88.0)),
        # 主标题起始 / 下限
        "title": max(72.0, min(0.115 * h, 200.0)),
        "title_min": max(46.0, min(0.066 * h, 104.0)),
    }


def _defs(scale: float, width: int, height: int) -> str:
    """公共 defs：底色渐变、柔光、强调色渐变、分隔线。

    这里删掉了原来的网格纹理（每 120px 一格的浅蓝细线）：它在成片上只是噪点，
    而且是「科技风模板」最明显的标志之一，正是用户觉得难看的原因之一。
    """
    return (
        "<defs>"
        '<linearGradient id="bg" x1="0.08" y1="0" x2="0.92" y2="1">'
        f'<stop offset="0" stop-color="{_BG_TOP}"/>'
        f'<stop offset="0.52" stop-color="{_BG_MID}"/>'
        f'<stop offset="1" stop-color="{_BG_BOTTOM}"/>'
        "</linearGradient>"
        '<linearGradient id="bg2" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{_BG_TOP}"/>'
        f'<stop offset="1" stop-color="{_BG_BOTTOM}"/>'
        "</linearGradient>"
        '<radialGradient id="glowA" cx="0.5" cy="0.5" r="0.5">'
        f'<stop offset="0" stop-color="{_GLOW}" stop-opacity="0.46"/>'
        f'<stop offset="0.55" stop-color="{_GLOW}" stop-opacity="0.15"/>'
        f'<stop offset="1" stop-color="{_GLOW}" stop-opacity="0"/>'
        "</radialGradient>"
        '<radialGradient id="glowB" cx="0.5" cy="0.5" r="0.5">'
        f'<stop offset="0" stop-color="{_GLOW}" stop-opacity="0.26"/>'
        f'<stop offset="0.5" stop-color="{_GLOW}" stop-opacity="0.07"/>'
        f'<stop offset="1" stop-color="{_GLOW}" stop-opacity="0"/>'
        "</radialGradient>"
        '<linearGradient id="accentBar" x1="0" y1="0" x2="1" y2="0">'
        f'<stop offset="0" stop-color="{_ACCENT}"/>'
        f'<stop offset="1" stop-color="{_ACCENT_2}"/>'
        "</linearGradient>"
        '<linearGradient id="line" x1="0" y1="0" x2="1" y2="0">'
        f'<stop offset="0" stop-color="{_TEXT}" stop-opacity="0.55"/>'
        f'<stop offset="1" stop-color="{_TEXT}" stop-opacity="0.08"/>'
        "</linearGradient>"
        # 上下压暗：让画面有收束感、文字块不「浮」在中间。
        # 实测 0.42 会把右下角压到亮度 0.0034（比修改前的一片死黑还黑），所以减半。
        '<linearGradient id="vignette" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#000000" stop-opacity="0.16"/>'
        '<stop offset="0.24" stop-color="#000000" stop-opacity="0"/>'
        '<stop offset="0.76" stop-color="#000000" stop-opacity="0"/>'
        '<stop offset="1" stop-color="#000000" stop-opacity="0.26"/>'
        "</linearGradient>"
        "</defs>"
    )


def _title_font(
    *,
    anchor: str,
    size: float,
    font: str,
    fill: str,
    scale: float,
    bold: bool = True,
    letter_spacing: float | None = None,
) -> str:
    """拼接 <text> 的字体属性。

    字距只能出现一次：SVG 属性重复（例如调用方又传一个 letter-spacing）会让 resvg
    直接报错 "attribute already defined"，整张卡片退化成保底绘制。
    """
    weight = ' font-weight="700"' if bold else ""
    spacing = max(0.0, 0.6 * scale) if letter_spacing is None else max(0.0, letter_spacing)
    return (
        f'font-family="{_esc(font)}" font-size="{size:.1f}"{weight} fill="{fill}" '
        f'text-anchor="{anchor}" letter-spacing="{spacing:.2f}"'
    )


def _layout_hero(
    *, width: int, height: int, title: str, subtitle: str, font: str, brand: str, scale: float
) -> list[str]:
    """左对齐的大标题片头：暖色强调条 + 品牌字，左下标题块，底部进度轨。

    这一版重排的要点（对应「难看」+「字太小」两条反馈）：
      - 去掉网格纹理与两处大光晕：它们叠加后只是在深色上糊出一片脏雾；
        改成「单一柔光 + 上下压暗」，画面干净、中心有落点；
      - 三级字号的差距重新拉开（品牌 3% / 副标题 4.8% / 标题 11.5% 画面高），
        层级靠字号和字重区分，不再靠压暗颜色——压暗只会显得脏；
      - 强调条从「细线」加粗成有存在感的色块，作为唯一的暖色落点。
    """
    t = _type_scale(height)
    margin = width * 0.085
    parts: list[str] = []

    # 背景：抬亮的深墨蓝渐变 + 柔光 + 上下压暗。
    # 主柔光刻意压在**标题块后面**（而不是画面角落）：实测原来的排法让文字落在
    # 全画面最暗处（亮度 0.0087），四个角反而更亮，层次是反的——文字像陷进一个黑洞。
    # 让文字坐在一块微微抬亮的底上，既拉开层次，又不用加色块或描边。
    parts.append(f'<rect width="{width}" height="{height}" fill="url(#bg)"/>')
    parts.append(
        f'<ellipse cx="{width * 0.30:.0f}" cy="{height * 0.54:.0f}" rx="{width * 0.56:.0f}" '
        f'ry="{height * 0.42:.0f}" fill="url(#glowA)"/>'
    )
    parts.append(
        f'<ellipse cx="{width * 0.88:.0f}" cy="{height * 0.12:.0f}" rx="{width * 0.34:.0f}" '
        f'ry="{height * 0.22:.0f}" fill="url(#glowB)"/>'
    )
    parts.append(f'<rect width="{width}" height="{height}" fill="url(#vignette)"/>')

    # 顶部：一块强调色块 + 品牌字（字距拉开，读起来像刊头）
    top_y = height * 0.115
    bar_w = 58 * (t["eyebrow"] / 32.0)
    bar_h = max(5.0, t["eyebrow"] * 0.30)
    parts.append(
        f'<rect x="{margin:.1f}" y="{top_y - bar_h * 0.62:.1f}" width="{bar_w:.1f}" '
        f'height="{bar_h:.1f}" rx="{bar_h * 0.32:.2f}" fill="url(#accentBar)"/>'
    )
    parts.append(
        f'<text x="{margin + bar_w + t["eyebrow"] * 0.72:.1f}" '
        f'y="{top_y + t["eyebrow"] * 0.34:.1f}" '
        f'{_title_font(anchor="start", size=t["eyebrow"], font=font, fill=_TEXT, scale=scale, bold=True, letter_spacing=t["eyebrow"] * 0.22)}>{_esc(brand.upper())}</text>'
    )

    title_size, title_lines = _fit_size(
        title or _TITLE_DEFAULT,
        max_width=width - margin * 2,
        start=t["title"],
        min_size=t["title_min"],
    )
    sub_size, sub_lines = _fit_size(
        subtitle or _SUBTITLE_DEFAULT,
        max_width=width - margin * 2,
        start=t["subtitle"],
        min_size=t["subtitle"] * 0.82,
        max_lines=2,
    )

    title_lh = title_size * 1.16
    gap = title_size * 0.34
    rule_h = max(3.0, title_size * 0.035)
    block_h = (
        len(title_lines) * title_lh
        + gap
        + rule_h
        + sub_size * 1.1
        + len(sub_lines) * sub_size * 1.42
    )
    y = height * 0.555 - block_h / 2

    for line in title_lines:
        parts.append(
            f'<text x="{margin:.1f}" y="{y:.1f}" '
            f'{_title_font(anchor="start", size=title_size, font=font, fill=_TEXT, scale=scale)}>'
            f"{_esc(line)}</text>"
        )
        y += title_lh

    y += gap
    # 标题与副标题之间用暖色短线分隔：唯一的暖色落点，代替原来那片浑浊的光晕
    parts.append(
        f'<rect x="{margin:.1f}" y="{y:.1f}" width="{width * 0.12:.1f}" '
        f'height="{rule_h:.2f}" rx="{rule_h / 2:.2f}" fill="url(#accentBar)"/>'
    )
    y += rule_h + sub_size * 1.1
    for line in sub_lines:
        parts.append(
            f'<text x="{margin:.1f}" y="{y:.1f}" '
            f'{_title_font(anchor="start", size=sub_size, font=font, fill=_TEXT_DIM, scale=scale, bold=False, letter_spacing=sub_size * 0.06)}>{_esc(line)}</text>'
        )
        y += sub_size * 1.42

    # 底部进度轨：暗示「正在开始」，也让静止画面有方向感
    track_y = height * 0.875
    track_h = max(3.0, height * 0.005)
    parts.append(
        f'<rect x="{margin:.1f}" y="{track_y:.1f}" width="{width - margin * 2:.1f}" '
        f'height="{track_h:.2f}" rx="{track_h / 2:.2f}" fill="{_TEXT}" opacity="0.16"/>'
    )
    parts.append(
        f'<rect x="{margin:.1f}" y="{track_y:.1f}" width="{(width - margin * 2) * 0.32:.1f}" '
        f'height="{track_h:.2f}" rx="{track_h / 2:.2f}" fill="url(#accentBar)"/>'
    )
    return parts


def _layout_frame(
    *, width: int, height: int, title: str, subtitle: str, font: str, brand: str, scale: float
) -> list[str]:
    """居中构图 + 多层细线画框：稳、干净，像知识类频道的片头。"""
    t = _type_scale(height)
    parts: list[str] = []
    parts.append(f'<rect width="{width}" height="{height}" fill="url(#bg2)"/>')
    parts.append(
        f'<ellipse cx="{width / 2:.0f}" cy="{height * 0.44:.0f}" rx="{width * 0.66:.0f}" '
        f'ry="{height * 0.3:.0f}" fill="url(#glowA)"/>'
    )
    parts.append(
        f'<ellipse cx="{width / 2:.0f}" cy="{height * 0.62:.0f}" rx="{width * 0.5:.0f}" '
        f'ry="{height * 0.22:.0f}" fill="url(#glowB)"/>'
    )
    parts.append(f'<rect width="{width}" height="{height}" fill="url(#vignette)"/>')

    # 三层同心细框：最外层几乎不可见，向内逐渐清晰
    inset = width * 0.06
    for _index, (alpha, pad) in enumerate(((0.10, 0.0), (0.18, 26 * scale), (0.30, 52 * scale))):
        x = inset + pad
        w = width - (inset + pad) * 2
        h = height - (inset + pad) * 2
        parts.append(
            f'<rect x="{x:.1f}" y="{inset + pad:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'rx="{26 * scale:.1f}" fill="none" stroke="{_ACCENT}" stroke-opacity="{alpha}" '
            f'stroke-width="{max(1.0, 1.6 * scale):.2f}"/>'
        )

    # 菱形标记：居中上方，替代俗套的四角括号
    diamond_y = height * 0.3
    d = 15 * scale
    parts.append(
        f'<path d="M {width / 2:.1f} {diamond_y - d:.1f} L {width / 2 + d:.1f} {diamond_y:.1f} '
        f'L {width / 2:.1f} {diamond_y + d:.1f} L {width / 2 - d:.1f} {diamond_y:.1f} Z" '
        f'fill="{_ACCENT}"/>'
    )

    margin = width * 0.12
    title_size, title_lines = _fit_size(
        title or _TITLE_DEFAULT, max_width=width - margin * 2, start=t["title"], min_size=t["title_min"]
    )
    sub_size, sub_lines = _fit_size(
        subtitle or _SUBTITLE_DEFAULT, max_width=width - margin * 2, start=t["subtitle"], min_size=t["subtitle"] * 0.82, max_lines=2
    )

    title_lh = title_size * 1.24
    block_h = len(title_lines) * title_lh + 56 * scale + len(sub_lines) * sub_size * 1.5
    y = height * 0.52 - block_h / 2 + title_size * 0.85

    for line in title_lines:
        parts.append(
            f'<text x="{width / 2:.1f}" y="{y:.1f}" '
            f'{_title_font(anchor="middle", size=title_size, font=font, fill=_TEXT, scale=scale)}>'
            f"{_esc(line)}</text>"
        )
        y += title_lh

    # 标题下的对称短分隔（两侧各一段，中间留白）
    sep_y = y + 6 * scale
    gap = 12 * scale
    seg = width * 0.14
    for sign in (-1, 1):
        x = width / 2 + sign * gap if sign > 0 else width / 2 - gap - seg
        parts.append(
            f'<rect x="{x:.1f}" y="{sep_y:.1f}" width="{seg:.1f}" '
            f'height="{max(1.6, 2.2 * scale):.2f}" fill="url(#accentBar)" opacity="0.8"/>'
        )

    y = sep_y + 48 * scale
    for line in sub_lines:
        parts.append(
            f'<text x="{width / 2:.1f}" y="{y:.1f}" '
            f'{_title_font(anchor="middle", size=sub_size, font=font, fill=_TEXT_DIM, scale=scale, bold=False, letter_spacing=3 * scale)}>{_esc(line)}</text>'
        )
        y += sub_size * 1.5

    parts.append(
        f'<text x="{width / 2:.1f}" y="{height - width * 0.06 - 30 * scale:.1f}" '
        f'{_title_font(anchor="middle", size=t["eyebrow"], font=font, fill=_TEXT, scale=scale, bold=True, letter_spacing=t["eyebrow"] * 0.22)} '
        f'opacity="0.75">{_esc(brand.upper())}</text>'
    )
    return parts


def _layout_band(
    *, width: int, height: int, title: str, subtitle: str, font: str, brand: str, scale: float
) -> list[str]:
    """左侧竖排强调块 + 右下错落标题：设计感最强，适合品牌片头。"""
    t = _type_scale(height)
    parts: list[str] = []
    parts.append(f'<rect width="{width}" height="{height}" fill="url(#bg)"/>')

    # 右上大面积深色斜切块，制造前后层次
    parts.append(
        f'<path d="M {width:.0f} 0 L {width:.0f} {height * 0.42:.0f} '
        f'L {width * 0.34:.0f} 0 Z" fill="#0e1c3c" opacity="0.55"/>'
    )
    parts.append(
        f'<ellipse cx="{width * 0.86:.0f}" cy="{height * 0.72:.0f}" rx="{width * 0.6:.0f}" '
        f'ry="{height * 0.24:.0f}" fill="url(#glowA)"/>'
    )
    parts.append(
        f'<ellipse cx="{width * 0.2:.0f}" cy="{height * 0.3:.0f}" rx="{width * 0.5:.0f}" '
        f'ry="{height * 0.2:.0f}" fill="url(#glowB)"/>'
    )
    parts.append(f'<rect width="{width}" height="{height}" fill="url(#vignette)"/>')

    # 左侧竖排强调块：粗色块 + 竖排品牌名
    margin = width * 0.085
    block_w = max(6.0, 10 * scale)
    block_y = height * 0.2
    block_h = height * 0.3
    parts.append(
        f'<rect x="{margin:.1f}" y="{block_y:.1f}" width="{block_w:.1f}" height="{block_h:.1f}" '
        f'rx="{block_w / 2:.2f}" fill="url(#accentBar)"/>'
    )

    title_size, title_lines = _fit_size(
        title or _TITLE_DEFAULT, max_width=width * 0.72, start=t["title"] * 0.94, min_size=t["title_min"]
    )
    sub_size, sub_lines = _fit_size(
        subtitle or _SUBTITLE_DEFAULT, max_width=width * 0.72, start=t["subtitle"], min_size=t["subtitle"] * 0.82, max_lines=2
    )

    title_lh = title_size * 1.2
    block_h_title = len(title_lines) * title_lh + len(sub_lines) * sub_size * 1.55
    x = margin + block_w + 46 * scale
    y = height * 0.66 - block_h_title

    for line in title_lines:
        parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" '
            f'{_title_font(anchor="start", size=title_size, font=font, fill=_TEXT, scale=scale)}>'
            f"{_esc(line)}</text>"
        )
        y += title_lh

    y += 14 * scale
    for line in sub_lines:
        parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" '
            f'{_title_font(anchor="start", size=sub_size, font=font, fill=_TEXT_DIM, scale=scale, bold=False, letter_spacing=1.4 * scale)}>{_esc(line)}</text>'
        )
        y += sub_size * 1.55

    # 品牌字：右上角小字，与左侧竖排块形成对角呼应
    parts.append(
        f'<text x="{width - margin:.1f}" y="{height * 0.115:.1f}" '
        f'{_title_font(anchor="end", size=t["eyebrow"], font=font, fill=_TEXT, scale=scale, bold=True, letter_spacing=t["eyebrow"] * 0.22)}>{_esc(brand.upper())}</text>'
    )
    parts.append(
        f'<rect x="{width - margin - 70 * scale:.1f}" y="{height * 0.14:.1f}" '
        f'width="{70 * scale:.1f}" height="{max(1.6, 2.4 * scale):.2f}" '
        f'fill="url(#accentBar)" opacity="0.8"/>'
    )
    return parts


_LAYOUT_FUNCS = {"hero": _layout_hero, "frame": _layout_frame, "band": _layout_band}


def build_svg(
    *,
    width: int,
    height: int,
    title: str,
    subtitle: str,
    font_family: str,
    brand: str = "EdxSpark",
    layout: str = DEFAULT_LAYOUT,
    progress: float = 0.32,
) -> str:
    """按选定版式生成标题卡 SVG。"""
    layout = layout if layout in _LAYOUT_FUNCS else DEFAULT_LAYOUT
    scale = min(width / _BASE_W, height / _BASE_H)
    body = _LAYOUT_FUNCS[layout](
        width=width, height=height, title=title, subtitle=subtitle,
        font=font_family, brand=brand, scale=scale,
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">' + _defs(scale, width, height) + "".join(body) + "</svg>"
    )


def _render_svg_png(svg_text: str, out_path: Path, width: int, height: int) -> str:
    """把 SVG 渲染成 PNG，返回使用的渲染器名；失败抛 RuntimeError。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    svg_path = out_path.with_suffix(".svg")
    svg_path.write_text(svg_text, encoding="utf-8")

    resvg = binaries.resolve_binary("resvg")
    if resvg:
        result = subprocess.run(
            [resvg, "--width", str(width), "--height", str(height), str(svg_path), str(out_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0 and out_path.exists():
            return "resvg"
        logger.warning("resvg 渲染失败：%s", (result.stderr or "").strip()[:200])

    magick = binaries.resolve_binary("magick")
    if magick:
        result = subprocess.run(
            [magick, "-background", "none", str(svg_path), "-resize", f"{width}x{height}!", str(out_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0 and out_path.exists():
            return "magick"
        logger.warning("ImageMagick 渲染失败：%s", (result.stderr or "").strip()[:200])

    raise RuntimeError("没有可用的 SVG 渲染器")


def _render_pillow_fallback(
    out_path: Path,
    *,
    width: int,
    height: int,
    title: str,
    subtitle: str,
    font_family: str,
) -> str:
    """保底渲染（无 resvg / ImageMagick 时）：纯像素渐变 + 文字，观感略朴素但可用。"""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (width, height), _BG_TOP)
    draw = ImageDraw.Draw(image)
    # 竖直渐变（逐行插值，避免依赖 numpy）
    top = tuple(int(_BG_TOP[i : i + 2], 16) for i in (1, 3, 5))
    bottom = tuple(int(_BG_BOTTOM[i : i + 2], 16) for i in (1, 3, 5))
    for y in range(height):
        ratio = y / max(1, height - 1)
        draw.line(
            [(0, y), (width, y)],
            fill=tuple(int(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3)),
        )

    def load_font(size: int):
        for name in (font_family, "Heiti SC", "Hiragino Sans GB", "Arial Unicode MS", "PingFang SC"):
            try:
                return ImageFont.truetype(f"/System/Library/Fonts/{name}.ttc", size)
            except Exception:  # noqa: BLE001 - 找不到就换下一个
                continue
        try:
            return ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", size)
        except Exception:  # noqa: BLE001
            return ImageFont.load_default()

    scale = min(width / _BASE_W, height / _BASE_H)
    safe = width * _SAFE_RATIO
    t = _type_scale(height)
    title_size, title_lines = _fit_size(title or _TITLE_DEFAULT, max_width=safe, start=t["title"], min_size=t["title_min"])
    sub_size, sub_lines = _fit_size(
        subtitle or _SUBTITLE_DEFAULT, max_width=safe, start=t["subtitle"], min_size=t["subtitle"] * 0.82, max_lines=3
    )
    title_font = load_font(int(title_size))
    sub_font = load_font(int(sub_size))

    total = len(title_lines) * title_size * 1.28 + 34 * scale + len(sub_lines) * sub_size * 1.5
    y = (height - total) / 2
    for line in title_lines:
        w = draw.textlength(line, font=title_font)
        draw.text(((width - w) / 2, y), line, font=title_font, fill=_TEXT)
        y += title_size * 1.28
    y += 34 * scale
    for line in sub_lines:
        w = draw.textlength(line, font=sub_font)
        draw.text(((width - w) / 2, y), line, font=sub_font, fill=_TEXT_DIM)
        y += sub_size * 1.5

    # 装饰线
    bar_w = min(safe * 0.5, 400 * scale)
    draw.line(
        [((width - bar_w) / 2, (height - total) / 2 - title_size * 0.7),
         ((width + bar_w) / 2, (height - total) / 2 - title_size * 0.7)],
        fill=_ACCENT,
        width=max(2, int(4 * scale)),
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path)
    return "pillow"


# 版式「设计版本」。
#
# 必须参与缓存 key：缓存只按尺寸/文案/字体/版式做键，不含配色与构图。
# 结果就是——改了设计、重新跑任务，画面却还是旧的那张（命中旧缓存），
# 用户会以为改动没生效。**每次改动配色或构图都要把这个数字 +1。**
_DESIGN_VERSION = 2


def _cache_key(width: int, height: int, title: str, subtitle: str, font: str, brand: str, layout: str) -> str:
    raw = f"v{_DESIGN_VERSION}|{width}x{height}|{title}|{subtitle}|{font}|{brand}|{layout}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _card_dir() -> Path:
    return settings.covers_dir / "_intro"


def _custom_image_usable(raw: str) -> Path | None:
    path = Path(str(raw or "").strip()).expanduser()
    if not raw or not path.is_file():
        return None
    return path


def build_card(
    *,
    width: int,
    height: int,
    config: dict,
    force: bool = False,
) -> CardResult:
    """生成（或复用缓存的）开头标题卡。

    config 为 intro 分组配置；custom_image 指向用户自备图片时直接使用该图。
    """
    width = max(2, min(int(width), _MAX_CARD_PX))
    height = max(2, min(int(height), _MAX_CARD_PX))

    custom_raw = str(config.get("card_image") or "").strip()
    custom = _custom_image_usable(custom_raw)
    if custom_raw and custom is None:
        logger.warning("开头画面指定的图片不存在，已回退为自动生成：%s", custom_raw)

    title = str(config.get("card_title") or _TITLE_DEFAULT).strip()
    subtitle = str(config.get("card_subtitle") or _SUBTITLE_DEFAULT).strip()
    brand = str(config.get("card_brand") or "EdxSpark").strip()
    font = _resolve_font(str(config.get("card_font") or ""))
    layout = str(config.get("card_layout") or DEFAULT_LAYOUT).strip().lower()
    if layout not in _LAYOUT_FUNCS:
        layout = DEFAULT_LAYOUT

    result = CardResult(
        path=Path(),
        width=width,
        height=height,
        renderer="custom" if custom else "",
        lines=[],
    )

    if custom is not None:
        # 用户自备图：原样使用，由 ffmpeg 侧负责缩放与留边
        result.path = custom
        return result

    key = _cache_key(width, height, title, subtitle, font, brand, layout)
    target = _card_dir() / f"intro_{width}x{height}_{key}.png"
    if target.exists() and target.stat().st_size > 0 and not force:
        result.path = target
        result.renderer = "cache"
        size, lines = title_layout(title, width)
        result.lines = lines or [title]
        result.title_size = round(size, 1)
        return result

    svg_text = build_svg(
        width=width,
        height=height,
        title=title,
        subtitle=subtitle,
        font_family=font,
        brand=brand,
        layout=layout,
    )
    try:
        renderer = _render_svg_png(svg_text, target, width, height)
    except Exception as exc:  # noqa: BLE001 - 渲染器缺失时降级，不让成片流程失败
        logger.warning("SVG 渲染不可用（%s），改用保底绘制", exc)
        renderer = _render_pillow_fallback(
            target,
            width=width,
            height=height,
            title=title,
            subtitle=subtitle,
            font_family=font,
        )

    result.path = target
    result.renderer = renderer
    size, lines = title_layout(title, width)
    result.lines = lines or [title]
    result.title_size = round(size, 1)
    return result


async def build_card_async(*, width: int, height: int, config: dict, force: bool = False) -> CardResult:
    return await asyncio.to_thread(build_card, width=width, height=height, config=config, force=force)
