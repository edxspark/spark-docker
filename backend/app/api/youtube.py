"""YouTube 登录与 cookies 接口。

匿名下载 YouTube 会撞「Sign in to confirm you're not a bot」风控，需要 cookies。
本模块让用户在应用内开一个浏览器窗口登录一次，然后把登录态导出成 yt-dlp 能用的
Netscape cookies.txt，并用一次真实解析验证它确实有效。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db import get_session
from app.schemas import MessageOut
from app.services import youtube_auth
from app.services.settings_store import settings_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/youtube", tags=["youtube"])

STATE_FILE = settings.auth_dir / "youtube.json"
COOKIES_FILE = settings.auth_dir / "youtube_cookies.txt"
# 验证用的样本视频：公开、短、可稳定解析
PROBE_URL = "https://www.youtube.com/watch?v=EN7frwQIbKc"


@router.get("/status")
async def status(session: AsyncSession = Depends(get_session)) -> dict:
    config = await settings_store.get_section(session, "download")
    configured = str(config.get("cookies_file") or "")
    return {
        "configured_cookies_file": configured,
        "configured_exists": bool(configured) and settings.data_dir.joinpath(configured).exists(),
        "builtin_cookies_path": str(COOKIES_FILE),
        "builtin_cookies_exists": COOKIES_FILE.exists(),
        "builtin_cookies_size": COOKIES_FILE.stat().st_size if COOKIES_FILE.exists() else 0,
        "state_exists": STATE_FILE.exists(),
        "login_session": youtube_auth.auth_manager.session.snapshot(),
    }


@router.post("/login", response_model=MessageOut)
async def start_login() -> MessageOut:
    """打开一个真实浏览器窗口登录 Google。

    登录成功后会导出 cookies 并自动验证；不需要用户手动填任何路径。
    """
    snapshot = youtube_auth.auth_manager.session.snapshot()
    if snapshot.get("running"):
        return MessageOut(message="登录流程已在进行中，请在弹出的浏览器里完成登录")

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    await youtube_auth.auth_manager.start_login(
        STATE_FILE, COOKIES_FILE, probe_url=PROBE_URL
    )
    return MessageOut(
        message="已打开浏览器，请登录 Google 账号；登录完成后会自动导出并验证 cookies"
    )


@router.get("/login/status")
async def login_status() -> dict:
    return youtube_auth.auth_manager.session.snapshot()


@router.post("/check", response_model=MessageOut)
async def check() -> MessageOut:
    """用一次真实解析验证现有 cookies 是否还有效（cookie 会过期）。"""
    if not COOKIES_FILE.exists():
        raise HTTPException(
            status_code=404,
            detail="还没有导出过 cookies，请先点「登录 YouTube 并导出 cookies」",
        )
    ok, detail = await youtube_auth.auth_manager.check(COOKIES_FILE, probe_url=PROBE_URL)
    return MessageOut(ok=ok, message=detail)


@router.get("/cookies")
async def download_cookies() -> FileResponse:
    """把导出的 cookies.txt 交给用户，便于在新机器上复用。"""
    if not COOKIES_FILE.exists():
        raise HTTPException(status_code=404, detail="尚未导出 cookies")
    return FileResponse(
        COOKIES_FILE, media_type="text/plain", filename="youtube_cookies.txt"
    )


@router.post("/logout", response_model=MessageOut)
async def logout() -> MessageOut:
    for path in (COOKIES_FILE, STATE_FILE):
        if path.exists():
            path.unlink()
    return MessageOut(message="已清除 YouTube 登录态与 cookies")
