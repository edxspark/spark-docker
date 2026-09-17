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
    # 主色对齐应用主题色 #325AB4（深海军蓝 → 靛蓝），强调色只用一种蓝，
    # 避免「蓝 + 紫 + 青」三种色相堆在一起显得杂。
    "tech_blue": {
        "top": (8, 14, 30),
        "bottom": (22, 44, 92),
        "accent": (110, 150, 240),
        "accent2": (50, 90, 180),
    },
    "tech_dark": {
        "top": (7, 9, 13),
        "bottom": (24, 30, 42),
        "accent": (94, 204, 190),
        "accent2": (42, 120, 130),
    },
    "minimal": {
        "top": (16, 17, 20),
        "bottom": (34, 35, 40),
        "accent": (238, 240, 245),
        "accent2": (150, 156, 168),
    },
}

# 候选中文字体：优先思源黑体（有 Bold 字重），其次系统黑体
# 只挑带 Bold 字重的黑体：封面标题需要足够粗才有「版式感」，
# 宋体/细黑在深色底上会显得没精神。
_FONT_CANDIDATES = (
    ("~/Library/Fonts/SourceHanSansCN-Bold.otf", 0),
    ("~/Library/Fonts/SourceHanSansCN-Medium.otf", 0),
    ("/System/Library/Fonts/STHeiti Medium.ttc", 0),
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),
    ("/Library/Fonts/Arial Unicode.ttf", 0),
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
    """统一的科技感背景：单一主色渐变 + 一处光晕 + 点阵网格。

    设计取舍（相对上一版）：上一版叠了「三层光晕 + 斜光带 + 线网格 + 暗角 + 颗粒」，
    元素互相抢戏、画面发灰；这一版只保留三层，并且都压到很低的强度：

      1. 深海军蓝纵向渐变（自上而下变亮，重心在下方的标题区）；
      2. 右上角一团强调色光晕 —— 唯一的「光源」，给画面方向感；
      3. 点阵网格 + 向下淡出 —— 点阵比线网格更克制，也更现代；
      4. 很轻的暗角，只用来收边，不再整体压暗。

    光晕位置由标题派生：同一视频稳定，不同视频略有差异。
    """
    import random

    from PIL import Image, ImageChops, ImageDraw, ImageFilter

    palette = THEMES.get(style.theme, THEMES["tech_blue"])
    rng = random.Random(seed)
    width, height = style.width, style.height

    canvas = _vertical_gradient((width, height), palette["top"], palette["bottom"]).convert("RGB")

    # ---- 唯一光源：右上角光晕 ----
    screen = Image.radial_gradient("L").resize((width, height), Image.BILINEAR)
    mask = ImageChops.invert(screen)
    glow_w = int(width * rng.uniform(1.05, 1.25))
    glow_h = int(height * rng.uniform(0.42, 0.55))
    mask = mask.resize((max(8, glow_w), max(8, glow_h)), Image.BILINEAR)
    # 强度刻意压低：光晕只负责「有光」，不该成为画面主体
    mask = mask.point(lambda v: int(v * 0.42))
    glow = Image.new("RGBA", mask.size, (*palette["accent"], 255))
    glow.putalpha(mask)
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    layer.alpha_composite(glow, (width - glow_w + int(width * 0.18), -int(glow_h * 0.34)))
    # 光晕边缘柔化，避免看到圆形边界
    layer = layer.filter(ImageFilter.GaussianBlur(max(12, width // 40)))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), layer).convert("RGB")

    # ---- 点阵网格 + 向下淡出 ----
    if style.show_grid:
        grid = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw_grid = ImageDraw.Draw(grid, "RGBA")
        step = max(46, width // 13)
        dot = max(2, int(step * 0.055))
        for x in range(step // 2, width, step):
            for y in range(step // 2, height, step):
                draw_grid.ellipse((x, y, x + dot, y + dot), fill=(*palette["accent"], 60))
        fade = _vertical_gradient((width, height), (255, 255, 255), (28, 28, 28)).convert("L")
        grid.putalpha(ImageChops.multiply(grid.getchannel("A"), fade))
        canvas = Image.alpha_composite(canvas.convert("RGBA"), grid).convert("RGB")

    # ---- 轻暗角：收边用 ----
    vignette = ImageChops.invert(screen.resize((width, height), Image.BILINEAR))
    vignette = vignette.point(lambda v: int(v * 0.34))
    canvas = Image.composite(Image.new("RGB", (width, height), (0, 0, 0)), canvas, vignette)

    # ---- 极轻颗粒，避免大面积渐变出现色带 ----
    grain_w = max(2, width // 5)
    grain_h = max(2, height // 5)
    grain_bytes = bytes(rng.getrandbits(8) for _ in range(grain_w * grain_h))
    grain = Image.frombytes("L", (grain_w, grain_h), grain_bytes).resize((width, height), Image.BILINEAR)
    noisy = ImageChops.add(canvas, Image.merge("RGB", (grain, grain, grain)), scale=1.0, offset=-128)
    canvas = Image.blend(canvas, noisy, 0.035)
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


def _fit_title_font(title: str, style: CoverStyle, scale: float, max_width: int, max_lines: int = 3):
    """按内容自适应字号：短标题放大（更有力量），长标题缩小（不溢出、不超行数）。

    两个方向都要有，否则「3 个字的标题」和「39 个字的标题」用同一个字号，
    前者会显得空、后者会挤——这正是封面看起来不精致的原因之一。
    """
    base = max(30, int(style.title_size * scale))

    # 先试放大：只在「放大后仍不超行数」时采用，多行标题自然不会放大
    candidate = base
    for factor in (1.5, 1.38, 1.26, 1.14):
        size = int(base * factor)
        if len(_wrap_title(title, _load_font(size), max_width, max_lines)) <= 2:
            candidate = size
            break

    for size in (candidate, int(candidate * 0.9), int(candidate * 0.8),
                 int(candidate * 0.72), int(candidate * 0.64), int(candidate * 0.56)):
        font = _load_font(max(26, size))
        if len(_wrap_title(title, font, max_width, max_lines)) <= max_lines:
            return font, size
    fallback = max(26, int(base * 0.56))
    return _load_font(fallback), fallback


def _measure(draw, text: str, font) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _draw_eyebrow(draw, text: str, style: CoverStyle, palette: dict, x: int, y: int, scale: float) -> int:
    """标题上方的极简标签：短竖条 + 小字，是全图唯一的「仪表盘」元素。"""
    font = _load_font(max(16, int(28 * scale)))
    bar_w = max(3, int(7 * scale))
    bar_h = max(18, int(34 * scale))
    draw.rounded_rectangle((x, y + int(2 * scale), x + bar_w, y + bar_h), radius=bar_w // 2,
                           fill=(*palette["accent"], 255))
    draw.text((x + bar_w + int(16 * scale), y), text, font=font, fill=(*palette["accent"], 235))
    return y + bar_h


def _draw_corner_ticks(draw, style: CoverStyle, palette: dict, margin: int, scale: float) -> None:
    """四角短刻度线：很轻的一笔，提供「仪表/取景框」的科技暗示而不喧宾夺主。"""
    length = int(38 * scale)
    width = max(2, int(3 * scale))
    color = (*palette["accent"], 120)
    inset = int(34 * scale)
    for cx, cy, dx, dy in (
        (margin - inset, margin - inset, 1, 0),
        (margin - inset, margin - inset, 0, 1),
        (style.width - margin + inset, margin - inset, -1, 0),
        (style.width - margin + inset, margin - inset, 0, 1),
        (margin - inset, style.height - margin + inset, 1, 0),
        (margin - inset, style.height - margin + inset, 0, -1),
        (style.width - margin + inset, style.height - margin + inset, -1, 0),
        (style.width - margin + inset, style.height - margin + inset, 0, -1),
    ):
        draw.line((cx, cy, cx + dx * length, cy + dy * length), fill=color, width=width)


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
    """生成统一版式的封面并写入 out_path。

    版式固定为「上留白 + 下方文字块」的四层结构，任何标题长度都得到同一套骨架：
        ① 顶部：品牌胶囊（左）
        ② 中部：留白 + 单光源背景（让画面有呼吸感）
        ③ 下部：eyebrow 小标签 → 大标题（自动折行/缩号）→ 标签胶囊行
        ④ 底部：发丝分割线 + 品牌落款
    这样不同视频的封面放在一起是「同一套设计」，而不是每张都在拼元素。
    """
    from PIL import Image, ImageDraw

    style = style or CoverStyle()
    palette = THEMES.get(style.theme, THEMES["tech_blue"])
    work_dir = work_dir or out_path.parent / "cover_work"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 基准缩放：所有尺寸按 1080x1440 竖版等比换算
    scale = style.height / 1440
    margin = int(96 * scale)

    from hashlib import sha1

    if style.background == "frame":
        background = _frame_background(video, style, frame_at, work_dir)
    else:
        seed = int(sha1((title or "").encode("utf-8")).hexdigest()[:8], 16)
        background = _tech_background(style, seed=seed)
    canvas = Image.new("RGBA", (style.width, style.height), (0, 0, 0, 255))
    canvas.paste(background, (0, 0))

    # 用视频截图作底图时才压暗（生成式背景本身已按可读性设计，再压会发黑）
    if style.background == "frame":
        overlay = Image.new("RGBA", (style.width, style.height), (*palette["bottom"], style.overlay_alpha))
        canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay)

    draw = ImageDraw.Draw(canvas, "RGBA")

    # 底部文字区加一层很淡的黑色渐变，保证长标题换行时每一行都有对比度
    scrim = Image.new("L", (style.width, int(style.height * 0.55)))
    scrim.putdata([
        int(200 * (i / max(1, scrim.height - 1)) ** 1.6)
        for i in range(scrim.height)
    ])
    scrim_layer = Image.new("RGBA", (style.width, int(style.height * 0.55)), (0, 0, 0, 0))
    scrim_layer.putalpha(scrim)
    canvas.alpha_composite(scrim_layer, (0, style.height - scrim.height))

    if style.show_grid:
        _draw_corner_ticks(draw, style, palette, margin, scale)

    # ---- ① 顶部品牌胶囊 ----
    if style.brand:
        brand_font = _load_font(max(16, int(26 * scale)))
        bw, bh = _measure(draw, style.brand, brand_font)
        pad_x, pad_y = int(24 * scale), int(13 * scale)
        box = (margin, margin, margin + bw + pad_x * 2, margin + bh + pad_y * 2)
        radius = (box[3] - box[1]) // 2
        draw.rounded_rectangle(box, radius=radius, fill=(*palette["accent"], 26),
                               outline=(*palette["accent"], 96), width=max(1, int(2 * scale)))
        # 胶囊内左侧一个小圆点，作为「信号灯」细节
        dot_r = max(3, int(7 * scale))
        cy = (box[1] + box[3]) // 2
        draw.ellipse((box[0] + pad_x, cy - dot_r, box[0] + pad_x + dot_r * 2, cy + dot_r),
                     fill=(*palette["accent"], 255))
        draw.text((box[0] + pad_x + dot_r * 2 + int(12 * scale), box[1] + pad_y - int(2 * scale)),
                  style.brand, font=brand_font, fill=(255, 255, 255, 236))

    # ---- ③ 下部文字块（自下而上排版，保证始终贴住底部安全区）----
    text_x = margin
    text_max_w = style.width - margin * 2
    title_font, title_size = _fit_title_font(title, style, scale, text_max_w)
    lines = _wrap_title(title, title_font, text_max_w)
    line_height = int(title_size * 1.32)
    title_block_h = line_height * len(lines)

    chip_font = _load_font(max(16, int(26 * scale)))
    chip_h = int(62 * scale)
    chip_gap = int(14 * scale)
    eyebrow_h = int(40 * scale)

    baseline = style.height - int(150 * scale)          # 底部落款线之上
    chips_bottom = baseline - int(64 * scale)
    chips_top = chips_bottom - chip_h
    title_bottom = chips_top - int(46 * scale)
    title_top = title_bottom - title_block_h
    eyebrow_y = title_top - eyebrow_h - int(26 * scale)

    eyebrow_text = "AI 译制 · 中文字幕" if style.brand else "AI 译制"
    _draw_eyebrow(draw, eyebrow_text, style, palette, text_x, eyebrow_y, scale)

    for index, line in enumerate(lines):
        y = title_top + index * line_height
        # 双层投影：深色底上纯白字容易「糊」，投影把字从背景里拎出来
        draw.text((text_x + int(2 * scale), y + int(4 * scale)), line, font=title_font, fill=(0, 0, 0, 150))
        draw.text((text_x, y), line, font=title_font, fill=(255, 255, 255, 255))

    # ---- 标签胶囊行 ----
    if tags and style.max_tags:
        shown = [t.strip().lstrip("#") for t in tags if t and t.strip()][: style.max_tags]
        x = text_x
        for index, tag in enumerate(shown):
            label = f"#{tag}"
            tw, _th = _measure(draw, label, chip_font)
            pad_x = int(20 * scale)
            box_w = tw + pad_x * 2
            if x + box_w > style.width - margin and index > 0:
                break
            box = (x, chips_top, x + box_w, chips_top + chip_h)
            # 统一描边胶囊，不再按奇偶换色：多变体反而显得乱
            draw.rounded_rectangle(box, radius=chip_h // 2, fill=(255, 255, 255, 16),
                                   outline=(*palette["accent"], 120), width=max(1, int(2 * scale)))
            draw.text((x + pad_x, chips_top + int(14 * scale)), label, font=chip_font,
                      fill=(226, 234, 250, 240))
            x += box_w + chip_gap

    # ---- ④ 底部发丝线 + 落款 ----
    line_y = style.height - int(120 * scale)
    draw.line((margin, line_y, style.width - margin, line_y), fill=(255, 255, 255, 40),
              width=max(1, int(2 * scale)))
    draw.line((margin, line_y, margin + int(150 * scale), line_y), fill=(*palette["accent"], 235),
              width=max(2, int(5 * scale)))
    footer_font = _load_font(max(14, int(22 * scale)))
    footer = "中文字幕 · AI 配音"
    fw, _fh = _measure(draw, footer, footer_font)
    draw.text((style.width - margin - fw, line_y + int(22 * scale)), footer, font=footer_font,
              fill=(255, 255, 255, 120))

    canvas.convert("RGB").save(out_path, "JPEG", quality=93)
    return out_path


# 抖音创作者中心的封面位比例（实测元素尺寸与 object-fit）：
#   竖封面位 90x120  -> 0.750 = 3:4
#   横封面位 160x120 -> 1.333 = 4:3
# 且图片以 object-fit: cover 投放——比例不匹配就会被居中裁切，只露出中间一条。
# 因此必须严格按这两个比例出图，不能沿用成片比例（16:9 的成片放进 3:4 的位子
# 就会被裁成中间一条，这正是「封面没有完全显示」的原因）。
PORTRAIT_COVER_SIZE = (1080, 1440)
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
