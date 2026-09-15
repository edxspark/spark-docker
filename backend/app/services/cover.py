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
    # 底图来源：generated 为程序化科技背景；frame 为视频截图
    # （开头是黑/白帧时会得到黑底或白底封面）
    background: str = "generated"
    # 封面来源：thumbnail 用视频原始缩略图（推荐，创作者设计过的封面图）；
    # generated 用程序生成的设计稿；frame 从成片抽帧
    source: str = "thumbnail"
    # 用缩略图时是否叠加标题与标签。缩略图本身通常已含文字，默认不叠加避免重复。
    overlay_text_on_thumbnail: bool = False
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


def best_thumbnail_url(url: str) -> str:
    """把 YouTube 缩略图换成最高清版本。

    yt-dlp 有时只给 hqdefault（480x360），而同一张图通常有 maxresdefault
    （1280x720）。用作封面时清晰度差别肉眼可见，因此统一升级到最高清。
    """
    if not url:
        return ""
    for low in ("hqdefault", "mqdefault", "sddefault", "default"):
        if f"/{low}." in url:
            return url.replace(f"/{low}.", "/maxresdefault.")
    return url


async def fetch_thumbnail_bytes(url: str, local_file: Path | None = None) -> bytes | None:
    """取缩略图数据：优先最高清远程图，失败再退回本地已下载的文件。"""
    import httpx

    candidates: list[str] = []
    upgraded = best_thumbnail_url(url)
    if upgraded:
        candidates.append(upgraded)
    if url and url != upgraded:
        candidates.append(url)

    for candidate in candidates:
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                response = await client.get(candidate)
            if response.status_code == 200 and len(response.content) > 5000:
                return response.content
        except Exception as exc:  # noqa: BLE001 - 逐级回退
            logger.warning("缩略图下载失败（%s）：%s", candidate[:60], exc)

    if local_file and Path(local_file).exists():
        try:
            return Path(local_file).read_bytes()
        except OSError as exc:
            logger.warning("本地缩略图读取失败：%s", exc)
    return None


def _fit_image(img, style: CoverStyle):
    """把图片放进目标画幅。

    比例一致时直接缩放；不一致时「完整显示 + 模糊铺满背景」，
    避免把创作者精心设计的封面裁掉关键信息（标题通常压在边角）。
    """
    from PIL import Image, ImageFilter

    width, height = style.width, style.height
    src_ratio = img.width / img.height
    dst_ratio = width / height

    if abs(src_ratio - dst_ratio) < 0.03:
        return img.resize((width, height), Image.LANCZOS)

    # 背景：原图铺满 + 重度模糊 + 压暗
    if src_ratio > dst_ratio:
        bg_h = width
        bg_w = int(bg_h * src_ratio)
    else:
        bg_w = height
        bg_h = int(bg_w / src_ratio)
    background = img.resize((max(2, bg_w), max(2, bg_h)), Image.LANCZOS)
    left = (background.width - width) // 2
    top = (background.height - height) // 2
    background = background.crop((left, top, left + width, top + height))
    background = background.filter(ImageFilter.GaussianBlur(48))
    background = Image.blend(background, Image.new("RGB", background.size, (8, 10, 16)), 0.45)

    # 前景：完整等比缩放到目标内
    if src_ratio > dst_ratio:
        fg_w = width
        fg_h = max(1, int(width / src_ratio))
    else:
        fg_h = height
        fg_w = max(1, int(height * src_ratio))
    foreground = img.resize((fg_w, fg_h), Image.LANCZOS)

    canvas = background.copy()
    canvas.paste(foreground, ((width - fg_w) // 2, (height - fg_h) // 2))
    return canvas


def build_thumbnail_cover(data: bytes, style: CoverStyle, out_path: Path) -> Path:
    """用视频原始缩略图生成封面。

    原视频封面由创作者设计，用于吸引点击，通常比程序生成的更好看，
    因此默认优先使用它。
    """
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(data))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    canvas = _fit_image(img, style)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(out_path, "JPEG", quality=92)
    return out_path


def _tech_background(style: CoverStyle, *, seed: int = 0):
    """纯生成式科技背景，完全不依赖视频画面。

    为什么不用视频截图：很多视频开头是纯黑或纯白帧，模糊后得到的是一张
    黑底或白底，既难看又让文字失去对比度——「封面黑乎乎的」正是这么来的。
    这里改用程序化绘制的抽象背景，任何视频都能得到稳定、干净的效果。

    构成（全部克制，避免抢标题的视觉焦点）：
      1. 深色纵向渐变打底；
      2. 两到三团强调色光晕，位置由标题派生，因此不同视频略有差异但同一视频稳定；
      3. 细网格 + 由上而下的淡出，提供科技感而不喧宾夺主；
      4. 一道斜向光带，打破纯渐变的呆板；
      5. 暗角 + 细颗粒，让画面更耐看、更有质感。
    """
    import random

    from PIL import Image, ImageChops, ImageDraw, ImageFilter

    palette = THEMES.get(style.theme, THEMES["tech_blue"])
    rng = random.Random(seed)
    width, height = style.width, style.height

    canvas = _vertical_gradient((width, height), palette["top"], palette["bottom"]).convert("RGB")

    # ---- 强调色光晕 ----
    # Image.radial_gradient 中心黑、边缘白，取反即得到中心亮的光晕蒙版
    glow_mask_base = Image.radial_gradient("L").resize((width, height), Image.BILINEAR)
    glow_mask_base = ImageChops.invert(glow_mask_base)

    glow_colors = [palette["accent"], palette["accent2"], palette["accent"]]
    for index in range(3):
        scale = rng.uniform(0.55, 0.95)
        gw = int(width * scale)
        gh = int(height * scale * rng.uniform(0.5, 0.8))
        mask = glow_mask_base.resize((max(8, gw), max(8, gh)), Image.BILINEAR)
        # 亮度：越靠后的光晕越弱，形成层次
        strength = (110, 84, 62)[index]
        mask = mask.point(lambda v, k=strength: min(255, int(v * k / 255)))
        layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        color = glow_colors[index % len(glow_colors)]
        patch = Image.new("RGBA", mask.size, (*color, 255))
        patch.putalpha(mask)
        x = int(rng.uniform(-gw * 0.25, width - gw * 0.5))
        y = int(rng.uniform(-gh * 0.3, height - gh * 0.5))
        layer.alpha_composite(patch, (max(0, x), max(0, y)) if x >= 0 and y >= 0 else (x, y))
        canvas = Image.alpha_composite(canvas.convert("RGBA"), layer).convert("RGB")

    # ---- 斜向光带 ----
    band = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw_band = ImageDraw.Draw(band, "RGBA")
    offset = int(height * 0.28)
    draw_band.polygon(
        [
            (0, offset),
            (width, offset - int(height * 0.16)),
            (width, offset - int(height * 0.16) + int(height * 0.045)),
            (0, offset + int(height * 0.045)),
        ],
        fill=(*palette["accent"], 26),
    )
    band = band.filter(ImageFilter.GaussianBlur(28))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), band).convert("RGB")

    # ---- 细网格 + 纵向淡出 ----
    if style.show_grid:
        grid = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw_grid = ImageDraw.Draw(grid, "RGBA")
        step = max(40, width // 16)
        for x in range(0, width, step):
            draw_grid.line([(x, 0), (x, height)], fill=(*palette["accent"], 16), width=1)
        for y in range(0, height, step):
            draw_grid.line([(0, y), (width, y)], fill=(*palette["accent"], 16), width=1)
        # 越靠下越淡：把网格乘上一个纵向渐变蒙版
        fade = _vertical_gradient((width, height), (255, 255, 255), (0, 0, 0)).convert("L")
        grid.putalpha(ImageChops.multiply(grid.getchannel("A"), fade))
        canvas = Image.alpha_composite(canvas.convert("RGBA"), grid).convert("RGB")

    # ---- 暗角 ----
    vignette = ImageChops.invert(glow_mask_base.resize((width, height), Image.BILINEAR))
    vignette = vignette.point(lambda v: int(v * 0.55))
    dark = Image.new("RGB", (width, height), (0, 0, 0))
    canvas = Image.composite(dark, canvas, vignette)

    # ---- 细颗粒，避免大面积纯色显得廉价 ----
    # 两个注意点：
    #   1. ImageChops.add(im1, im2, scale) 的结果是 (im1+im2)/scale。
    #      早先误用 scale=6.0，等于把整图亮度除以 6，封面严重发暗。改用 blend 混合，
    #      blend 的 alpha 才是「颗粒强度」的正确表达方式。
    #   2. 不用 Image.effect_noise：它不受我们的随机种子控制，会导致同一标题
    #      每次生成不同封面。改为用受种子控制的随机字节，在低分辨率生成后放大，
    #      既完全可复现，又比逐像素生成快得多。
    grain_w = max(2, width // 4)
    grain_h = max(2, height // 4)
    grain_bytes = bytes(rng.getrandbits(8) for _ in range(grain_w * grain_h))
    grain = Image.frombytes("L", (grain_w, grain_h), grain_bytes).resize(
        (width, height), Image.BILINEAR
    )
    grain_rgb = Image.merge("RGB", (grain, grain, grain))
    noisy = ImageChops.add(canvas, grain_rgb, scale=1.0, offset=-128)
    canvas = Image.blend(canvas, noisy, 0.05)

    return canvas


def _frame_background(video: Path, style: CoverStyle, frame_at: float, work_dir: Path):
    """取视频一帧、裁成目标比例、重度模糊作为底图。

    注意：视频开头若为纯黑/纯白帧，出来的就是黑底或白底封面——
    因此默认不使用，仅在显式配置 cover_background="frame" 时启用。
    """
    from PIL import Image, ImageFilter

    canvas = _vertical_gradient(
        (style.width, style.height),
        THEMES.get(style.theme, THEMES["tech_blue"])["top"],
        THEMES.get(style.theme, THEMES["tech_blue"])["bottom"],
    ).convert("RGB")
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
        logger.warning("封面底图取帧失败，改用生成式背景：%s", exc)
        return canvas

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
    return frame.filter(ImageFilter.GaussianBlur(style.blur_radius))


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

    # 默认使用生成式背景：不依赖视频画面，避免开头是黑/白帧时得到黑底或白底封面
    from hashlib import sha1

    if style.background == "frame":
        background = _frame_background(video, style, frame_at, work_dir)
    else:
        # 由标题派生随机种子：同一视频稳定，不同视频略有差异
        seed = int(sha1((title or "").encode("utf-8")).hexdigest()[:8], 16)
        background = _tech_background(style, seed=seed)
    canvas = Image.new("RGBA", (style.width, style.height), (0, 0, 0, 255))
    canvas.paste(background, (0, 0))

    # 仅在使用视频截图作底图时才需要压暗——照片内容不可控，必须拉低亮度保证文字可读。
    # 生成式背景本身已经是按「文字可读」设计的深色，再压一层会直接把画面涂黑：
    # 早先无条件叠加一层 alpha=255 的纯色，实测把整图平均亮度从 52 压到 4，
    # 封面看起来就是一片黑（这正是用户反馈的「黑乎乎」）。
    if style.background == "frame":
        overlay = Image.new(
            "RGBA", (style.width, style.height), (*palette["bottom"], style.overlay_alpha)
        )
        canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay)

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


# 抖音的横封面固定为 4:3（弹窗里显示「横封面预览（4:3）」）
LANDSCAPE_COVER_SIZE = (1440, 1080)


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
