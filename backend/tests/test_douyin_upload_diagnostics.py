"""上传等待环节的诊断与自愈。

事故复盘：真实发布只报一句「等待视频上传完成超时（600s）」，而页面上明明写着
「已上传 78.6MB/86.9MB」「当前速度 1.2MB/s」这类决定性信息，日志里却拿不到，
于是「平台改版」还是「网络太慢」只能靠猜。

实测（同一台机器、同一账号）：86.9MB 成片以 1.2~1.4MB/s 上传，75 秒完成；
整条链路（上传+填文案+双封面）191 秒走到发布按钮前，六项自检全绿。
可见 600 秒本身不缺，缺的是超时时能说清卡在哪。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.providers.base import ProviderError
from app.providers.publisher.douyin import DouyinPublisher
from app.services.settings_store import PublishConfig


class FakeLocator:
    def __init__(self, counts: dict[str, int], selector: str) -> None:
        self.counts = counts
        self.selector = selector

    async def count(self) -> int:
        return self.counts.get(self.selector, 0)


class FakePage:
    """只实现 _wait_upload_finished 用到的那几件事。"""

    def __init__(self, *, done: bool = False, failed: bool = False,
                 progress: str = "已上传： 78.6MB/86.9MB 当前速度：1.2MB/s 剩余时间：7秒",
                 reset: bool = False, advance_until: int | None = None) -> None:
        # advance_until: 前 N 次读取进度时字节数持续增长，之后才变成「上传完成」
        self.advance_until = advance_until
        self.progress_reads = 0
        self.url = (
            "https://creator.douyin.com/creator-micro/content/upload"
            if reset
            else "https://creator.douyin.com/creator-micro/content/post/video"
        )
        self._done = done
        self._failed = failed
        self._progress = progress
        self._reset = reset
        self.evaluations: list[str] = []

    def locator(self, selector: str) -> FakeLocator:
        counts = {}
        if self._done:
            counts[selector] = 1
        elif self._failed:
            counts[selector] = 1 if "上传失败" in selector else 0
        return FakeLocator(counts, selector)

    async def evaluate(self, script: str):
        self.evaluations.append(script)
        if "upload-progress-detail" in script and "upload-progress-inner" in script:
            if self.advance_until is None:
                return self._progress
            self.progress_reads += 1
            if self.progress_reads > self.advance_until:
                self._done = True
                return self._progress
            # 字节数持续增长：慢速但一直在推进
            return f"已上传： {self.progress_reads * 5}.0MB/86.9MB 当前速度：200KB/s 剩余时间：5分"
        if "拖入此区域" in script:
            # 上传区空态判定：空态时没有进度、也没有「取消上传」
            return {
                "empty": self._reset,
                "cancelling": not self._reset,
                "progressed": not self._reset,
            }
        return None


def _publisher() -> DouyinPublisher:
    # timeout 的下限是 60，等待时长由 _wait_upload_finished 的 timeout_ms 单独控制
    return DouyinPublisher(PublishConfig(provider="douyin"), Path("/tmp/none.json"))


async def test_stalled_upload_error_carries_progress():
    """卡死时的错误必须带上页面自己的进度文案，而不是只有一句「超时」。"""
    page = FakePage(progress="已上传： 12.0MB/86.9MB 当前速度：80.0KB/s 剩余时间：16分钟")
    with pytest.raises(ProviderError) as err:
        await _publisher()._wait_upload_finished(
            page, Path("/tmp/v.mp4"), 60000, stall_limit=1.0
        )

    message = str(err.value)
    assert "停滞" in message
    assert "12.0MB/86.9MB" in message, "错误里没有上传进度，无法判断是慢还是卡死"
    assert "80.0KB/s" in message
    assert page.url in message, "错误里没有页面 URL"


async def test_periodic_log_reports_progress():
    """等待期间要周期性把进度写进日志，而不是只说「仍在等待」。"""
    page = FakePage(progress="已上传： 40.0MB/86.9MB 当前速度：1.3MB/s 剩余时间：36秒")
    with pytest.raises(ProviderError):
        await _publisher()._wait_upload_finished(
            page, Path("/tmp/v.mp4"), 60000, stall_limit=1.0
        )
    # 进度读取被调用过，说明日志里带上了抖音自己的进度文案
    assert any("upload-progress-detail" in script for script in page.evaluations)


async def test_cover_area_text_does_not_look_like_an_interrupted_upload():
    """发布页封面区也写着「点击上传新的视频封面」，不能因此判定上传被打断。

    误判的代价是把正在进行的上传重启一次，反而把正常流程拖成超时。
    """
    page = FakePage()  # 停在发布页，正常上传中
    assert await _publisher()._upload_was_reset(page) is False


async def test_interrupted_upload_is_reuploaded_once():
    """退回上传页且上传区回到空态时，应判定上传被打断并重选文件，而不是干等到超时。"""
    import app.providers.publisher.douyin as mod

    page = FakePage(reset=True)
    pub = _publisher()
    selected: list[str] = []

    async def fake_first_visible(_page, _selectors, **_kwargs):
        class FakeInput:
            async def set_input_files(self, path: str) -> None:
                selected.append(path)

        return FakeInput()


    original = mod._first_visible
    mod._first_visible = fake_first_visible
    try:
        with pytest.raises(ProviderError):
            await pub._wait_upload_finished(
                page, Path("/tmp/v.mp4"), 60000, stall_limit=1.0
            )
    finally:
        mod._first_visible = original

    assert selected == ["/tmp/v.mp4"], f"上传被打断却没有重新选中文件：{selected}"


async def test_completed_upload_returns_immediately():
    page = FakePage(done=True)
    await _publisher()._wait_upload_finished(
        page, Path("/tmp/v.mp4"), 5000, stall_limit=1.0
    )
    assert True  # 不抛异常即通过


async def test_slow_but_progressing_upload_is_not_killed():
    """慢速但一直在推进的上传不能被固定墙钟超时误杀。

    实测同一个 86.9MB 成片，上传速度在 17.9KB/s ~ 1.4MB/s 之间波动，
    页面自报剩余时间一度到「1小时22分」。若按固定 2 秒超时，这种上传必被判死。
    """
    page = FakePage(advance_until=4)
    pub = _publisher()
    # 基础超时 2 秒（远小于总耗时），但字节数一直上涨 → 应当续期直到完成
    await pub._wait_upload_finished(
        page, Path("/tmp/v.mp4"), 2000, stall_limit=1.0
    )
    assert page.progress_reads >= 5, f"没有续期，只读了 {page.progress_reads} 次进度"


async def test_stalled_upload_fails_with_speed_and_eta():
    """真的卡死才失败，且错误里要带上速度/剩余时间，能自证原因。"""
    page = FakePage(progress="已上传： 3.4MB/86.9MB 当前速度：104.7KB/s 剩余时间：13分37秒")
    with pytest.raises(ProviderError) as err:
        await _publisher()._wait_upload_finished(
            page, Path("/tmp/v.mp4"), 60000, stall_limit=1.0
        )

    message = str(err.value)
    assert "停滞" in message
    assert "104.7KB/s" in message, "卡死信息里没有速度，无法判断是带宽不足还是真卡死"
    assert "13分37秒" in message
