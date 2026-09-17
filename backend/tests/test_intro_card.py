"""统一开头语的「科技感开头画面」（标题卡）。

覆盖三件事：
1. 标题卡本身能生成（矢量渲染 + 保底渲染都可用，尺寸与内容正确）；
2. 自定义图片与缺失图片的回退行为；
3. 成片渲染时标题卡是否真的出现在开头语那几秒里（这是最容易漏的：
   tpad 插入的帧时间戳为负，overlay 的 enable 条件若按正时间写就永远不生效）。
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.services import intro_card
from tests.test_intro import _create_task, _load, _prepare, _run, _settings, INTRO_TEXT

pytestmark = pytest.mark.asyncio

CARD_CONFIG = {
    "card_mode": "auto",
    "card_title": "欢迎来到 EdxSpark",
    "card_subtitle": "AI 创业 · 工作 · 创新 · 学习",
    "card_brand": "EdxSpark",
}


def _card_size(path) -> tuple[int, int]:
    from PIL import Image

    with Image.open(path) as image:
        return image.size


def _luminance(path, box=None) -> float:
    from PIL import Image

    with Image.open(path).convert("RGB") as image:
        crop = image.crop(box) if box else image
        pixels = list(crop.getdata())
    if not pixels:
        return 0.0
    return sum(0.2126 * r + 0.7152 * g + 0.0722 * b for r, g, b in pixels) / len(pixels)


# ---------------------------------------------------------------- 卡片生成


async def test_card_is_generated_at_requested_size():
    result = intro_card.build_card(width=1080, height=1920, config=CARD_CONFIG, force=True)

    assert result.path.exists() and result.path.stat().st_size > 0
    assert result.width == 1080 and result.height == 1920
    assert result.renderer in {"resvg", "magick", "pillow"}, result.renderer
    assert _card_size(result.path) == (1080, 1920)


async def test_card_is_dark_with_bright_text():
    result = intro_card.build_card(width=720, height=1280, config=CARD_CONFIG, force=True)
    from PIL import Image

    with Image.open(result.path).convert("RGB") as image:
        pixels = list(image.getdata())
    luminance = [0.2126 * r + 0.7152 * g + 0.0722 * b for r, g, b in pixels]

    # 深色科技底：整体偏暗，但必须有足够亮的主标题，否则就是一张黑图
    assert sum(luminance) / len(luminance) < 90, "背景不够暗，可能没渲染出底色"
    assert max(luminance) > 180, "没有亮色文字，可能是空白图"
    bright_ratio = sum(1 for value in luminance if value > 150) / len(luminance)
    assert 0.002 < bright_ratio < 0.2, f"文字占比异常：{bright_ratio:.4f}"


async def test_card_is_cached_between_calls():
    first = intro_card.build_card(width=540, height=960, config=CARD_CONFIG, force=True)
    second = intro_card.build_card(width=540, height=960, config=CARD_CONFIG)

    assert second.path == first.path, "相同配置应复用同一张卡片"
    assert second.renderer == "cache", f"第二次应命中缓存，实际：{second.renderer}"


async def test_long_title_is_wrapped_inside_safe_area():
    from PIL import Image

    title = "欢迎来到EdxSpark，我们将为您提供高质量的AI创业学习资讯"
    result = intro_card.build_card(
        width=1080, height=1920, config={**CARD_CONFIG, "card_title": title}, force=True
    )
    safe = 1080 * intro_card._SAFE_RATIO

    # 自动缩小 + 折行后，每一行都必须落在安全区内
    assert len(result.lines) >= 2, f"长标题没有折行：{result.lines}"
    assert result.title_size < 118, "长标题没有自动缩小字号"
    for line in result.lines:
        assert intro_card.text_width(line, result.title_size) <= safe + 1, f"超出安全区：{line}"

    # 图上不能出现「贴边」的亮色文字：最左/最右 3% 区域内不应有文字像素
    with Image.open(result.path).convert("RGB") as image:
        width, height = image.size
        edge = int(width * 0.03)
        for box in ((0, 0, edge, height), (width - edge, 0, width, height)):
            crop = list(image.crop(box).getdata())
            bright = sum(1 for r, g, b in crop if 0.2126 * r + 0.7152 * g + 0.0722 * b > 170)
            assert bright == 0, "文字贴到了画面边缘"


async def test_custom_image_and_missing_image(tmp_path):
    custom = tmp_path / "my-card.png"
    from PIL import Image

    Image.new("RGB", (200, 400), "#102030").save(custom)

    used = intro_card.build_card(width=540, height=960, config={**CARD_CONFIG, "card_mode": "custom", "card_image": str(custom)})
    assert used.path == custom and used.renderer == "custom"

    # 路径不存在：不能失败，回退为自动生成
    fallback = intro_card.build_card(
        width=540, height=960, config={**CARD_CONFIG, "card_mode": "custom", "card_image": str(tmp_path / "nope.png")}
    )
    assert fallback.path != custom
    assert fallback.path.exists()
    # 同尺寸卡片可能已被前面的用例生成过，因此这里也接受「命中缓存」
    assert fallback.renderer in {"resvg", "magick", "pillow", "cache"}, fallback.renderer


async def test_card_served_as_data_uri(tmp_root):
    """配置页的预览接口：返回可直接显示的 data URI。"""
    import httpx

    from app.db import init_db
    from app.main import app

    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post("/api/settings/intro/card", json={"intro": CARD_CONFIG})
        assert response.status_code == 200, response.text
        payload = response.json()

    assert payload["image"].startswith("data:image/png;base64,")
    assert payload["width"] == 1080 and payload["height"] == 1920


# ---------------------------------------------------------------- 成片集成


async def test_intro_card_appears_in_output(tmp_path, monkeypatch, sample_video, sample_srt):
    if not (await _ffmpeg_ok()):
        pytest.skip("未安装 ffmpeg")

    await _prepare(monkeypatch, sample_video, sample_srt, _settings(**CARD_CONFIG))
    task_id = await _create_task("https://www.youtube.com/watch?v=card0001")
    await _run(task_id)
    task, items, logs = await _load(task_id)
    item = items[0]

    assert task.status == "succeeded", task.message
    card_stats = (item.stats or {}).get("intro_card") or {}
    assert card_stats.get("ok") is True, card_stats
    assert (settings.data_dir / card_stats["path"]).exists()
    assert any("开头画面已就绪" in log.message for log in logs)

    offset = float(item.stats["intro"]["offset"])
    output = settings.data_dir / item.output_path

    # 开头语时段应是标题卡：原片测试图（testsrc）在角落是彩条，卡片是深色底
    from app.utils import ffmpeg as ffmpeg_utils

    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    card_frame = frames_dir / "card.png"
    main_frame = frames_dir / "main.png"
    await ffmpeg_utils.run_ffmpeg(
        ["-ss", f"{offset / 2:.2f}", "-i", str(output), "-frames:v", "1", str(card_frame)]
    )
    await ffmpeg_utils.run_ffmpeg(
        ["-ss", f"{offset + 1.0:.2f}", "-i", str(output), "-frames:v", "1", str(main_frame)]
    )

    assert _luminance(card_frame) < 90, "开头语时段看起来不是标题卡（太亮）"
    assert _luminance(main_frame) > 90, "正片时段画面异常（太暗）"
    # 成片总长应为「原片 + 开头语」
    assert await ffmpeg_utils.audio_duration(output) == pytest.approx(12.0 + offset, abs=0.8)


async def test_card_mode_none_keeps_freeze_frame(monkeypatch, sample_video, sample_srt):
    if not (await _ffmpeg_ok()):
        pytest.skip("未安装 ffmpeg")

    await _prepare(monkeypatch, sample_video, sample_srt, _settings(card_mode="none"))
    task_id = await _create_task("https://www.youtube.com/watch?v=card0002")
    await _run(task_id)
    task, items, _ = await _load(task_id)

    assert task.status == "succeeded", task.message
    assert not (items[0].stats or {}).get("intro_card")


async def _ffmpeg_ok() -> bool:
    from app.utils import ffmpeg as ffmpeg_utils

    return ffmpeg_utils.ffmpeg_available()


async def test_intro_text_still_matches_card_title(monkeypatch, sample_video, sample_srt):
    """配音文案与卡片标题是两处配置，容易各改一半：这里固化住它们的关系。"""
    if not (await _ffmpeg_ok()):
        pytest.skip("未安装 ffmpeg")

    config = _settings(**CARD_CONFIG)
    assert INTRO_TEXT in config["text"]
    assert "EdxSpark" in CARD_CONFIG["card_title"]

async def test_all_layouts_render_distinct_designs():
    """三种版式必须都能渲染成 PNG，且构图确实不同（不只是换了文案）。"""
    from PIL import Image

    signatures: dict[str, tuple] = {}
    for layout in intro_card.LAYOUTS:
        result = intro_card.build_card(
            width=720, height=1280, config={**CARD_CONFIG, "card_layout": layout}, force=True
        )
        assert result.renderer in {"resvg", "magick", "pillow"}, f"{layout}: {result.renderer}"
        assert _card_size(result.path) == (720, 1280)

        with Image.open(result.path).convert("RGB") as image:
            pixels = list(image.getdata())
        luminance = [0.2126 * r + 0.7152 * g + 0.0722 * b for r, g, b in pixels]
        assert max(luminance) > 180, f"{layout}: 没有亮色文字"

        image = Image.open(result.path).convert("L").resize((72, 128))
        signatures[layout] = tuple(image.getdata())

    # 两两比较：至少 6% 的网格点差异明显（否则说明版式没生效）
    layouts = list(intro_card.LAYOUTS)
    for i, first in enumerate(layouts):
        for second in layouts[i + 1 :]:
            diff = sum(1 for a, b in zip(signatures[first], signatures[second]) if abs(a - b) > 24)
            ratio = diff / len(signatures[first])
            assert ratio > 0.06, f"{first} 与 {second} 构图几乎一样（差异 {ratio:.1%}）"


async def test_layout_participates_in_cache_key():
    hero = intro_card.build_card(width=360, height=640, config={**CARD_CONFIG, "card_layout": "hero"}, force=True)
    frame = intro_card.build_card(width=360, height=640, config={**CARD_CONFIG, "card_layout": "frame"})

    assert hero.path != frame.path, "切换版式后不应复用旧卡片"
    # 同版式第二次应命中缓存
    again = intro_card.build_card(width=360, height=640, config={**CARD_CONFIG, "card_layout": "hero"})
    assert again.path == hero.path
