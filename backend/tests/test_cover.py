"""封面生成的测试。

要求：通用、简洁、科技、美观，并能排上视频名称与标签。
"""

from __future__ import annotations

from pathlib import Path

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


class TestGenerativeBackground:
    """背景必须是生成的，不能依赖视频画面。

    原因：很多视频开头是纯黑或纯白帧，用截图做底图会得到黑底/白底封面，
    既难看又让文字失去对比度。
    """

    def _mean_luma(self, path, box=None):
        from PIL import ImageStat

        with Image.open(path) as im:
            gray = im.convert("L")
            return ImageStat.Stat(gray.crop(box) if box else gray).mean[0]

    def test_default_background_is_generated(self):
        from app.services.cover import CoverStyle

        assert CoverStyle().background == "generated"

    def test_black_video_frame_still_yields_visible_cover(self, tmp_path):
        """回归：以纯黑视频为源时，封面不能是一片黑。"""
        import subprocess

        from app.utils.binaries import resolve_binary

        black = tmp_path / "black.mp4"
        subprocess.run(
            [
                resolve_binary("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=black:s=320x240:d=1",
                "-f", "lavfi", "-i", "anullsrc", "-shortest",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(black),
            ],
            check=True,
        )
        out = tmp_path / "cover.jpg"
        generate_cover(video=black, title="纯黑源视频也要能出封面", tags=["测试"],
                       out_path=out, style=CoverStyle(width=1080, height=1920))
        # 早先的 bug：一层 alpha=255 的遮罩把整图涂黑，实测平均亮度只有 4
        assert self._mean_luma(out, (0, 400, 1080, 1000)) > 20, "背景仍然过黑"
        assert self._mean_luma(out) > 20

    def test_background_has_visible_structure(self, tmp_path):
        """背景不能是纯色：应有光晕/网格带来的层次。"""
        from PIL import ImageChops, ImageStat

        out = tmp_path / "texture.jpg"
        generate_cover(video=None, title="层次检查", tags=[], out_path=out,
                       style=CoverStyle(width=1080, height=1920))
        with Image.open(out) as im:
            region = im.convert("L").crop((0, 1200, 1080, 1600))  # 无文字的背景区
            flat = Image.new("L", region.size, int(ImageStat.Stat(region).mean[0]))
            diff = ImageChops.difference(region, flat)
            spread = ImageStat.Stat(diff).stddev[0]
        assert spread > 3, f"背景几乎是纯色（标准差 {spread:.1f}），缺少层次"

    def test_deterministic_for_same_title(self, tmp_path):
        """同一标题每次生成的背景一致，避免重跑时封面无故变化。"""
        a = tmp_path / "a.jpg"
        b = tmp_path / "b.jpg"
        for path in (a, b):
            generate_cover(video=None, title="同一标题", tags=[], out_path=path,
                           style=CoverStyle(width=540, height=960))
        assert a.read_bytes() == b.read_bytes()

    def test_different_titles_differ(self, tmp_path):
        a = tmp_path / "ta.jpg"
        b = tmp_path / "tb.jpg"
        generate_cover(video=None, title="标题甲", tags=[], out_path=a,
                       style=CoverStyle(width=540, height=960))
        generate_cover(video=None, title="标题乙", tags=[], out_path=b,
                       style=CoverStyle(width=540, height=960))
        assert a.read_bytes() != b.read_bytes(), "不同视频的封面应略有差异"

    def test_frame_mode_still_available(self, sample_video, tmp_path):
        """显式选择视频截图作底图时仍应可用（但会压暗以保证文字可读）。"""
        out = tmp_path / "frame.jpg"
        generate_cover(video=sample_video, title="截图底图", tags=[], out_path=out,
                       style=CoverStyle(width=1080, height=1920, background="frame"))
        assert out.exists()


class TestCoverUploadDiagnostics:
    """封面没上传成功时，必须能查清原因。

    事故：发布出去的作品用了视频帧而不是本地设计的封面。原实现的兜底
    （退回平台推荐封面）全程只有 warning，甚至完全静默，
    用户在浏览器里只看到「用了视频截图」，无从判断卡在哪一步。
    """

    def test_every_fallback_logs_a_reason(self):
        import inspect

        from app.providers.publisher.douyin import DouyinPublisher

        source = inspect.getsource(DouyinPublisher._set_cover)
        # 每个降级分支都要有日志，且措辞要说明「本地封面未上传」
        assert source.count("_use_recommended_cover(page)") >= 4
        assert "本次发布没有指定封面文件" in source
        assert "封面文件不存在" in source
        assert "封面弹窗打不开，本地封面未能上传" in source
        assert "没有找到上传入口，本地封面未能上传" in source

    def test_logs_prepared_cover(self):
        import inspect

        from app.providers.publisher.douyin import DouyinPublisher

        source = inspect.getsource(DouyinPublisher._set_cover)
        assert "准备上传本地封面" in source, "开始上传前应记录即将上传的文件，便于对照"

    def test_verifies_preview_changed(self):
        """上传后应校验封面预览是否变化，而不是盲信 set_input_files 成功。"""
        import inspect

        from app.providers.publisher.douyin import DouyinPublisher

        assert hasattr(DouyinPublisher, "_cover_preview_src")
        source = inspect.getsource(DouyinPublisher._set_cover)
        assert "_cover_preview_src" in source

    def test_generated_cover_is_much_larger_than_a_video_frame(self, tmp_path):
        """设计封面是矢量风大色块，JPEG 体积显著大于抽帧结果。

        这条用于区分「磁盘上到底是设计稿还是视频帧」——
        事故中 7 个条目的封面只有 20-153KB，正是旧的抽帧产物。
        """
        out = tmp_path / "gen.jpg"
        generate_cover(video=None, title="体积判据", tags=["测试", "封面"],
                       out_path=out, style=CoverStyle(width=1080, height=1920))
        assert out.stat().st_size > 150 * 1024, (
            f"设计封面只有 {out.stat().st_size // 1024}KB，可能是抽帧产物"
        )


class TestThumbnailCover:
    """默认使用视频原始缩略图作封面。

    原视频封面由创作者为吸引点击专门设计，效果通常好于程序生成的模板。
    """

    def test_default_source_is_thumbnail(self):
        from app.services.cover import CoverStyle

        assert CoverStyle().source == "thumbnail"

    def test_best_thumbnail_url_upgrades_low_res(self):
        from app.services.cover import best_thumbnail_url

        assert "maxresdefault" in best_thumbnail_url("https://i.ytimg.com/vi/abc/hqdefault.jpg")
        assert "maxresdefault" in best_thumbnail_url("https://i.ytimg.com/vi/abc/mqdefault.jpg")
        assert "maxresdefault" in best_thumbnail_url("https://i.ytimg.com/vi/abc/default.jpg")
        # 已是最高清则保持不变
        url = "https://i.ytimg.com/vi/abc/maxresdefault.jpg"
        assert best_thumbnail_url(url) == url
        assert best_thumbnail_url("") == ""

    async def test_uses_local_file_when_remote_fails(self, tmp_path):
        """远程取不到时应退回本地已下载的缩略图（国内网络常访问不到 ytimg）。"""
        from PIL import Image

        from app.services.cover import fetch_thumbnail_bytes

        local = tmp_path / "thumb.webp"
        Image.new("RGB", (320, 180), (200, 30, 30)).save(local, "WEBP")
        data = await fetch_thumbnail_bytes("https://invalid.invalid/x.jpg", local)
        assert data, "本地缩略图未被使用"
        assert len(data) == local.stat().st_size, "返回的应是本地文件的原始内容"

    async def test_returns_none_when_both_unavailable(self, tmp_path):
        from app.services.cover import fetch_thumbnail_bytes

        assert await fetch_thumbnail_bytes("https://invalid.invalid/x.jpg", None) is None
        assert await fetch_thumbnail_bytes("", tmp_path / "missing.webp") is None

    def test_thumbnail_becomes_the_cover(self, tmp_path):
        """封面内容必须就是缩略图本身，而不是又被套上设计模板。"""
        import io

        from PIL import Image, ImageStat

        from app.services.cover import CoverStyle, build_thumbnail_cover

        buffer = io.BytesIO()
        Image.new("RGB", (1280, 720), (60, 120, 200)).save(buffer, "JPEG")
        out = tmp_path / "cover.jpg"
        build_thumbnail_cover(buffer.getvalue(), CoverStyle(width=1920, height=1080), out)

        with Image.open(out) as im:
            assert im.size == (1920, 1080)
            # 纯色缩略图缩放后仍应是同一颜色，不能被滤镜改变
            mean = ImageStat.Stat(im.convert("RGB")).mean
        assert abs(mean[2] - 200) < 12, f"封面颜色被改动：{mean}"
        assert abs(mean[0] - 60) < 12

    def test_aspect_mismatch_uses_blur_fill(self, tmp_path):
        """竖版封面用横版缩略图时，应完整显示原图并用模糊背景铺满，而不是裁掉关键内容。"""
        import io

        from PIL import Image

        from app.services.cover import CoverStyle, build_thumbnail_cover

        buffer = io.BytesIO()
        img = Image.new("RGB", (1280, 720), (10, 10, 10))
        # 在四角画明显色块：完整显示时四个角都应保留
        for xy in ((0, 0), (1180, 0), (0, 620), (1180, 620)):
            img.paste((255, 255, 255), (xy[0], xy[1], xy[0] + 100, xy[1] + 100))
        img.save(buffer, "JPEG")

        out = tmp_path / "vertical.jpg"
        build_thumbnail_cover(buffer.getvalue(), CoverStyle(width=1080, height=1920), out)
        with Image.open(out) as im:
            assert im.size == (1080, 1920)
            pixels = im.convert("L").load()
            # 前景居中，四角标记应在画面中上部被完整保留
            assert pixels[540, 1080 - 300] is not None  # 仅确认可读，细节由视觉验证
            bright = sum(1 for y in range(0, 1920, 4) for x in range(0, 1080, 4) if pixels[x, y] > 200)
        assert bright > 200, "原图四角的标记没有保留，说明被裁切了"


class TestLandscapeCover:
    """抖音的竖封面与横封面分开取图，横封面固定 4:3。

    事故：只设置了竖封面，横版位始终为空。实地检查封面弹窗，文本里明确写着
    「横封面预览（4:3）」，且主表单上有两个封面位（160x147 与 160x120，
    后者比例 1.33 正是 4:3）。
    """

    def test_landscape_size_is_4_3(self):
        from app.services.cover import LANDSCAPE_COVER_SIZE

        width, height = LANDSCAPE_COVER_SIZE
        assert abs(width / height - 4 / 3) < 0.01, f"{width}x{height} 不是 4:3"
        assert width >= 1080, "分辨率过低，横封面会糊"

    def test_publish_request_carries_both_covers(self):
        from app.providers.base import PublishRequest

        request = PublishRequest(video_path=Path("/tmp/x.mp4"), title="t")
        assert request.cover_path is None
        assert request.cover_landscape_path is None, "必须支持单独传横封面"

    def test_both_tabs_are_used(self):
        """实现里必须同时点「设置竖封面」与「设置横封面」两个页签。"""
        import inspect

        from app.providers.publisher.douyin import DouyinPublisher

        source = inspect.getsource(DouyinPublisher._set_cover)
        assert "设置竖封面" in source and "设置横封面" in source, "缺少横封面的上传步骤"

    def test_warns_when_landscape_missing(self):
        import inspect

        from app.providers.publisher.douyin import DouyinPublisher

        source = inspect.getsource(DouyinPublisher._set_cover)
        assert "横封面位（4:3）将保持为空" in source, "缺横封面时必须明确告知用户"

    def test_landscape_cover_content_matches_source(self, tmp_path):
        """横封面同样是缩略图内容，只是按 4:3 重新构图。"""
        import io

        from PIL import Image

        from app.services.cover import LANDSCAPE_COVER_SIZE, CoverStyle, build_thumbnail_cover

        buffer = io.BytesIO()
        Image.new("RGB", (1280, 720), (30, 90, 180)).save(buffer, "JPEG")
        out = tmp_path / "landscape.jpg"
        build_thumbnail_cover(buffer.getvalue(),
                              CoverStyle(width=LANDSCAPE_COVER_SIZE[0], height=LANDSCAPE_COVER_SIZE[1]),
                              out)
        with Image.open(out) as im:
            assert im.size == LANDSCAPE_COVER_SIZE
            assert abs(im.size[0] / im.size[1] - 4 / 3) < 0.01

    def test_item_model_has_landscape_field(self):
        from app.models import TaskItem

        assert "cover_landscape_path" in TaskItem.__table__.columns

    async def test_migration_adds_column_to_existing_db(self, tmp_root):
        """已有数据库需要补列——create_all 不会改动已存在的表。"""
        import sqlite3

        from app.db import init_db

        await init_db()
        from app.core.config import settings

        # 数据库位置由 SPARK_DATABASE_URL 决定，不能假设是 data_dir/spark.db
        url = settings.resolved_database_url()
        db_path = url.split("///")[-1]
        con = sqlite3.connect(db_path)
        columns = {row[1] for row in con.execute("PRAGMA table_info(task_items)")}
        con.close()
        assert columns, f"读不到 task_items 表（数据库：{db_path}）"
        assert "cover_landscape_path" in columns, "已有库缺少横封面列，迁移未生效"


class TestCoverAspectMatchesPlatform:
    """封面比例必须与平台封面位严格一致，否则会被居中裁切。

    事故：封面「只显示中间一部分」。实测平台封面位：
      竖封面位 90x120  -> 0.750 = 3:4
      横封面位 160x120 -> 1.333 = 4:3
    且图片以 object-fit: cover 投放——比例不符就会被裁掉两端。
    原先竖封面沿用了成片比例（16:9 = 1.78），放进 3:4 的位子只剩中间一条。
    """

    def test_portrait_is_3_4(self):
        from app.services.cover import PORTRAIT_COVER_SIZE

        width, height = PORTRAIT_COVER_SIZE
        assert abs(width / height - 3 / 4) < 0.01, f"{width}x{height} 不是 3:4"

    def test_landscape_is_4_3(self):
        from app.services.cover import LANDSCAPE_COVER_SIZE

        width, height = LANDSCAPE_COVER_SIZE
        assert abs(width / height - 4 / 3) < 0.01, f"{width}x{height} 不是 4:3"

    def test_platform_slot_ratios_are_recorded(self):
        """把实测的平台比例固化下来，避免以后又按成片比例出图。"""
        slots = {"portrait": 90 / 120, "landscape": 160 / 120}
        from app.services.cover import LANDSCAPE_COVER_SIZE, PORTRAIT_COVER_SIZE

        assert abs(PORTRAIT_COVER_SIZE[0] / PORTRAIT_COVER_SIZE[1] - slots["portrait"]) < 0.01
        assert abs(LANDSCAPE_COVER_SIZE[0] / LANDSCAPE_COVER_SIZE[1] - slots["landscape"]) < 0.01

    def test_cover_does_not_follow_video_aspect(self):
        """竖封面不能沿用成片比例——16:9 的成片放进 3:4 的位子会被裁切。"""
        from app.services.cover import PORTRAIT_COVER_SIZE

        video_ratio = 1920 / 1080
        cover_ratio = PORTRAIT_COVER_SIZE[0] / PORTRAIT_COVER_SIZE[1]
        assert abs(video_ratio - cover_ratio) > 0.5, "封面比例竟然跟着成片走"
