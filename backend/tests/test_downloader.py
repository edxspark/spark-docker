"""下载器错误上报与重试逻辑的单元测试。

刻意不访问网络：用假的 YoutubeDL 复现「yt-dlp 只写日志、不抛异常」这一真实行为
（这正是线上出现「下载失败但看不到原因」的根因）。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from app.providers.base import ProviderError, VideoInfo
from app.providers.downloader.ytdlp import (
    _download_sync,
    _ErrorCollector,
    _find_video,
    _friendly_error,
    _looks_transient,
)
from app.services.settings_store import DownloadConfig

VIDEO_ID = "testvid001"


class FakeYoutubeDL:
    """可编程的 yt-dlp 替身。

    - log_errors: 通过 logger.error() 上报（yt-dlp 常见路径，不抛异常）
    - raise_error: 直接抛异常
    - produce: 生成假的视频/字幕文件
    """

    behavior: dict = {}

    def __init__(self, opts: dict) -> None:
        self.opts = opts

    def __enter__(self) -> FakeYoutubeDL:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def download(self, urls: list[str]) -> int:
        spec = FakeYoutubeDL.behavior
        logger = self.opts.get("logger")
        for message in spec.get("log_errors", []):
            if logger is not None:
                logger.error(message)
        if spec.get("raise_error"):
            raise RuntimeError(spec["raise_error"])
        if spec.get("produce"):
            out = Path(self.opts["outtmpl"]).parent
            out.mkdir(parents=True, exist_ok=True)
            for name, size in spec["produce"].items():
                (out / name.format(id=VIDEO_ID)).write_bytes(b"x" * size)
        return int(spec.get("retcode", 0))


@pytest.fixture
def fake_ytdlp(monkeypatch):
    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = FakeYoutubeDL  # type: ignore[attr-defined]
    FakeYoutubeDL.behavior = {}
    monkeypatch.setitem(sys.modules, "yt_dlp", module)
    return FakeYoutubeDL


def _download(tmp_path: Path, *, expect_error: bool = True):
    item = VideoInfo(video_id=VIDEO_ID, url="https://www.youtube.com/watch?v=x", title="t")
    config = DownloadConfig(retries=1, subtitle_langs=["en"])
    if expect_error:
        with pytest.raises(ProviderError) as info:
            _download_sync(item, tmp_path, config)
        return str(info.value)
    return _download_sync(item, tmp_path, config)


class TestFindVideo:
    def test_ignores_partial_files(self, tmp_path):
        (tmp_path / f"{VIDEO_ID}.mp4.part").write_bytes(b"partial")
        assert _find_video(tmp_path, VIDEO_ID) is None

    def test_picks_largest_completed_file(self, tmp_path):
        (tmp_path / f"{VIDEO_ID}.webm").write_bytes(b"x" * 10)
        (tmp_path / f"{VIDEO_ID}.mp4").write_bytes(b"x" * 100)
        picked = _find_video(tmp_path, VIDEO_ID)
        assert picked is not None and picked.suffix == ".mp4"


class TestErrorCollector:
    def test_collects_errors_and_warnings(self):
        collector = _ErrorCollector()
        collector.error("ERROR: something broke")
        collector.warning("WARNING: retrying")
        assert "something broke" in collector.summary()

    def test_dedupes_repeated_messages(self):
        collector = _ErrorCollector()
        for _ in range(5):
            collector.error("ERROR: same")
        assert collector.summary().count("same") == 1

    def test_falls_back_to_warning_when_no_error(self):
        collector = _ErrorCollector()
        collector.warning("WARNING: unable to fetch subtitles")
        assert "unable to fetch subtitles" in collector.summary()


class TestTransientDetection:
    @pytest.mark.parametrize(
        "text",
        [
            "Unable to download API page: timed out",
            "connection reset by peer",
            "SSL: CERTIFICATE_VERIFY_FAILED",
            "Giving up after 3 retries",
        ],
    )
    def test_transient_patterns(self, text):
        assert _looks_transient(text)

    @pytest.mark.parametrize(
        "text",
        [
            "Requested format is not available",
            "Sign in to confirm your age",
            "Video unavailable",
        ],
    )
    def test_non_transient_patterns(self, text):
        assert not _looks_transient(text)


class TestFriendlyError:
    def test_network_timeout_mentions_proxy(self):
        message = _friendly_error(RuntimeError("timed out"), action="下载视频")
        assert "代理" in message and message.startswith("下载视频失败")

    def test_age_restricted_mentions_cookies(self):
        message = _friendly_error(RuntimeError("Sign in to confirm your age"), action="下载视频")
        assert "cookies" in message

    def test_format_unavailable_is_actionable(self):
        message = _friendly_error(RuntimeError("Requested format is not available"), action="下载视频")
        assert "格式" in message and "分辨率" in message

    def test_probe_action_prefix(self):
        assert _friendly_error(RuntimeError("boom")).startswith("解析链接失败")


class TestDownloadErrorSurfacing:
    """核心回归：yt-dlp 只写日志、不抛异常时，真实原因必须被上报。"""

    def test_surfaces_logged_error_instead_of_generic_message(self, fake_ytdlp, tmp_path):
        fake_ytdlp.behavior = {
            "log_errors": ["ERROR: [youtube] testvid001: Unable to download API page: timed out"],
        }
        message = _download(tmp_path)
        assert "Unable to download API page" in message, "真实错误被吞掉了"
        assert "代理" in message, "应给出可照做的建议"
        assert "未找到输出文件" not in message, "不应退回无用的兜底文案"

    def test_surfaces_raised_error(self, fake_ytdlp, tmp_path):
        fake_ytdlp.behavior = {"raise_error": "ERROR: [youtube] testvid001: Video unavailable"}
        message = _download(tmp_path)
        assert "Video unavailable" in message

    def test_surfaces_nonzero_retcode_without_log(self, fake_ytdlp, tmp_path):
        fake_ytdlp.behavior = {"retcode": 1}
        message = _download(tmp_path)
        assert "非零状态码" in message

    def test_error_is_in_chinese_actionable_form(self, fake_ytdlp, tmp_path):
        fake_ytdlp.behavior = {"log_errors": ["ERROR: unable to download webpage: timed out"]}
        message = _download(tmp_path)
        assert message.startswith("下载视频失败：")
        assert "「系统配置 → 下载」" in message


class TestDownloadSuccess:
    def test_returns_video_and_subtitle(self, fake_ytdlp, tmp_path):
        fake_ytdlp.behavior = {
            "produce": {
                "{id}.mp4": 2048,
                "{id}.en.srt": 128,
            }
        }
        result = _download(tmp_path, expect_error=False)
        assert result.video_path.name == f"{VIDEO_ID}.mp4"
        assert result.subtitle_path is not None
        assert result.subtitle_kind == "manual"

    def test_video_without_subtitle_still_succeeds(self, fake_ytdlp, tmp_path):
        fake_ytdlp.behavior = {"produce": {"{id}.mp4": 2048}}
        result = _download(tmp_path, expect_error=False)
        assert result.video_path.exists()
        assert result.subtitle_path is None
        assert result.subtitle_kind == "none"

    def test_does_not_retry_when_format_unavailable(self, fake_ytdlp, tmp_path):
        """确定性失败不应触发整轮重试（否则白白多等一轮）。"""
        calls = {"n": 0}
        original = FakeYoutubeDL.download

        def counting_download(self, urls):
            calls["n"] += 1
            return original(self, urls)

        FakeYoutubeDL.download = counting_download
        try:
            fake_ytdlp.behavior = {"log_errors": ["ERROR: Requested format is not available"]}
            _download(tmp_path)
        finally:
            FakeYoutubeDL.download = original
        # 两种字幕模式各一次，但不进入第二轮
        assert calls["n"] == 2, f"确定性失败被重试了：{calls['n']} 次调用"


class TestAnsiStripping:
    """yt-dlp 带 ANSI 颜色码的报错不能泄漏到界面/日志。"""

    def test_collector_strips_ansi(self):
        collector = _ErrorCollector()
        collector.error("\x1b[0;31mERROR:\x1b[0m [youtube] xyz: Unable to download API page")
        summary = collector.summary()
        assert "\x1b" not in summary
        assert summary.startswith("ERROR: [youtube]")

    def test_friendly_error_strips_ansi(self):
        message = _friendly_error(
            RuntimeError("\x1b[0;31mERROR:\x1b[0m [youtube] xyz: timed out"),
            action="下载视频",
        )
        assert "\x1b" not in message and "[0;31m" not in message

    def test_strip_ansi_helper(self):
        from app.providers.downloader.ytdlp import _strip_ansi

        assert _strip_ansi("\x1b[1;32mOK\x1b[0m") == "OK"
        assert _strip_ansi("plain") == "plain"
        assert _strip_ansi("") == ""
