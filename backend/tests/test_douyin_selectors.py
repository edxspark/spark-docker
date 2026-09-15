"""抖音发布页元素定位的测试。

事故复现：真实发布报「未找到上传入口，抖音创作者中心页面结构可能已改版」。
实测页面（2026-09）只有 1 个 `<input type="file">`，无 class/id，尺寸 1x1，
且 SPA 需要几秒才渲染出来。原实现是「扫一遍候选选择器，没命中就返回 None」，
页面加载后只等 2 秒，于是五个候选瞬间全部落空 → 误报为「页面已改版」。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.providers.publisher.douyin import (
    UPLOAD_INPUT_SELECTORS,
    VERIFICATION_SELECTORS,
    _first_visible,
)


class FakeLocator:
    def __init__(self, page: FakePage, selector: str) -> None:
        self.page = page
        self.selector = selector
        self._first = True

    @property
    def first(self) -> FakeLocator:
        return self

    async def count(self) -> int:
        return self.page.counts.get(self.selector, 0)

    async def wait_for(self, *, state: str, timeout: float) -> None:
        self.page.states_used.add(state)
        if self.page.counts.get(self.selector, 0) == 0:
            raise TimeoutError("not found")
        if self.page.attached_only and state == "visible":
            raise TimeoutError("hidden element")


class FakePage:
    """可编程页面：可设置「若干秒后才出现」的元素。"""

    def __init__(self, mapping: dict[str, int], *, appears_after: float = 0.0, attached_only: bool = False):
        self.counts: dict[str, int] = {}
        self.mapping = mapping
        self.appears_after = appears_after
        self.attached_only = attached_only
        self.states_used: set[str] = set()
        self._start = asyncio.get_event_loop().time()

    def locator(self, selector: str) -> FakeLocator:
        elapsed = asyncio.get_event_loop().time() - self._start
        if elapsed >= self.appears_after:
            self.counts[selector] = self.mapping.get(selector, 0)
        else:
            self.counts[selector] = 0
        return FakeLocator(self, selector)


class TestFirstVisiblePolling:
    async def test_finds_element_that_appears_later(self):
        """回归：元素晚几秒出现时，必须继续等，而不是立刻放弃。"""
        page = FakePage({'input[type="file"]': 1}, appears_after=0.8)
        locator = await _first_visible(page, ('input[type="file"]',), timeout=5000)
        assert locator is not None, "元素延迟出现时应继续轮询直到超时"

    async def test_returns_none_after_timeout(self):
        page = FakePage({}, appears_after=0.0)
        locator = await _first_visible(page, ("input.nope",), timeout=900)
        assert locator is None

    async def test_falls_back_across_candidates(self):
        """第一个候选不存在时，应继续尝试后面的候选。"""
        page = FakePage({'input[accept*="video"]': 1})
        locator = await _first_visible(
            page, ("input.upload-btn-input", 'input[accept*="video"]'), timeout=3000
        )
        assert locator is not None
        assert locator.selector == 'input[accept*="video"]'

    async def test_attached_state_for_hidden_inputs(self):
        """文件输入框常被隐藏（尺寸 1x1），用 attached 才能命中。"""
        page = FakePage({'input[type="file"]': 1}, attached_only=True)
        assert await _first_visible(page, ('input[type="file"]',), timeout=1500) is None, (
            "隐藏元素不该被判为 visible"
        )
        found = await _first_visible(
            page, ('input[type="file"]',), timeout=1500, state="attached"
        )
        assert found is not None, "attached 状态下应能找到隐藏的文件输入框"

    async def test_timeout_is_respected(self):
        import time

        page = FakePage({}, appears_after=100)
        start = time.time()
        await _first_visible(page, ("input.nope",), timeout=1000)
        assert 0.8 <= time.time() - start <= 3.0, "超时时间应被遵守"


class TestSelectors:
    def test_has_generic_video_file_candidate(self):
        """必须有足够通用的候选，避免平台改 class 名就完全失配。"""
        assert any("file" in s and "accept" in s for s in UPLOAD_INPUT_SELECTORS), (
            "缺少基于 accept 的通用文件输入选择器"
        )

    def test_verification_selectors_cover_common_challenges(self):
        joined = " ".join(VERIFICATION_SELECTORS)
        for keyword in ("身份验证", "安全验证", "验证码", "滑块"):
            assert keyword in joined, f"风控选择器缺少 {keyword}"


class TestDryRunSupport:
    """干跑：完整走上传与填写流程，但停在「发布」前。

    价值在于把「选择器失效」「遇到风控」这类问题提前暴露，
    而不是等到真正发布时才发现——那时视频已经传上去了。
    """

    def test_request_has_dry_run_flag(self):
        from app.providers.base import PublishRequest

        assert PublishRequest(video_path=Path("/tmp/x.mp4"), title="t").dry_run is False
        assert PublishRequest(video_path=Path("/tmp/x.mp4"), title="t", dry_run=True).dry_run is True

    def test_api_schema_accepts_dry_run(self):
        from app.schemas import PublishItemRequest

        assert PublishItemRequest().dry_run is False
        assert PublishItemRequest(dry_run=True).dry_run is True
        # 与既有的 immediate 互不影响
        assert PublishItemRequest(dry_run=True, immediate=False).immediate is False

    async def test_dry_run_does_not_click_publish(self, monkeypatch):
        """干跑绝不能触发真正的发布点击。"""
        from app.providers.base import ProviderError, PublishRequest
        from app.providers.publisher.douyin import DouyinPublisher
        from app.services.settings_store import PublishConfig

        publisher = DouyinPublisher(PublishConfig(provider="douyin"), Path("/tmp/x.json"))
        clicked: list[bool] = []

        async def fake_click(page, timeout_ms):
            clicked.append(True)
            raise AssertionError("干跑不应调用 _click_publish")

        async def fake_report(page):
            return {"publish_button_found": True}

        monkeypatch.setattr(publisher, "_click_publish", fake_click)
        monkeypatch.setattr(publisher, "_dry_run_report", fake_report)

        # 直接验证：dry_run 分支在 _click_publish 之前就返回
        video = Path("/tmp/dryrun.mp4")
        video.write_bytes(b"x")
        try:
            await publisher._publish_locked(
                PublishRequest(video_path=video, title="t", dry_run=True), video, headless=True
            )
            raise AssertionError("未配置登录态时应抛错，而不是走到发布")
        except (ProviderError, AssertionError) as exc:
            assert "干跑不应调用" not in str(exc)
        finally:
            video.unlink(missing_ok=True)
        assert clicked == [], "干跑路径不应触碰发布点击"


class TestClickReachability:
    """事故：发布按钮位于首屏之外，force click 在视口外点击 → 静默落空。"""

    def test_launch_uses_chromium_channel(self):
        """默认构建是 Chromium for Testing，带已知自动化标记；应使用真实 Chromium 通道。"""
        from app.providers.publisher.douyin import _launch_options

        opts = _launch_options(headless=True)
        assert opts["channel"] == "chromium"
        assert opts["headless"] is True
        assert any("AutomationControlled" in a for a in opts["args"])

    def test_launch_falls_back_without_channel(self):
        from app.providers.publisher.douyin import _launch_options

        assert "channel" not in _launch_options(headless=True, channel=None)

    async def test_playwright_prefers_patchright(self):
        import importlib.util

        from app.providers.publisher.douyin import _require_playwright

        api = _require_playwright()
        if importlib.util.find_spec("patchright") is not None:
            assert api.__module__.startswith("patchright"), (
                "已安装 patchright 时应优先使用它（stealth 驱动）"
            )

    async def test_can_click_at_detects_offscreen_element(self):
        """元素存在但其中心点在视口外时，必须判定为不可点击。"""
        from app.providers.publisher.douyin import _can_click_at

        class OffscreenLocator:
            async def bounding_box(self):
                return {"x": 100, "y": 5000, "width": 120, "height": 32}

            async def evaluate(self, _script):
                return False  # 模拟 elementFromPoint 返回 null

        assert await _can_click_at(None, OffscreenLocator()) is False

    async def test_can_click_at_handles_missing_box(self):
        from app.providers.publisher.douyin import _can_click_at

        class NoBox:
            async def bounding_box(self):
                return None

        assert await _can_click_at(None, NoBox()) is False
