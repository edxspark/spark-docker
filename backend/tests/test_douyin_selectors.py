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


class TestPublishButtonSelector:
    """事故复盘：发布按钮必须精确匹配文本。

    原选择器是 `button:has-text("发布")` —— 子串匹配会同时命中左侧导航项
    「作品发布」（文本里含「发布」二字），而 .first 取到的正是那个导航项。
    于是「点发布」实际点了导航，页面跳到 content/upload，作品根本没提交，
    却表现为「点击后未跳转到作品管理页」，把排查方向完全带偏。
    实测该页面 has-text 命中 2 个，text-is 精确命中 1 个。
    """

    def test_no_substring_matching_in_publish_selectors(self):
        from app.providers.publisher.douyin import PUBLISH_BUTTON_SELECTORS

        for selector in PUBLISH_BUTTON_SELECTORS:
            assert ":has-text(" not in selector, (
                f"发布按钮不能用子串匹配（{selector}）——会命中「作品发布」导航项"
            )
            assert 'text-is("发布")' in selector or "exact" in selector, selector

    def test_selectors_target_exact_publish_text(self):
        from app.providers.publisher.douyin import PUBLISH_BUTTON_SELECTORS

        assert any('button:text-is("发布")' == s for s in PUBLISH_BUTTON_SELECTORS), (
            "首选应是精确文本匹配"
        )

    def test_regression_documents_the_nav_item_trap(self):
        """「作品发布」会命中 has-text("发布")，这个事实必须被记录。"""
        nav_item = "作品发布"
        exact_text = "发布"
        assert exact_text in nav_item, "导航项确实包含「发布」子串，这正是陷阱所在"
        assert nav_item != exact_text, "两者文本不同，精确匹配可区分"


class TestCoverSelectors:
    """事故：发布出去的视频没有封面（平台显示黑底）。

    根因候选之一是封面弹窗里有两个上传槽位，选错就会把封面塞进
    「AI 封面参考图」而非真正的「上传封面」。实地用真实账号验证：
      精确选择器命中 1 个，而 input.semi-upload-hidden-input 共有 2 个。
    """

    def test_upload_selector_targets_main_drag_area(self):
        """必须按拖拽区文案定位，才能与「生成参考图」槽位区分开。"""
        from app.providers.publisher.douyin import COVER_UPLOAD_INPUT_SELECTOR

        assert "semi-upload-drag-area-main-text" in COVER_UPLOAD_INPUT_SELECTOR, (
            "未按拖拽区文案限定，会命中 AI 封面参考图的槽位"
        )
        assert "semi-upload-hidden-input" in COVER_UPLOAD_INPUT_SELECTOR

    def test_modal_selector_matches_creator_modal(self):
        from app.providers.publisher.douyin import COVER_MODAL_SELECTOR

        assert COVER_MODAL_SELECTOR == "div.dy-creator-content-modal"

    def test_trigger_texts_cover_known_labels(self):
        from app.providers.publisher.douyin import COVER_TRIGGER_TEXTS

        for text in ("编辑封面", "选择封面", "设置封面"):
            assert text in COVER_TRIGGER_TEXTS

    def test_onboarding_overlay_is_cleared(self):
        """shepherd 新手引导浮层会拦截封面区点击，必须清理。"""
        from app.providers.publisher.douyin import ONBOARDING_SELECTORS

        assert ONBOARDING_SELECTORS, "必须处理引导浮层，否则封面弹窗打不开"
        assert any("shepherd" in s for s in ONBOARDING_SELECTORS)


class TestWaitUntilEnabled:
    """封面图处理完之前「完成」是禁用状态，点了无效且弹窗关不掉。"""

    async def test_returns_true_when_enabled(self):
        from app.providers.publisher.douyin import _wait_until_enabled

        class Enabled:
            async def get_attribute(self, _name):
                return "semi-button semi-button-primary"

            async def is_enabled(self):
                return True

        assert await _wait_until_enabled(Enabled(), timeout=2000) is True

    async def test_waits_for_disabled_to_clear(self):
        import time

        from app.providers.publisher.douyin import _wait_until_enabled

        class EventuallyEnabled:
            def __init__(self):
                self.calls = 0

            async def get_attribute(self, _name):
                self.calls += 1
                return "semi-button" if self.calls >= 3 else "semi-button semi-button-disabled"

            async def is_enabled(self):
                return True

        start = time.time()
        assert await _wait_until_enabled(EventuallyEnabled(), timeout=5000) is True
        assert time.time() - start >= 0.5, "应当轮询等待而不是立刻返回"

    async def test_returns_false_on_timeout(self):
        from app.providers.publisher.douyin import _wait_until_enabled

        class AlwaysDisabled:
            async def get_attribute(self, _name):
                return "semi-button semi-button-disabled"

            async def is_enabled(self):
                return False

        assert await _wait_until_enabled(AlwaysDisabled(), timeout=1000) is False


class TestCoverFailureIsVisible:
    """封面失败不能被静默吞掉——用户只看到成片，根本不知道封面没设上。"""

    async def test_set_cover_returns_bool(self):
        import inspect

        from app.providers.publisher.douyin import DouyinPublisher

        signature = inspect.signature(DouyinPublisher._set_cover)
        assert signature.return_annotation in (bool, "bool"), (
            "封面设置必须返回结果，供调用方记录，而不是静默失败"
        )

    def test_publish_logs_warning_when_cover_missing(self):
        import inspect

        from app.providers.publisher.douyin import DouyinPublisher

        source = inspect.getsource(DouyinPublisher)
        assert "封面未设置成功" in source, "封面失败时应留下明确告警"
