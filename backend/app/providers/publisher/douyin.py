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
PUBLISH_BUTTON_SELECTORS = (
    'button:has-text("发布")',
    'button.semi-button-primary:has-text("发布")',
)
UPLOAD_DONE_SELECTORS = (
    '[class^="long-card"] div:has-text("重新上传")',
    'text=重新上传',
)
UPLOAD_FAILED_SELECTORS = (
    'div.progress-div > div:has-text("上传失败")',
    'text=上传失败',
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

                # 1) 选择文件并等待跳转到发布页
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
                await self._wait_publish_page(page, timeout_ms)

                # 2) 标题 / 正文 / 话题
                await self._fill_title_and_description(
                    page, request.title, request.description, request.tags
                )

                # 3) 等待上传完成（长视频耗时较长）
                await self._wait_upload_finished(page, video_path, timeout_ms)

                # 4) 封面
                await self._set_cover(page, request)

                # 5) AI 生成声明（本流水线含 AI 配音与 AI 字幕，如实声明）
                await self._apply_ai_declaration(page)

                # 6) 定时发布
                if request.schedule_at:
                    await self._set_schedule(page, request.schedule_at)

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
            except ProviderError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("抖音发布失败")
                raise ProviderError(f"抖音发布失败：{exc}") from exc
            finally:
                await context.close()
                await browser.close()

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

    async def _set_cover(self, page, request: PublishRequest) -> None:
        """有自定义封面就上传；否则让抖音自动选推荐封面。"""
        try:
            if request.cover_path and Path(request.cover_path).exists():
                opened = await self._open_cover_dialog(page)
                if not opened:
                    return
                modal = page.locator("div.dy-creator-content-modal").first
                if await modal.count() == 0:
                    return
                file_input = modal.locator('input[type="file"]').last
                if await file_input.count():
                    await file_input.set_input_files(str(request.cover_path))
                    await page.wait_for_timeout(2500)
                done_button = modal.locator('button:has-text("完成")').first
                if await done_button.count():
                    await done_button.click()
                    await page.wait_for_timeout(1500)
            elif await _exists(page, ("text=请设置封面后再发布",)):
                recommend = page.locator('[class^="recommendCover-"]').first
                if await recommend.count():
                    await recommend.click()
                    await page.wait_for_timeout(800)
                    confirm = page.get_by_role("button", name="确定").first
                    if await confirm.count():
                        await confirm.click()
                        await page.wait_for_timeout(800)
        except Exception as exc:  # noqa: BLE001
            logger.warning("封面设置未完成（不影响发布主流程）：%s", exc)

    async def _open_cover_dialog(self, page) -> bool:
        candidates = ("text=选择封面", "text=编辑封面", "text=设置封面")
        for text in candidates:
            locator = page.get_by_text(text, exact=True).first
            try:
                if await locator.count():
                    await locator.click(timeout=5000)
                    await page.wait_for_timeout(1500)
                    return True
            except Exception:  # noqa: BLE001
                continue
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

            button = await _first_visible(page, PUBLISH_BUTTON_SELECTORS, timeout=5000)
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
                    await self._set_cover(
                        page, PublishRequest(video_path=Path(), title="", cover_path=None)
                    )
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
