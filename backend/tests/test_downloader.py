"""下载器错误上报与重试逻辑的单元测试。

刻意不访问网络：用假的 YoutubeDL 复现「yt-dlp 只写日志、不抛异常」这一真实行为
（这正是线上出现「下载失败但看不到原因」的根因）。
"""

from __future__ import annotations

import asyncio
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


class TestBaseOptsWiring:
    """yt-dlp 需要知道 ffmpeg 与 JS 运行时的位置，否则会在下载/合并阶段失败。"""

    def test_sets_ffmpeg_location_when_available(self):
        from app.providers.downloader.ytdlp import _base_opts
        from app.utils.binaries import resolve_binary

        ffmpeg = resolve_binary("ffmpeg")
        opts = _base_opts(DownloadConfig())
        if ffmpeg:
            assert opts.get("ffmpeg_location"), "未把 ffmpeg 目录告知 yt-dlp"
            assert Path(opts["ffmpeg_location"]).joinpath("ffmpeg").exists() or Path(
                opts["ffmpeg_location"]
            ).joinpath("ffmpeg.exe").exists(), "ffmpeg_location 不是可用的目录"
        else:
            assert "ffmpeg_location" not in opts

    def test_sets_js_runtimes_when_available(self):
        from app.providers.downloader.ytdlp import _base_opts
        from app.utils.binaries import resolve_js_runtime

        runtime = resolve_js_runtime()
        opts = _base_opts(DownloadConfig())
        if runtime:
            name, path = runtime
            assert opts.get("js_runtimes") == {name: {"path": path}}
        else:
            assert "js_runtimes" not in opts

    def test_js_runtime_format_matches_ytdlp_schema(self):
        """yt-dlp 要求 {runtime: {config}}；传错格式会直接抛 ValueError。"""
        from app.providers.downloader.ytdlp import _base_opts

        opts = _base_opts(DownloadConfig())
        runtimes = opts.get("js_runtimes")
        if runtimes is None:
            pytest.skip("本机没有可用的 JS 运行时")
        assert isinstance(runtimes, dict)
        for name, config in runtimes.items():
            assert isinstance(name, str)
            assert config is None or isinstance(config, dict)

    def test_ignoreerrors_default_off(self):
        """回归：ignoreerrors 一旦开启，yt-dlp 的真实错误会被整体吞掉。"""
        from app.providers.downloader.ytdlp import _base_opts

        assert _base_opts(DownloadConfig())["ignoreerrors"] is False


class TestJsRuntimeResolution:
    def test_returns_name_and_path(self):
        from app.utils.binaries import resolve_js_runtime

        runtime = resolve_js_runtime()
        if runtime is None:
            pytest.skip("本机没有可用的 JS 运行时")
        name, path = runtime
        assert name in ("deno", "node", "bun", "quickjs")
        assert Path(path).exists()

    def test_unknown_preference_falls_back_to_autodetect(self):
        from app.utils.binaries import clear_cache, resolve_js_runtime

        clear_cache()
        runtime = resolve_js_runtime("not-a-runtime")
        # 不应抛异常；要么探测到别的运行时，要么返回 None
        assert runtime is None or runtime[0] in ("deno", "node", "bun", "quickjs")


class TestDependencyErrorHints:
    def test_impersonation_error_is_actionable(self):
        message = _friendly_error(
            RuntimeError(
                "ERROR: The extractor specified to use impersonation for this download, "
                "but impersonation is not available"
            ),
            action="下载视频",
        )
        assert "curl-cffi" in message

    def test_js_runtime_error_is_actionable(self):
        message = _friendly_error(
            RuntimeError("ERROR: No supported JavaScript runtime could be found."),
            action="下载视频",
        )
        assert "deno" in message or "node" in message

    def test_missing_ffmpeg_error_is_actionable(self):
        message = _friendly_error(
            RuntimeError("ERROR: You have requested merging of multiple formats but ffmpeg is not installed"),
            action="下载视频",
        )
        assert "brew install ffmpeg" in message


class TestThreadsafeProgress:
    """回归：进度上报必须在 yt-dlp 的工作线程里也能安全调用。

    真实事故：hook 在工作线程中调用 asyncio.get_event_loop() 会抛 RuntimeError，
    而 hook 异常会让 yt-dlp 中止整个下载。修复方式是先在协程中取 loop，
    再用 call_soon_threadsafe 把上报排回去。
    """

    def test_get_event_loop_raises_in_worker_thread(self):
        """先固化「为什么会出事」这个事实，避免以后有人改回去。

        显式把线程的事件循环置空，复现 yt-dlp 工作线程所处的环境。
        """
        import threading

        result: list[str] = []

        def worker():
            try:
                asyncio.set_event_loop(None)
                asyncio.get_event_loop()
                result.append("ok")
            except BaseException as exc:  # noqa: BLE001
                result.append(type(exc).__name__)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        assert result == ["RuntimeError"], (
            f"预计在工作线程里 get_event_loop 会抛 RuntimeError，实际 {result}；"
            "若 Python 行为已变，请复核 make_threadsafe_progress 的必要性"
        )

    def test_schedule_from_worker_thread_runs_coroutine(self):
        from app.pipeline.context import make_threadsafe_progress

        async def main():
            ran: list[float] = []

            async def report(value: float) -> None:
                ran.append(value)

            schedule = make_threadsafe_progress(asyncio.get_running_loop())

            def worker():
                # 模拟 yt-dlp 在工作线程里连续回调
                for i in range(5):
                    schedule(report(float(i)))

            await asyncio.to_thread(worker)
            # 给排队的回调一点时间落地
            for _ in range(50):
                if len(ran) == 5:
                    break
                await asyncio.sleep(0.01)
            return ran

        assert asyncio.run(main()) == [0.0, 1.0, 2.0, 3.0, 4.0]

    def test_schedule_after_loop_closed_is_silent(self):
        """事件循环关闭后调用不应抛异常（任务被取消/服务停机的场景）。"""
        import threading

        from app.pipeline.context import make_threadsafe_progress

        captured: list = []

        async def main():
            captured.append(make_threadsafe_progress(asyncio.get_running_loop()))

        asyncio.run(main())
        schedule = captured[0]

        async def never_awaited() -> None:  # pragma: no cover - 不应被执行
            raise AssertionError("关闭后的回调不应执行")

        errors: list[BaseException] = []

        def worker():
            try:
                schedule(never_awaited())
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        assert errors == [], f"关闭 loop 后调用抛异常了：{errors}"

    def test_scheduler_is_reusable_and_ordered(self):
        from app.pipeline.context import make_threadsafe_progress

        async def main():
            seen: list[str] = []

            async def report(tag: str) -> None:
                seen.append(tag)

            schedule = make_threadsafe_progress(asyncio.get_running_loop())
            await asyncio.to_thread(lambda: [schedule(report("a")) or schedule(report("b"))])
            for _ in range(50):
                if len(seen) == 2:
                    break
                await asyncio.sleep(0.01)
            return seen

        assert asyncio.run(main()) == ["a", "b"]
