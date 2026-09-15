"""封面生成：把视频帧做成一张有设计感的封面图。

设计取向（用户要求「通用、简洁、科技、美观」）：
    1. 底图取视频自身的一帧并重度模糊 —— 每张封面都带着视频本身的色调，
       不是千篇一律的模板底色；
    2. 压一层深色渐变遮罩，保证任何画面上文字都清晰可读；
    3. 科技感来自克制的元素：细网格、左上角品牌条、标题左侧的强调色块、
       标签用描边胶囊 —— 不加花哨特效，避免喧宾夺主；
    4. 标题自动折行并自适应字号，标签最多展示若干个，超出以 +N 表示。

输出尺寸默认 1080x1920（抖音竖版封面），也支持 16:9 与 1:1。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from app.utils.binaries import resolve_binary

logger = logging.getLogger(__name__)

# 主题：底色渐变 + 强调色
THEMES: dict[str, dict] = {
    "tech_blue": {
        "top": (10, 16, 32),
        "bottom": (24, 46, 96),
        "accent": (64, 158, 255),
        "accent2": (127, 90, 240),
    },
    "tech_dark": {
        "top": (8, 10, 14),
        "bottom": (26, 32, 44),
        "accent": (0, 224, 184),
        "accent2": (86, 204, 242),
    },
    "minimal": {
        "top": (18, 18, 20),
        "bottom": (38, 38, 42),
        "accent": (240, 240, 245),
        "accent2": (170, 170, 180),
    },
}

# 候选中文字体：优先思源黑体（有 Bold 字重），其次系统黑体
_FONT_CANDIDATES = (
    ("~/Library/Fonts/SourceHanSansCN-Bold.otf", 0),
    ("~/Library/Fonts/SourceHanSansCN-Medium.otf", 0),
    ("/System/Library/Fonts/STHeiti Medium.ttc", 0),
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),
    ("/Library/Fonts/Arial Unicode.ttf", 0),
    ("/System/Library/Fonts/Supplemental/Songti.ttc", 0),
)


@dataclass
class CoverStyle:
    """封面样式。字号均以 1080x1920 为基准，按实际尺寸等比缩放。"""

    width: int = 1080
    height: int = 1920
    theme: str = "tech_blue"
    title_size: int = 76
    tag_size: int = 30
    brand: str = "AI 译制"
    max_tags: int = 4
    show_grid: bool = True
    blur_radius: int = 42
    overlay_alpha: int = 205
    title: str = ""
    tags: list[str] = field(default_factory=list)


def _load_font(size: int):
    from PIL import ImageFont

    for raw, index in _FONT_CANDIDATES:
        path = Path(raw).expanduser()
        if not path.exists():
            continue
        try:
            return ImageFont.truetype(str(path), size, index=index)
        except Exception:  # noqa: BLE001 - 换下一个候选
            continue
    from PIL import ImageFont

    return ImageFont.load_default()


def _make_background(video: Path, style: CoverStyle, frame_at: float, work_dir: Path):
    """取一帧、裁成目标比例、重度模糊作为底图。失败时退化为纯渐变。"""
    from PIL import Image, ImageFilter

    canvas = Image.new("RGB", (style.width, style.height), THEMES.get(style.theme, THEMES["tech_blue"])["top"])
    if video is None or not Path(video).exists():
        return canvas

    work_dir.mkdir(parents=True, exist_ok=True)
    frame_path = work_dir / "cover_frame.png"
    try:
        ffmpeg = resolve_binary("ffmpeg")
        if not ffmpeg:
            return canvas
        import subprocess

        subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{max(0.0, frame_at):.2f}", "-i", str(video), "-frames:v", "1",
                str(frame_path),
            ],
            check=True,
            timeout=120,
        )
        frame = Image.open(frame_path).convert("RGB")
    except Exception as exc:  # noqa: BLE001 - 取帧失败不该阻断封面生成
        logger.warning("封面底图取帧失败，改用纯渐变：%s", exc)
        return canvas

    # 等比铺满并居中裁剪
    src_ratio = frame.width / frame.height
    dst_ratio = style.width / style.height
    if src_ratio > dst_ratio:
        new_h = style.height
        new_w = int(new_h * src_ratio)
    else:
        new_w = style.width
        new_h = int(new_w / src_ratio)
    frame = frame.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - style.width) // 2
    top = (new_h - style.height) // 2
    frame = frame.crop((left, top, left + style.width, top + style.height))

    # 重度模糊 + 略微降饱和，让前景文字成为唯一焦点
    frame = frame.filter(ImageFilter.GaussianBlur(style.blur_radius))
    return frame


def _vertical_gradient(size: tuple[int, int], top: tuple[int, int, int], bottom: tuple[int, int, int]):
    from PIL import Image

    width, height = size
    base = Image.new("RGB", (1, height))
    pixels = base.load()
    for y in range(height):
        ratio = y / max(1, height - 1)
        # 缓动曲线：上方更暗，下方稍亮，观感更稳
        ratio = ratio ** 0.85
        pixels[0, y] = tuple(
            int(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3)
        )
    return base.resize((width, height), Image.BILINEAR)


def _draw_grid(draw, style: CoverStyle, color: tuple[int, int, int, int]) -> None:
    """极淡的网格：科技感的来源，但必须几乎察觉不到。"""
    step = max(48, style.width // 14)
    for x in range(0, style.width, step):
        draw.line([(x, 0), (x, style.height)], fill=color, width=1)
    for y in range(0, style.height, step):
        draw.line([(0, y), (style.width, y)], fill=color, width=1)


def _wrap_title(text: str, font, max_width: int, max_lines: int = 3) -> list[str]:
    """按实际渲染宽度折行。中英混排逐字测量，避免长英文单词溢出。"""
    text = (text or "").strip()
    if not text:
        return []

    from PIL import Image, ImageDraw

    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))

    def width_of(s: str) -> float:
        return probe.textlength(s, font=font)

    lines: list[str] = []
    current = ""
    for ch in text:
        candidate = current + ch
        if width_of(candidate) <= max_width or not current:
            current = candidate
            continue
        lines.append(current.rstrip())
        current = ch
        if len(lines) >= max_lines:
            break

    if len(lines) < max_lines and current.strip():
        lines.append(current.rstrip())
    elif current.strip() and len(lines) >= max_lines:
        # 超出最大行数：最后一行加省略号
        last = lines[-1]
        while last and width_of(last + "…") > max_width:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines[:max_lines]


def generate_cover(
    *,
    video: Path | None,
    title: str,
    tags: list[str],
    out_path: Path,
    style: CoverStyle | None = None,
    frame_at: float = 1.0,
    work_dir: Path | None = None,
) -> Path:
    """生成封面并写入 out_path，返回该路径。"""
    from PIL import Image, ImageDraw

    style = style or CoverStyle()
    palette = THEMES.get(style.theme, THEMES["tech_blue"])
    work_dir = work_dir or out_path.parent / "cover_work"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 基准缩放：所有尺寸按 1080x1920 等比换算
    scale = style.height / 1920
    margin = int(84 * scale)
    title_size = max(24, int(style.title_size * scale))
    tag_size = max(14, int(style.tag_size * scale))

    background = _make_background(video, style, frame_at, work_dir)
    canvas = Image.new("RGBA", (style.width, style.height), (0, 0, 0, 255))
    canvas.paste(background, (0, 0))

    # 渐变遮罩：颜色随主题，上方稍浅、下方压暗，保证文字区域对比度
    gradient = _vertical_gradient(
        (style.width, style.height),
        (0, 0, 0),
        (0, 0, 0),
    ).convert("RGBA")
    overlay = Image.new("RGBA", (style.width, style.height), (*palette["bottom"], style.overlay_alpha))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay)
    canvas = Image.alpha_composite(canvas, gradient.point(lambda v: v))

    draw = ImageDraw.Draw(canvas, "RGBA")

    if style.show_grid:
        _draw_grid(draw, style, (*palette["accent"], 12))

    # 顶部品牌胶囊
    if style.brand:
        brand_font = _load_font(max(16, int(30 * scale)))
        bbox = draw.textbbox((0, 0), style.brand, font=brand_font)
        bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
        pad_x, pad_y = int(26 * scale), int(14 * scale)
        box = (margin, margin, margin + bw + pad_x * 2, margin + bh + pad_y * 2 + int(6 * scale))
        draw.rounded_rectangle(box, radius=(box[3] - box[1]) // 2,
                               fill=(*palette["accent"], 38), outline=(*palette["accent"], 150), width=max(1, int(2 * scale)))
        draw.text((box[0] + pad_x, box[1] + pad_y - bbox[1] + int(3 * scale)),
                  style.brand, font=brand_font, fill=(*palette["accent"], 255))

    # 标题：从下方三分之一处往上排版，保证竖版画面的视觉重心
    title_font = _load_font(title_size)
    lines = _wrap_title(title, title_font, style.width - margin * 2 - int(28 * scale))
    line_height = int(title_size * 1.38)
    tags_block = int(96 * scale) if (tags and style.max_tags) else int(20 * scale)
    title_block_h = line_height * len(lines)
    title_top = style.height - int(430 * scale) - tags_block - title_block_h

    if lines:
        # 左侧强调竖条
        bar_x = margin
        bar_w = int(10 * scale)
        draw.rounded_rectangle(
            (bar_x, title_top + int(6 * scale), bar_x + bar_w, title_top + title_block_h - int(6 * scale)),
            radius=bar_w // 2,
            fill=(*palette["accent"], 255),
        )
        text_x = bar_x + bar_w + int(26 * scale)
        for i, line in enumerate(lines):
            y = title_top + i * line_height
            # 轻微投影提升可读性
            draw.text((text_x + int(2 * scale), y + int(3 * scale)), line, font=title_font,
                      fill=(0, 0, 0, 130))
            draw.text((text_x, y), line, font=title_font, fill=(255, 255, 255, 255))

    # 标签胶囊
    if tags and style.max_tags:
        tag_font = _load_font(tag_size)
        x = margin
        y = style.height - int(300 * scale)
        drawn = 0
        shown = [t.strip().lstrip("#") for t in tags if t and t.strip()][: style.max_tags]
        for index, tag in enumerate(shown):
            label = f"#{tag}"
            bbox = draw.textbbox((0, 0), label, font=tag_font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            pad_x, pad_y = int(20 * scale), int(11 * scale)
            box_w = tw + pad_x * 2
            if x + box_w > style.width - margin and index > 0:
                break
            box = (x, y, x + box_w, y + th + pad_y * 2)
            color = palette["accent"] if index % 2 == 0 else palette["accent2"]
            draw.rounded_rectangle(box, radius=(box[3] - box[1]) // 2,
                                   fill=(*color, 30), outline=(*color, 170), width=max(1, int(2 * scale)))
            draw.text((x + pad_x, y + pad_y - bbox[1] + int(2 * scale)), label,
                      font=tag_font, fill=(*color, 255))
            x += box_w + int(16 * scale)
            drawn += 1
        remaining = len([t for t in tags if t and t.strip()]) - drawn
        if remaining > 0 and x + int(120 * scale) < style.width - margin:
            label = f"+{remaining}"
            bbox = draw.textbbox((0, 0), label, font=tag_font)
            box = (x, y, x + (bbox[2] - bbox[0]) + int(34 * scale), y + (bbox[3] - bbox[1]) + int(22 * scale))
            draw.rounded_rectangle(box, radius=(box[3] - box[1]) // 2,
                                   fill=(255, 255, 255, 18), outline=(255, 255, 255, 90),
                                   width=max(1, int(2 * scale)))
            draw.text((x + int(17 * scale), y + int(11 * scale) - bbox[1]), label,
                      font=tag_font, fill=(255, 255, 255, 210))

    # 底部装饰线与品牌
    y_bottom = style.height - int(120 * scale)
    draw.line([(margin, y_bottom), (margin + int(140 * scale), y_bottom)],
              fill=(*palette["accent"], 220), width=max(2, int(6 * scale)))

    canvas.convert("RGB").save(out_path, "JPEG", quality=92)
    return out_path


def cover_size_for(aspect: str, source_width: int = 0, source_height: int = 0) -> tuple[int, int]:
    """按成片比例给出封面尺寸。抖音封面以竖版 9:16 最通用。"""
    if aspect == "16:9":
        return 1920, 1080
    if aspect == "original" and source_width and source_height:
        ratio = source_width / source_height
        if ratio > 1.15:      # 横屏
            return 1920, 1080
        if ratio < 0.87:      # 竖屏
            return 1080, 1920
        return 1080, 1080     # 方形
    return 1080, 1920
