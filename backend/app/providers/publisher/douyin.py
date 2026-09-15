"""抖音创作者中心自动发布（Playwright 浏览器自动化）。

抖音没有面向个人创作者的官方开放 API，因此采用浏览器自动化：
- 首次使用需扫码登录，登录态保存为 Playwright storage_state（data/auth/douyin_*.json）。
- 上传视频 → 填标题/正文/话题 → 可选定时发布 → 点击「发布」→ 等待跳转作品管理页。

选择器参考并兼容抖音创作者中心 v1(post) / v2(post/video) 两版发布页，
平台改版时只需调整下面的 *_SELECTORS 列表。

注意：自动化发布存在平台风控风险，请控制频率、如实勾选 AI 生成声明。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app.providers.base import (
    BasePublisher,
    ProviderError,
    PublishRequest,
    PublishResult,
)
from app.services.settings_store import PublishConfig

logger = logging.getLogger(__name__)

UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"
LOGIN_URL = "https://creator.douyin.com/"
MANAGE_URL_GLOB = "https://creator.douyin.com/creator-micro/content/manage**"
PUBLISH_URL_PATTERNS = (
    "https://creator.douyin.com/creator-micro/content/publish?enter_from=publish_page",
    "https://creator.douyin.com/creator-micro/content/post/video?enter_from=publish_page",
)

# 每个语义操作给多个候选选择器，按顺序尝试，任一命中即可（平台改版时只需改这里）
UPLOAD_INPUT_SELECTORS = (
    # 实测 2026-09 的页面：
    #   <input type="file" accept="video/x-flv,video/mp4,...,video/*,...">，无 class/id，尺寸 1x1
    'input[type="file"][accept*="video"]',
    'input[accept*="video"]',
    "input.upload-btn-input",
    "div[class^='container'] input[accept]",
    "div[class^='container'] input[type='file']",
    "div[class^='container'] input.upload-input",
    "input[type='file']",
)
TITLE_INPUT_SELECTORS = (
    'input[placeholder*="填写作品标题"]',
    'input[placeholder*="作品标题"]',
    "input.semi-input",
)
DESCRIPTION_SELECTORS = (
    'div.zone-container[contenteditable="true"]',
    'div[contenteditable="true"]',
)
SCHEDULE_RADIO_SELECTORS = (
    "[class^='radio']:has-text('定时发布')",
    "label:has-text('定时发布')",
    "div:has-text('定时发布') >> nth=0",
)
SCHEDULE_INPUT_SELECTORS = (
    '.semi-input[placeholder="日期和时间"]',
    'input[placeholder="日期和时间"]',
)
# 提交按钮必须精确匹配文本。
# 事故复盘：原先用 button:has-text("发布") —— 子串匹配会同时命中左侧导航项
# 「作品发布」（文本里含「发布」二字），而 .first 拿到的正是那个导航项，
# 于是「点发布」实际是点了导航，页面跳到 content/upload，作品根本没提交，
# 却表现为「点击后未跳转到作品管理页」，排查方向被完全带偏。
# 实测该页面上 has-text 命中 2 个，text-is 精确命中 1 个。
PUBLISH_BUTTON_SELECTORS = (
    'button:text-is("发布")',
    'button.semi-button-primary:text-is("发布")',
    'button.semi-button:text-is("发布")',
)
UPLOAD_DONE_SELECTORS = (
    '[class^="long-card"] div:has-text("重新上传")',
    'text=重新上传',
)
UPLOAD_FAILED_SELECTORS = (
    'div.progress-div > div:has-text("上传失败")',
    'text=上传失败',
)

# 封面弹窗内的上传输入框。
# 弹窗里有两个 input.semi-upload-hidden-input（各自还带一个 -replace 兄弟）：
#   ① 左侧「生成参考图」——AI 封面参考图槽，拖拽区 class 为 semi-upload-drag-area-custom
#   ② 帧选择区「上传封面」——拖拽区含 .semi-upload-drag-area-main-text
# 若用 .first 取到 ①，封面会被塞进 AI 参考图槽：真封面没设上，成片仍是黑封面，
# 而且弹窗里的检测会一直转、「完成」按钮永远不解禁。
COVER_UPLOAD_INPUT_SELECTOR = (
    ".semi-upload:has(.semi-upload-drag-area-main-text) input.semi-upload-hidden-input"
)
COVER_MODAL_SELECTOR = "div.dy-creator-content-modal"
COVER_TRIGGER_TEXTS = ("编辑封面", "选择封面", "设置封面")
COVER_AREA_SELECTORS = (
    '[class*="cover-"]',
    '[class*="cover"]',
)
# 抖音的新手引导浮层会拦截封面区的点击，导致弹窗打不开
ONBOARDING_SELECTORS = (
    ".shepherd-element",
    ".shepherd-modal-overlay-container",
    "[class*='shepherd']",
)


# 需要人工介入的风控/校验：无头模式下无法自动通过，命中后应降级为有头
VERIFICATION_SELECTORS = (
    "text=身份验证",
    "text=请完成安全验证",
    "text=获取验证码",
    "text=短信验证",
    "div.uc-ui-verify_sms-verify_button",
    "text=拖动滑块",
    "text=验证码",
)

_HUMAN_MARKERS = ("身份验证", "安全验证", "验证码", "滑块", "验证")


def _needs_human(exc: Exception) -> bool:
    """判断该失败是否属于「必须人工过校验」，用于决定是否降级为有头模式。"""
    message = str(exc)
    return any(marker in message for marker in _HUMAN_MARKERS)

# 反自动化检测：抹掉最常见的可检测特征
STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
if (originalQuery) {
  window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
      ? Promise.resolve({state: Notification.permission})
      : originalQuery(parameters)
  );
}
"""


def _require_playwright():
    """返回浏览器驱动入口。

    优先 patchright：它是 Playwright 的 stealth 分支，去掉了 CDP 检测特征。
    参考实现（social-auto-upload）用的正是它，抖音对原生 Playwright 的自动化
    指纹更敏感，表现为「页面正常打开但关键按钮点了没反应」这类静默失败。
    未安装时退回 playwright。
    """
    try:
        from patchright.async_api import async_playwright  # noqa: PLC0415

        logger.debug("使用 patchright（stealth 驱动）")
        return async_playwright
    except ImportError:
        pass
    try:
        from playwright.async_api import async_playwright  # noqa: PLC0415

        logger.info("未安装 patchright，回退到 playwright；建议 `uv pip install patchright` 以降低被识别风险")
        return async_playwright
    except ImportError as exc:  # pragma: no cover
        raise ProviderError(
            "未安装浏览器驱动：请执行 `uv pip install patchright` 并 `patchright install chromium`"
        ) from exc


async def _resolve_publish_button(page, *, timeout: float = 5000):
    """定位「发布」提交按钮。

    优先用 get_by_role(name, exact=True)：只有精确文本匹配才能把提交按钮
    和文本里同样包含「发布」的左侧导航项「作品发布」区分开。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout / 1000
    while loop.time() < deadline:
        for locator in (
            page.get_by_role("button", name="发布", exact=True).last,
            page.locator('button:text-is("发布")').last,
        ):
            try:
                if await locator.count():
                    return locator
            except Exception:  # noqa: BLE001
                continue
        await asyncio.sleep(0.3)
    return None


async def _dismiss_onboarding(page) -> None:
    """清掉新手引导浮层。

    shepherd 引导层会盖在封面区上方拦截点击，表现为「封面弹窗打不开」。
    """
    for selector in ONBOARDING_SELECTORS:
        try:
            locator = page.locator(selector)
            count = await locator.count()
            for i in range(count):
                item = locator.nth(i)
                if await item.is_visible():
                    await item.evaluate(
                        "el => el.parentNode && el.parentNode.removeChild(el)"
                    )
        except Exception:  # noqa: BLE001
            continue


async def _wait_until_enabled(locator, *, timeout: float = 20000) -> bool:
    """等按钮解除禁用。

    封面上传后抖音要处理图片，期间「完成」带 semi-button-disabled，点了无效——
    直接点会导致弹窗关不掉，进而挡住后面的发布按钮。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout / 1000
    while loop.time() < deadline:
        try:
            cls = (await locator.get_attribute("class")) or ""
            if "semi-button-disabled" not in cls and await locator.is_enabled():
                return True
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.4)
    return False


async def _dismiss_overlays(page) -> None:
    """关掉可能挡住点击的浮层。

    发布页会残留 semi-portal 之类的浮层（公告、提示、上一步打开的对话框）。
    它们不一定可见，但会拦截指针事件，让 Playwright 的可操作性检查失败。
    """
    with contextlib.suppress(Exception):
        await page.keyboard.press("Escape")
    for selector in (
        ".semi-modal-close",
        ".semi-sidesheet-close",
        'button[aria-label="关闭"]',
        'button[aria-label="Close"]',
    ):
        try:
            locator = page.locator(selector).first
            if await locator.count() and await locator.is_visible():
                await locator.click(timeout=2000)
                await page.wait_for_timeout(300)
        except Exception:  # noqa: BLE001 - 关不掉也不该中断流程
            continue


async def _can_click_at(page, locator) -> bool:
    """判断元素中心点是否真的接收点击（elementFromPoint 命中的是它自身或后代）。

    这一步很关键：元素存在、甚至 is_enabled() 为真，都不代表点得到——
    它可能在视口之外（实测发布按钮 y=1292 而视口高 900），或被浮层遮住。
    此时点击会静默落空，表现为「点了发布但页面没反应」。
    """
    try:
        box = await locator.bounding_box()
        if not box or box["width"] <= 0 or box["height"] <= 0:
            return False
        return bool(
            await locator.evaluate(
                """el => {
                    const r = el.getBoundingClientRect();
                    const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
                    if (cy < 0 || cy > window.innerHeight || cx < 0 || cx > window.innerWidth) return false;
                    const top = document.elementFromPoint(cx, cy);
                    return !!(top && (el === top || el.contains(top)));
                }"""
            )
        )
    except Exception:  # noqa: BLE001
        return False


async def _native_click(page, locator) -> bool:
    """用真实鼠标事件点击，并补发完整的 pointer/mouse 事件序列。

    抖音部分自定义组件只认这套事件（单纯 click() 会被静默忽略）。
    点击前必须先把元素滚进视口——否则坐标落在视口外，点击等于没点。
    """
    try:
        await locator.scroll_into_view_if_needed(timeout=5000)
    except Exception:  # noqa: BLE001
        pass
    await page.wait_for_timeout(250)

    if not await _can_click_at(page, locator):
        return False

    box = await locator.bounding_box()
    if not box:
        return False
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    try:
        await page.mouse.move(x, y)
        await page.wait_for_timeout(120)
        await page.mouse.click(x, y)
        await page.wait_for_timeout(150)
        await page.evaluate(
            """({x, y}) => {
                const el = document.elementFromPoint(x, y);
                if (!el) return;
                const opts = {bubbles:true,cancelable:true,composed:true,clientX:x,clientY:y,
                              view:window,pointerId:1,pointerType:'mouse',isPrimary:true,button:0,buttons:1};
                for (const t of ['pointerover','pointerenter','pointerdown','mousedown',
                                 'pointerup','mouseup','click']) {
                    const C = t.startsWith('pointer') ? PointerEvent : MouseEvent;
                    try { el.dispatchEvent(new C(t, opts)); }
                    catch (e) { try { el.dispatchEvent(new MouseEvent(t, opts)); } catch (_) {} }
                }
            }""",
            {"x": x, "y": y},
        )
        return True
    except Exception:  # noqa: BLE001
        return False


async def _first_visible(page, selectors: tuple[str, ...], *, timeout: float = 3000, state: str = "visible"):
    """按候选顺序轮询，返回第一个命中且达到指定状态的 locator。

    必须在整个 timeout 内反复轮询，而不是「扫一遍没命中就放弃」——
    创作者中心是 SPA，上传控件要几秒才渲染出来。实测页面加载后 2 秒时
    `input[type=file]` 还不存在；一次性检查会让所有候选瞬间落空，
    最终误报「未找到上传入口，页面结构可能已改版」。

    state 默认 visible；文件输入框常被样式隐藏（实测尺寸仅 1x1），
    这类元素应传 state="attached"——set_input_files 对隐藏输入同样有效。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.5, timeout / 1000)
    while loop.time() < deadline:
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if await locator.count() == 0:
                    continue
                await locator.wait_for(state=state, timeout=1500)
                return locator
            except Exception:  # noqa: BLE001 - 逐个候选试错
                continue
        await asyncio.sleep(0.4)
    return None


async def _exists(page, selectors: tuple[str, ...]) -> bool:
    for selector in selectors:
        try:
            if await page.locator(selector).count() > 0:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _launch_options(headless: bool, *, channel: str | None = "chromium") -> dict:
    """chromium.launch 只接受启动级参数；viewport/locale 属于 new_context。

    channel="chromium" 用真实 Chromium 构建，而不是 Playwright 默认下载的
    "Chromium for Testing"——后者带有已知的自动化标记，容易被平台识别。
    """
    options: dict = {
        "headless": headless,
        "args": [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
    }
    if channel:
        options["channel"] = channel
    return options


async def _new_context(playwright, storage_state: Path | None, headless: bool):
    try:
        browser = await playwright.chromium.launch(**_launch_options(headless))
    except Exception as exc:  # noqa: BLE001
        # channel 未安装等情况下退回默认构建，保证流程仍可运行
        logger.warning("channel=chromium 启动失败（%s），回退到默认 Chromium", str(exc)[:150])
        browser = await playwright.chromium.launch(**_launch_options(headless, channel=None))
    options: dict = {
        "viewport": {"width": 1440, "height": 900},
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }
    if storage_state and Path(storage_state).exists():
        options["storage_state"] = str(storage_state)
    context = await browser.new_context(**options)
    await context.add_init_script(STEALTH_SCRIPT)
    return browser, context


# --------------------------------------------------------------------------------------
# 登录会话（供 API 轮询状态）
# --------------------------------------------------------------------------------------


@dataclass
class LoginSession:
    status: str = "idle"          # idle / waiting / success / failed / expired
    message: str = ""
    qrcode_path: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    _task: asyncio.Task | None = field(default=None, repr=False)

    def running(self) -> bool:
        return self._task is not None and not self._task.done()


class DouyinAuthManager:
    """管理扫码登录会话：一次只允许一个会话。"""

    def __init__(self) -> None:
        self.session = LoginSession()

    async def start_login(self, account_file: Path, *, timeout: int = 300) -> LoginSession:
        if self.session.running():
            return self.session
        self.session = LoginSession(status="waiting", message="等待扫码登录…", started_at=datetime.now())
        self.session._task = asyncio.create_task(self._login_worker(account_file, timeout))
        return self.session

    async def _login_worker(self, account_file: Path, timeout: int) -> None:
        session = self.session
        async_playwright = _require_playwright()
        try:
            async with async_playwright() as playwright:
                browser, context = await _new_context(playwright, None, headless=False)
                try:
                    page = await context.new_page()
                    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=90000)
                    # 截图里的二维码同时落盘，方便无显示器场景
                    qrcode_path = account_file.with_suffix(".qrcode.png")
                    try:
                        await page.wait_for_timeout(2500)
                        await page.screenshot(path=str(qrcode_path))
                        session.qrcode_path = str(qrcode_path)
                    except Exception:  # noqa: BLE001
                        pass

                    deadline = asyncio.get_running_loop().time() + timeout
                    while asyncio.get_running_loop().time() < deadline:
                        if "creator.douyin.com/creator-micro" in page.url:
                            break
                        if await _exists(page, ('text=二维码失效', "text=二维码已失效")):
                            session.status = "expired"
                            session.message = "二维码已失效，请重新发起登录"
                            return
                        await page.wait_for_timeout(2000)
                    else:
                        session.status = "failed"
                        session.message = f"登录超时（{timeout}s），请重试"
                        return

                    account_file.parent.mkdir(parents=True, exist_ok=True)
                    await context.storage_state(path=str(account_file))
                    session.status = "success"
                    session.message = "登录成功，登录态已保存"
                finally:
                    await context.close()
                    await browser.close()
        except Exception as exc:  # noqa: BLE001
            logger.exception("抖音登录失败")
            session.status = "failed"
            session.message = f"登录失败：{exc}"
        finally:
            session.finished_at = datetime.now()

    def snapshot(self) -> dict:
        return {
            "status": self.session.status,
            "message": self.session.message,
            "qrcode_ready": bool(self.session.qrcode_path),
            "running": self.session.running(),
            "started_at": self.session.started_at.isoformat() if self.session.started_at else None,
            "finished_at": self.session.finished_at.isoformat() if self.session.finished_at else None,
        }


auth_manager = DouyinAuthManager()


# --------------------------------------------------------------------------------------
# 发布
# --------------------------------------------------------------------------------------


class DouyinPublisher(BasePublisher):
    name = "douyin"

    def __init__(self, config: PublishConfig, account_file: Path) -> None:
        self.config = config
        self.account_file = Path(account_file)
        # 同一账号的发布串行化，避免并发操作同一登录态被风控
        self._lock = asyncio.Lock()

    # -- 登录态 ----------------------------------------------------------------

    async def check_login(self) -> tuple[bool, str]:
        if not self.account_file.exists():
            return False, "尚未登录，请先扫码登录抖音创作者账号"
        async_playwright = _require_playwright()
        async with async_playwright() as playwright:
            browser, context = await _new_context(playwright, self.account_file, headless=True)
            try:
                page = await context.new_page()
                await page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=90000)
                await page.wait_for_timeout(2500)
                if await _exists(page, ('text=扫码登录', 'text=手机号登录', 'text=二维码失效')):
                    return False, "登录态已失效，请重新扫码登录"
                if "content/upload" in page.url:
                    return True, "登录态有效"
                return False, f"无法确认登录状态（当前地址：{page.url}）"
            except Exception as exc:  # noqa: BLE001
                return False, f"登录态校验失败：{exc}"
            finally:
                await context.close()
                await browser.close()

    # -- 发布 ------------------------------------------------------------------

    async def publish(self, request: PublishRequest) -> PublishResult:
        video_path = Path(request.video_path)
        if not video_path.exists():
            raise ProviderError(f"视频文件不存在：{video_path}")
        if not request.title.strip():
            raise ProviderError("发布标题不能为空")

        async with self._lock:
            headless = request.headless or self.config.headless
            if not headless:
                return await self._publish_locked(request, video_path, headless=False)

            # 无头优先：后台静默完成，不弹出浏览器窗口。
            # 但无头更容易触发平台的身份验证/验证码——这类校验必须有可见窗口才能人工过。
            # 因此命中风控时自动降级为「有头」重试一次，让用户接手，而不是直接失败。
            try:
                return await self._publish_locked(request, video_path, headless=True)
            except ProviderError as exc:
                if not self.config.headless_fallback_to_visible or not _needs_human(exc):
                    raise
                logger.warning("无头发布遇到人工校验，改为有头模式重试：%s", str(exc)[:200])
                return await self._publish_locked(request, video_path, headless=False)

    async def _publish_locked(
        self, request: PublishRequest, video_path: Path, *, headless: bool
    ) -> PublishResult:
        async_playwright = _require_playwright()
        timeout_ms = self.config.timeout * 1000

        async with async_playwright() as playwright:
            browser, context = await _new_context(playwright, self.account_file, headless)
            page = None
            failure: Exception | None = None
            try:
                page = await context.new_page()
                page.set_default_timeout(min(timeout_ms, 120000))

                await page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=90000)
                await page.wait_for_timeout(2000)
                if await _exists(page, ('text=扫码登录', 'text=手机号登录')):
                    raise ProviderError("抖音登录态已失效，请先在「抖音账号」页重新扫码登录")
                # 风控校验（短信验证 / 身份验证 / 滑块）：无头下无法通过，需要人工介入
                if await _exists(page, VERIFICATION_SELECTORS):
                    raise ProviderError(
                        "抖音要求完成身份验证（短信或滑块），无头模式无法自动通过。"
                        "请保持「无头模式」关闭后重试，在弹窗中人工完成验证"
                    )

                logger.info("[发布] 1/6 定位上传入口…")
                # 文件输入框在页面上是 1x1 的隐藏元素，用 attached 而非 visible
                upload_input = await _first_visible(
                    page, UPLOAD_INPUT_SELECTORS, timeout=60000, state="attached"
                )
                if upload_input is None:
                    # 附上当期页面的实际 input 情况，避免下次只能靠猜
                    try:
                        probe = await page.evaluate(
                            """() => Array.from(document.querySelectorAll('input')).map(el => ({
                                type: el.type, accept: (el.accept || '').slice(0, 40),
                                cls: String(el.className || '').slice(0, 60),
                            }))"""
                        )
                    except Exception:  # noqa: BLE001
                        probe = "（无法读取页面信息）"
                    raise ProviderError(
                        f"未找到上传入口。当前页面 URL={page.url}，"
                        f"页面上的 input 元素={probe}。"
                        "若确实已改版，请更新 douyin.py 顶部的 UPLOAD_INPUT_SELECTORS"
                    )
                await upload_input.set_input_files(str(video_path))
                logger.info("[发布] 2/6 等待进入发布页…")
                await self._wait_publish_page(page, timeout_ms)
                logger.info("[发布] 2/6 已进入发布页")

                # 2) 标题 / 正文 / 话题
                logger.info("[发布] 3/6 填标题与正文…")
                await self._fill_title_and_description(
                    page, request.title, request.description, request.tags
                )

                # 3) 等待上传完成（长视频耗时较长）
                logger.info("[发布] 4/6 等待视频上传完成（超时 %ss）…", self.config.timeout)
                await self._wait_upload_finished(page, video_path, timeout_ms)
                logger.info("[发布] 4/6 视频上传完成")

                logger.info("[发布] 5/6 设置封面与声明…")
                # 4) 封面。失败不阻断发布，但必须如实记录——
                #    封面没设上会直接导致发布出去的视频显示黑底，用户光看成片看不出原因。
                cover_ok = await self._set_cover(page, request)
                if not cover_ok:
                    logger.warning(
                        "封面未设置成功，发布后的作品可能没有封面（平台显示黑底）。"
                        "可到创作者中心手动设置，或检查成片是否已生成封面图"
                    )

                # 5) AI 生成声明（本流水线含 AI 配音与 AI 字幕，如实声明）
                await self._apply_ai_declaration(page)

                # 6) 定时发布
                if request.schedule_at:
                    await self._set_schedule(page, request.schedule_at)

                logger.info("[发布] 6/6 点击发布…")
                # 7) 发布（干跑时到此为止）
                if request.dry_run:
                    checks = await self._dry_run_report(page)
                    screenshot = state_path = None
                    try:
                        state_path = Path(self.account_file).with_suffix(".dryrun.png")
                        await page.screenshot(path=str(state_path), full_page=True)
                        screenshot = str(state_path)
                    except Exception:  # noqa: BLE001 - 截图失败不影响结论
                        screenshot = None
                    await context.storage_state(path=str(self.account_file))
                    return PublishResult(
                        success=True,
                        work_url="",
                        message=(
                            "干跑通过：已上传视频并填好标题/正文/话题，"
                            "停在「发布」按钮前，未真正发布。"
                            f"校验结果 {checks}"
                        ),
                        raw={"dry_run": True, "checks": checks, "screenshot": screenshot},
                    )

                await self._click_publish(page, timeout_ms)

                work_url = ""
                if self.config.collect_url:
                    work_url = await self._collect_work_url(page)

                await context.storage_state(path=str(self.account_file))
                return PublishResult(
                    success=True,
                    work_url=work_url,
                    message="发布成功" + (f"（定时 {request.schedule_at}）" if request.schedule_at else ""),
                    raw={"headless": headless},
                )
            except ProviderError as exc:
                failure = exc
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("抖音发布失败")
                failure = exc
                raise ProviderError(f"抖音发布失败：{exc}") from exc
            finally:
                # 可见模式下失败时把窗口留住：否则窗口一闪而过，
                # 用户既看不到卡在哪一步，也没机会接手处理验证码。
                if failure is not None and page is not None and not headless:
                    await self._hold_for_inspection(page, failure)
                await context.close()
                await browser.close()

    async def _hold_for_inspection(self, page, failure: Exception) -> None:
        """失败时保留浏览器窗口，供人工查看当前页面并处理验证码。

        等待期间若用户手动关闭了窗口则立即结束；也支持在窗口里直接操作
        （例如通过滑块验证），操作完关掉窗口即可。
        """
        if not self.config.keep_browser_on_failure:
            return
        hold = int(self.config.failure_hold_seconds or 0)
        if hold <= 0:
            return

        screenshot_path = Path(self.account_file).with_suffix(".failed.png")
        try:
            await page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception:  # noqa: BLE001
            screenshot_path = None  # type: ignore[assignment]

        logger.warning(
            "发布失败，浏览器窗口将保留 %s 秒供你查看（截图：%s）：%s",
            hold,
            screenshot_path or "未保存",
            str(failure)[:200],
        )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + hold
        while loop.time() < deadline:
            if page.is_closed():
                logger.info("检测到窗口已关闭，提前结束等待")
                return
            await asyncio.sleep(1)

    async def _dry_run_report(self, page) -> dict:
        """干跑时的关键校验：确认走到发布页、标题已填、上传已完成、发布按钮可点。"""
        checks: dict[str, object] = {"url": page.url}
        try:
            title_input = await _first_visible(page, TITLE_INPUT_SELECTORS, timeout=5000)
            checks["title_input_found"] = title_input is not None
            if title_input is not None:
                checks["title_value"] = (await title_input.input_value())[:60]
        except Exception as exc:  # noqa: BLE001
            checks["title_error"] = str(exc)[:120]

        checks["editor_found"] = (
            await _first_visible(page, DESCRIPTION_SELECTORS, timeout=5000)
        ) is not None

        upload_done = await _exists(page, UPLOAD_DONE_SELECTORS)
        checks["upload_finished"] = upload_done
        checks["upload_failed"] = await _exists(page, UPLOAD_FAILED_SELECTORS)

        publish_button = await _first_visible(page, PUBLISH_BUTTON_SELECTORS, timeout=5000)
        checks["publish_button_found"] = publish_button is not None
        if publish_button is not None:
            try:
                checks["publish_button_enabled"] = await publish_button.is_enabled()
            except Exception:  # noqa: BLE001
                checks["publish_button_enabled"] = None
        checks["needs_verification"] = await _exists(page, VERIFICATION_SELECTORS)
        return checks

    async def _wait_publish_page(self, page, timeout_ms: int) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000
        while loop.time() < deadline:
            for pattern in PUBLISH_URL_PATTERNS:
                try:
                    await page.wait_for_url(pattern, timeout=3000)
                    logger.info("已进入发布页：%s", page.url)
                    return
                except Exception:  # noqa: BLE001
                    continue
            if await _exists(page, ('text=上传失败', "text=文件格式不支持")):
                raise ProviderError("抖音侧上传失败：文件格式不支持或超出大小限制")
            await asyncio.sleep(0.5)
        raise ProviderError("等待抖音发布页超时，可能是视频过大或网络异常")

    async def _fill_title_and_description(
        self, page, title: str, description: str, tags: list[str]
    ) -> None:
        title_input = await _first_visible(page, TITLE_INPUT_SELECTORS, timeout=120000)
        if title_input is None:
            raise ProviderError("未找到标题输入框，发布页结构可能已改版")
        await title_input.fill(title[: self.config.title_max_len])

        editor = await _first_visible(page, DESCRIPTION_SELECTORS, timeout=60000)
        if editor is None:
            logger.warning("未找到正文编辑器，跳过正文与话题填写")
            return

        await editor.click()
        await page.keyboard.press("Control+KeyA")
        await page.keyboard.press("Delete")
        if description.strip():
            await page.keyboard.type(description.strip())
        for tag in tags:
            clean = tag.strip().lstrip("#")
            if not clean:
                continue
            await page.keyboard.type(f" #{clean}")
            await page.keyboard.press("Space")
            await page.wait_for_timeout(150)
        await page.keyboard.press("Escape")

    async def _wait_upload_finished(self, page, video_path: Path, timeout_ms: int) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000
        reported = 0.0
        while loop.time() < deadline:
            if await _exists(page, UPLOAD_DONE_SELECTORS):
                logger.info("视频上传完成：%s", video_path.name)
                return
            if await _exists(page, UPLOAD_FAILED_SELECTORS):
                # 重试一次：重新选择文件
                logger.warning("检测到上传失败，尝试重新上传")
                upload_input = await _first_visible(
                    page, UPLOAD_INPUT_SELECTORS, timeout=10000, state="attached"
                )
                if upload_input is None:
                    raise ProviderError("视频上传失败，且未找到重试入口")
                await upload_input.set_input_files(str(video_path))
                await asyncio.sleep(3)
            if loop.time() - reported > 15:
                reported = loop.time()
                logger.info("仍在等待抖音上传完成…")
            await asyncio.sleep(2)
        raise ProviderError(f"等待视频上传完成超时（{self.config.timeout}s）")

    async def _set_cover(self, page, request: PublishRequest) -> bool:
        """设置封面。成功返回 True。

        失败不再静默：封面设不上会直接导致发布出去的视频没有封面（平台显示黑底），
        用户只能看到成片却看不出原因，因此这里把结论如实返回给调用方记录。
        """
        cover = Path(request.cover_path) if request.cover_path else None
        if cover is None:
            logger.warning("本次发布没有指定封面文件，将使用平台推荐封面（视频帧）")
            return await self._use_recommended_cover(page)
        if not cover.exists():
            logger.error("封面文件不存在：%s —— 将退回平台推荐封面（视频帧）", cover)
            return await self._use_recommended_cover(page)
        logger.info("准备上传本地封面：%s（%.0f KB）", cover.name, cover.stat().st_size / 1024)

        # 1) 打开封面弹窗（引导浮层会拦截点击，先清掉）
        await _dismiss_onboarding(page)
        if not await self._open_cover_dialog(page):
            await _dismiss_onboarding(page)
            if not await self._open_cover_dialog(page):
                logger.error("封面弹窗打不开，本地封面未能上传，将退回平台推荐封面（视频帧）")
                return await self._use_recommended_cover(page)

        modal = page.locator(COVER_MODAL_SELECTOR).first
        if await modal.count() == 0:
            logger.error("未找到封面弹窗容器，本地封面未能上传，将退回平台推荐封面（视频帧）")
            return await self._use_recommended_cover(page)

        # 2) 定位「上传封面」的输入框。
        #    必须按拖拽区文案精确定位：弹窗里另一个隐藏 input 属于左侧
        #    「生成参考图」（AI 封面参考图）槽位，传错地方会导致真封面没设上、
        #    成片依旧没有封面，且「完成」按钮永远不解禁。
        upload = modal.locator(COVER_UPLOAD_INPUT_SELECTOR).first
        if await upload.count() == 0:
            upload = modal.locator("input.semi-upload-hidden-input").last
        if await upload.count() == 0:
            upload = modal.locator('input[type="file"]').last
        if await upload.count() == 0:
            logger.error("封面弹窗里没有找到上传入口，本地封面未能上传，将退回平台推荐封面（视频帧）")
            return await self._use_recommended_cover(page)

        # 3) 优先设置竖版封面（抖音推荐竖屏），弹窗默认就在该页
        try:
            portrait_tab = modal.get_by_text("设置竖封面", exact=True).first
            if await portrait_tab.count():
                await portrait_tab.click(timeout=3000)
                await page.wait_for_timeout(600)
        except Exception:  # noqa: BLE001 - 已在目标页时点击会失败，忽略
            pass

        # 记录上传前的封面预览状态，用于确认这次确实换掉了
        before_src = await self._cover_preview_src(page)
        await upload.set_input_files(str(cover))
        await page.wait_for_timeout(3000)
        after_src = await self._cover_preview_src(page)
        logger.info(
            "封面已提交到上传框：预览图 %s",
            "已更新" if (after_src and after_src != before_src) else "未检测到变化（继续等待处理）",
        )

        # 4) 等「完成」解禁：图片处理完之前它是 disabled，点了无效
        done = modal.get_by_role("button", name="完成", exact=True).first
        if await done.count() == 0:
            done = modal.locator('button:has-text("完成")').first
        if await done.count() == 0:
            logger.error("封面弹窗里没有「完成」按钮，本地封面未能上传，将退回平台推荐封面（视频帧）")
            return await self._use_recommended_cover(page)

        if not await _wait_until_enabled(done, timeout=20000):
            logger.warning("封面图处理超时，「完成」始终处于禁用状态")
            await _dismiss_overlays(page)
            return False

        if not await _native_click(page, done):
            try:
                await done.click(timeout=5000)
            except Exception:  # noqa: BLE001
                logger.warning("无法点击封面「完成」按钮")
                return False
        await page.wait_for_timeout(1800)

        # 5) 校验弹窗确实关掉了——它还开着的话会盖住发布按钮
        try:
            still_open = await page.locator(COVER_MODAL_SELECTOR).first.is_visible()
        except Exception:  # noqa: BLE001
            still_open = False
        if still_open:
            logger.warning("封面弹窗未能关闭，尝试用 Esc 收起")
            await _dismiss_overlays(page)
        logger.info("自定义封面上传完成：%s", cover.name)
        return True

    async def _cover_preview_src(self, page) -> str:
        """取封面预览图的 src，用于判断封面上传后预览是否真的变了。"""
        try:
            return await page.evaluate(
                """() => {
                    const img = document.querySelector('[class*="cover-"] img, [class*="cover"] img');
                    return img ? (img.currentSrc || img.src || '').slice(0, 200) : '';
                }"""
            )
        except Exception:  # noqa: BLE001
            return ""

    async def _use_recommended_cover(self, page) -> bool:
        """兜底：使用抖音推荐的封面帧，至少不会是没有封面的黑底。"""
        try:
            if not await _exists(page, ("text=请设置封面后再发布",)):
                return False
            recommend = page.locator('[class^="recommendCover-"]').first
            if await recommend.count():
                await _native_click(page, recommend)
                await page.wait_for_timeout(800)
                confirm = page.get_by_role("button", name="确定").first
                if await confirm.count():
                    await _native_click(page, confirm)
                    await page.wait_for_timeout(800)
                logger.info("已选用平台推荐封面")
                return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("选择推荐封面失败：%s", exc)
        return False

    async def _open_cover_dialog(self, page) -> bool:
        """打开封面编辑弹窗。

        封面区的点击入口只在 hover 后才浮现，且抖音自定义组件对普通 click 常静默
        失效，因此这里：hover → 用原生事件点击 → 校验弹窗是否真的出现 → 重试。
        """
        modal = page.locator(COVER_MODAL_SELECTOR).first
        if await modal.count() and await modal.is_visible():
            return True

        area = None
        for selector in COVER_AREA_SELECTORS:
            candidate = page.locator(selector).filter(has=page.locator("img")).first
            try:
                if await candidate.count():
                    area = candidate
                    break
            except Exception:  # noqa: BLE001
                continue
        if area is None:
            return False

        with contextlib.suppress(Exception):
            await area.scroll_into_view_if_needed(timeout=5000)
        for _ in range(3):
            with contextlib.suppress(Exception):
                await area.hover()
                await page.wait_for_timeout(600)

            trigger = None
            for text in COVER_TRIGGER_TEXTS:
                candidate = page.get_by_text(text, exact=True).first
                with contextlib.suppress(Exception):
                    if await candidate.count() and await candidate.is_visible():
                        trigger = candidate
                        break

            target = trigger if trigger is not None else area
            await _native_click(page, target)
            with contextlib.suppress(Exception):
                await modal.wait_for(state="visible", timeout=5000)
            if await modal.count() and await modal.is_visible():
                return True
            await page.wait_for_timeout(800)
        return False

    async def _apply_ai_declaration(self, page, declaration: str = "内容由AI生成") -> None:
        """如实勾选「内容由AI生成」自主声明；失败不阻断发布。"""
        try:
            entry = page.get_by_text("作品声明", exact=False).first
            if not await entry.count():
                return
            await entry.click(timeout=5000)
            await page.wait_for_timeout(1200)
            dialog = page.locator(".semi-modal-content, .semi-modal-body").first
            scope = dialog if await dialog.count() else page
            option = scope.get_by_text(declaration, exact=False).first
            if await option.count():
                await option.click(timeout=5000)
                await page.wait_for_timeout(500)
                confirm = scope.locator('button:has-text("确定")').first
                if await confirm.count():
                    await confirm.click()
                    await page.wait_for_timeout(800)
        except Exception as exc:  # noqa: BLE001
            logger.warning("AI 生成声明勾选失败（请发布前人工确认）：%s", exc)

    async def _set_schedule(self, page, schedule_at: str) -> None:
        radio = await _first_visible(page, SCHEDULE_RADIO_SELECTORS, timeout=10000)
        if radio is None:
            raise ProviderError("未找到「定时发布」选项，无法按定时方式发布")
        await radio.click()
        await page.wait_for_timeout(1200)
        field = await _first_visible(page, SCHEDULE_INPUT_SELECTORS, timeout=10000)
        if field is None:
            raise ProviderError("未找到定时时间输入框")
        await field.click()
        await page.keyboard.press("Control+KeyA")
        await page.keyboard.type(schedule_at)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(1000)

    async def _click_publish(self, page, timeout_ms: int) -> None:
        """点击「发布」并等待跳转到作品管理页。

        必须先把按钮滚进视口并校验其中心点真的接收点击。
        原实现直接 click(force=True) 作为兜底——force 不滚动也不做可操作性检查，
        在按钮位于首屏之外时（实测 y=1292、视口高 900）会在视口外点击，
        表现为「点了发布但页面毫无反应」，最后误报为「未跳转到作品管理页」。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000
        last_reason = "未找到发布按钮"

        while loop.time() < deadline:
            await _dismiss_overlays(page)

            button = await _resolve_publish_button(page)
            if button is not None:
                if await _native_click(page, button):
                    last_reason = "已点击发布按钮，但页面未跳转"
                else:
                    # 可达性不足时先修环境再重试，而不是在视口外空点
                    await button.scroll_into_view_if_needed()
                    await page.wait_for_timeout(500)
                    if await _native_click(page, button):
                        last_reason = "已点击发布按钮，但页面未跳转"
                    else:
                        last_reason = "发布按钮存在但无法点击（可能被浮层遮挡或在视口外）"

            try:
                await page.wait_for_url(MANAGE_URL_GLOB, timeout=6000)
                return
            except Exception:  # noqa: BLE001
                if await _exists(page, ("text=请设置封面后再发布",)):
                    await self._use_recommended_cover(page)
                await page.wait_for_timeout(1200)

        raise ProviderError(f"点击发布后未跳转到作品管理页（{last_reason}），请在抖音后台确认发布状态")

    async def _collect_work_url(self, page) -> str:
        try:
            await page.wait_for_timeout(2500)
            links = page.locator('a[href*="/video/"]')
            if await links.count():
                href = await links.first.get_attribute("href")
                if href:
                    return href if href.startswith("http") else f"https://www.douyin.com{href}"
        except Exception:  # noqa: BLE001
            pass
        return ""


class MockPublisher(BasePublisher):
    """离线演示：不打开浏览器，仅落盘一份发布记录。"""

    name = "mock"

    def __init__(self, config: PublishConfig | None = None, **_: object) -> None:
        self.config = config

    async def check_login(self) -> tuple[bool, str]:
        return True, "Mock 模式无需登录"

    async def publish(self, request: PublishRequest) -> PublishResult:
        await asyncio.sleep(1.0)
        if not Path(request.video_path).exists():
            raise ProviderError(f"视频文件不存在：{request.video_path}")
        title = request.title.strip()
        if not title:
            raise ProviderError("发布标题不能为空")
        return PublishResult(
            success=True,
            work_url=f"https://www.douyin.com/video/mock-{abs(hash(title)) % 10**12}",
            message=f"[Mock] 已模拟发布：{title[:20]}｜话题 {len(request.tags)} 个"
            + (f"｜定时 {request.schedule_at}" if request.schedule_at else ""),
            raw={"mock": True},
        )
