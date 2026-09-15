"""封面生成的测试。

要求：通用、简洁、科技、美观，并能排上视频名称与标签。
"""

from __future__ import annotations

import pytest
from PIL import Image

from app.services.cover import (
    THEMES,
    CoverStyle,
    cover_size_for,
    generate_cover,
)


@pytest.fixture
def out_path(tmp_path):
    return tmp_path / "cover.jpg"


class TestCoverSize:
    def test_vertical_by_default(self):
        """抖音封面以竖版 9:16 最通用。"""
        assert cover_size_for("9:16") == (1080, 1920)

    def test_landscape(self):
        assert cover_size_for("16:9") == (1920, 1080)

    def test_original_follows_source_ratio(self):
        assert cover_size_for("original", 1920, 1080) == (1920, 1080)
        assert cover_size_for("original", 1080, 1920) == (1080, 1920)
        assert cover_size_for("original", 1000, 1000) == (1080, 1080)

    def test_missing_source_size_falls_back_to_vertical(self):
        assert cover_size_for("original") == (1080, 1920)


class TestGenerateCover:
    def test_produces_image_of_requested_size(self, sample_video, out_path):
        style = CoverStyle(width=1080, height=1920, title_size=76)
        result = generate_cover(
            video=sample_video, title="测试标题", tags=["AI", "科普"],
            out_path=out_path, style=style,
        )
        assert result.exists()
        with Image.open(result) as im:
            assert im.size == (1080, 1920)

    def test_works_without_video(self, out_path):
        """没有视频（取帧失败）时应退化为纯渐变底，而不是报错。"""
        result = generate_cover(
            video=None, title="无视频也能出封面", tags=["测试"],
            out_path=out_path, style=CoverStyle(),
        )
        assert result.exists()
        with Image.open(result) as im:
            assert im.size == (1080, 1920)

    def test_handles_missing_video_file(self, tmp_path, out_path):
        result = generate_cover(
            video=tmp_path / "missing.mp4", title="文件不存在", tags=[],
            out_path=out_path, style=CoverStyle(),
        )
        assert result.exists()

    def test_creates_parent_directories(self, sample_video, tmp_path):
        nested = tmp_path / "a" / "b" / "cover.jpg"
        generate_cover(video=sample_video, title="嵌套目录", tags=[], out_path=nested)
        assert nested.exists()

    def test_all_themes_render(self, sample_video, tmp_path):
        for theme in THEMES:
            path = tmp_path / f"{theme}.jpg"
            generate_cover(
                video=sample_video, title=f"主题 {theme}", tags=["x"],
                out_path=path, style=CoverStyle(theme=theme),
            )
            assert path.exists(), f"主题 {theme} 渲染失败"

    def test_empty_title_and_tags_still_produces_cover(self, sample_video, out_path):
        generate_cover(video=sample_video, title="", tags=[], out_path=out_path)
        assert out_path.exists()

    def test_landscape_size(self, sample_video, out_path):
        generate_cover(
            video=sample_video, title="横版封面", tags=["a"],
            out_path=out_path, style=CoverStyle(width=1920, height=1080),
        )
        with Image.open(out_path) as im:
            assert im.size == (1920, 1080)


class TestTextRendering:
    """封面必须真的把标题与标签画上去，而不是只出个底色。

    判定方式用「与同底图空白版做差分」，而不是数亮像素——
    视频帧本身就可能有大量亮部，会把差异淹没（实测 tags/brand 两项因此误判）。
    """

    @staticmethod
    def _diff_ratio(a, b, box) -> float:
        """两张图在指定区域的差异像素占比。"""
        from PIL import ImageChops

        with Image.open(a) as ia, Image.open(b) as ib:
            ca = ia.convert("L").crop(box)
            cb = ib.convert("L").crop(box)
            diff = ImageChops.difference(ca, cb)
            data = diff.tobytes()
        return sum(1 for v in data if v > 24) / max(1, len(data))

    def _render(self, sample_video, path, **kwargs):
        style_kwargs = kwargs.pop("style_kwargs", {})
        generate_cover(
            video=sample_video, out_path=path,
            style=CoverStyle(width=1080, height=1920, **style_kwargs), **kwargs
        )
        return path

    def test_title_is_drawn(self, sample_video, tmp_path):
        blank = self._render(sample_video, tmp_path / "blank.jpg", title="", tags=[])
        titled = self._render(
            sample_video, tmp_path / "titled.jpg",
            title="Anthropic 的 Claude 如何用电脑", tags=[],
        )
        assert self._diff_ratio(blank, titled, (60, 1040, 1020, 1420)) > 0.01, "标题区域没有任何变化"

    def test_tags_are_drawn(self, sample_video, tmp_path):
        blank = self._render(sample_video, tmp_path / "b.jpg", title="标题", tags=[])
        tagged = self._render(
            sample_video, tmp_path / "t.jpg", title="标题", tags=["AI", "科普", "效率"],
        )
        assert self._diff_ratio(blank, tagged, (60, 1580, 1020, 1710)) > 0.01, "标签没有被画出来"

    def test_brand_is_drawn(self, sample_video, tmp_path):
        blank = self._render(sample_video, tmp_path / "nb.jpg", title="标题", tags=[],
                             style_kwargs={"brand": ""})
        branded = self._render(sample_video, tmp_path / "wb.jpg", title="标题", tags=[],
                               style_kwargs={"brand": "AI 译制"})
        assert self._diff_ratio(blank, branded, (60, 60, 520, 230)) > 0.01, "品牌胶囊没有被画出来"

    def test_longer_title_occupies_more_area(self, sample_video, tmp_path):
        short = self._render(sample_video, tmp_path / "s.jpg", title="短", tags=[])
        long_ = self._render(
            sample_video, tmp_path / "l.jpg",
            title="这是一个明显更长的标题用来验证文字确实被完整绘制出来",
            tags=[],
        )
        region = (60, 1000, 1020, 1500)
        assert self._diff_ratio(short, long_, region) > 0.02

    def test_max_tags_limits_rendering(self, sample_video, tmp_path):
        """超出上限的标签以 +N 表示，不应无限铺开。"""
        few = self._render(sample_video, tmp_path / "few.jpg", title="标题", tags=["a", "b"],
                           style_kwargs={"max_tags": 2})
        many = self._render(sample_video, tmp_path / "many.jpg", title="标题",
                            tags=[f"t{i}" for i in range(8)], style_kwargs={"max_tags": 2})
        # 都是最多 2 个胶囊，差异应当很小（只有 +N 的计数不同）
        assert self._diff_ratio(few, many, (60, 1580, 1020, 1710)) < 0.5


class TestTitleWrapping:
    def test_wraps_long_title(self):
        from app.services.cover import _load_font, _wrap_title

        font = _load_font(76)
        lines = _wrap_title("Anthropic 的 Claude 如何用电脑：一次彻底的能力跃迁与实测", font, 900)
        assert len(lines) > 1
        assert all(len(line) > 0 for line in lines)

    def test_respects_max_lines_with_ellipsis(self):
        from app.services.cover import _load_font, _wrap_title

        font = _load_font(76)
        lines = _wrap_title("很长的标题" * 20, font, 400, max_lines=2)
        assert len(lines) == 2
        assert lines[-1].endswith("…")

    def test_empty_title(self):
        from app.services.cover import _load_font, _wrap_title

        assert _wrap_title("", _load_font(40), 900) == []
        assert _wrap_title("   ", _load_font(40), 900) == []
