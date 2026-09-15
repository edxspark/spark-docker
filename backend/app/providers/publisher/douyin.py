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
    try:
        from playwright.async_api import async_playwright  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise ProviderError(
            "未安装 playwright：请执行 `pip install playwright && playwright install chromium`"
        ) from exc
    return async_playwright


async def _first_visible(page, selectors: tuple[str, ...], *, timeout: float = 3000):
    """按候选顺序返回第一个匹配到的 locator。"""
    per_selector = max(0.4, timeout / 1000 / max(1, len(selectors)))
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.count() == 0:
                continue
            await locator.wait_for(state="visible", timeout=per_selector * 1000)
            return locator
        except Exception:  # noqa: BLE001 - 逐个候选试错
            continue
    return None


async def _exists(page, selectors: tuple[str, ...]) -> bool:
    for selector in selectors:
        try:
            if await page.locator(selector).count() > 0:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _launch_options(headless: bool) -> dict:
    """chromium.launch 只接受启动级参数；viewport/locale 属于 new_context。"""
    return {
        "headless": headless,
        "args": [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
    }


async def _new_context(playwright, storage_state: Path | None, headless: bool):
    browser = await playwright.chromium.launch(**_launch_options(headless))
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
            return await self._publish_locked(request, video_path)

    async def _publish_locked(self, request: PublishRequest, video_path: Path) -> PublishResult:
        async_playwright = _require_playwright()
        headless = request.headless or self.config.headless
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

                # 1) 选择文件并等待跳转到发布页
                upload_input = await _first_visible(page, UPLOAD_INPUT_SELECTORS, timeout=60000)
                if upload_input is None:
                    raise ProviderError("未找到上传入口，抖音创作者中心页面结构可能已改版")
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

                # 7) 发布
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
                upload_input = await _first_visible(page, UPLOAD_INPUT_SELECTORS, timeout=10000)
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
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000
        while loop.time() < deadline:
            button = await _first_visible(page, PUBLISH_BUTTON_SELECTORS, timeout=5000)
            if button is not None:
                try:
                    await button.click(timeout=5000)
                except Exception:  # noqa: BLE001
                    await page.get_by_role("button", name="发布", exact=True).click(force=True)
            try:
                await page.wait_for_url(MANAGE_URL_GLOB, timeout=5000)
                return
            except Exception:  # noqa: BLE001
                # 可能弹出「请设置封面后再发布」等拦截
                if await _exists(page, ("text=请设置封面后再发布",)):
                    await self._set_cover(page, PublishRequest(video_path=Path(), title="", cover_path=None))
                await page.wait_for_timeout(1000)
        raise ProviderError("点击发布后未跳转到作品管理页，请在抖音后台确认发布状态")

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
