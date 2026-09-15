"""抖音账号接口：登录状态、扫码登录、退出登录。"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db import get_session
from app.models import DouyinAccount, utcnow
from app.providers.publisher.douyin import auth_manager
from app.schemas import MessageOut
from app.services.settings_store import PublishConfig, settings_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/douyin", tags=["douyin"])

ACCOUNT_FILE = settings.auth_dir / "douyin_default.json"


async def _ensure_account(session: AsyncSession) -> DouyinAccount:
    row = (
        await session.execute(select(DouyinAccount).order_by(DouyinAccount.id))
    ).scalars().first()
    if row is None:
        row = DouyinAccount(
            name="default",
            nickname="抖音创作者账号",
            storage_state_path=str(ACCOUNT_FILE),
            is_default=True,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


@router.get("/account")
async def get_account(session: AsyncSession = Depends(get_session)) -> dict:
    account = await _ensure_account(session)
    config = await settings_store.get_section(session, "publish")
    return {
        "id": account.id,
        "name": account.name,
        "nickname": account.nickname,
        "status": account.status,
        "is_default": account.is_default,
        "storage_state_path": account.storage_state_path,
        "storage_state_exists": ACCOUNT_FILE.exists(),
        "last_login_at": account.last_login_at.isoformat() if account.last_login_at else None,
        "last_check_at": account.last_check_at.isoformat() if account.last_check_at else None,
        "provider": config.get("provider"),
        "login_session": auth_manager.snapshot(),
    }


@router.post("/login", response_model=MessageOut)
async def start_login(session: AsyncSession = Depends(get_session)) -> MessageOut:
    """启动扫码登录：会打开一个真实浏览器窗口，请用抖音 App 扫码。"""
    await _ensure_account(session)
    snapshot = auth_manager.snapshot()
    if snapshot.get("running"):
        return MessageOut(message="登录流程已在进行中，请在弹出的浏览器中完成扫码")

    ACCOUNT_FILE.parent.mkdir(parents=True, exist_ok=True)
    await auth_manager.start_login(ACCOUNT_FILE)
    return MessageOut(message="已打开登录浏览器，请使用抖音 App 扫码；扫码完成后状态会自动更新")


@router.get("/login/status")
async def login_status(session: AsyncSession = Depends(get_session)) -> dict:
    account = await _ensure_account(session)
    snapshot = auth_manager.snapshot()
    if snapshot.get("status") == "success" and account.status != "logged_in":
        account.status = "logged_in"
        account.last_login_at = utcnow()
        account.storage_state_path = str(ACCOUNT_FILE)
        await session.commit()
    return {"account_status": account.status, "login_session": snapshot}


@router.get("/login/qrcode")
async def login_qrcode() -> FileResponse:
    """返回登录窗口的截图（含二维码），方便无显示器/远程环境扫码。"""
    qrcode_path = getattr(auth_manager.session, "qrcode_path", "")
    if not qrcode_path:
        raise HTTPException(status_code=404, detail="二维码尚未生成")
    file_path = Path(qrcode_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="二维码文件不存在")
    return FileResponse(file_path, media_type="image/png")


@router.post("/check", response_model=MessageOut)
async def check_login(session: AsyncSession = Depends(get_session)) -> MessageOut:
    account = await _ensure_account(session)
    config = await settings_store.get_section(session, "publish")
    from app.providers import build_publisher

    publisher = build_publisher(PublishConfig(**config), ACCOUNT_FILE)
    check = getattr(publisher, "check_login", None)
    if check is None:
        return MessageOut(message=f"当前发布器（{publisher.name}）无需登录校验")
    ok, message = await check()
    account.status = "logged_in" if ok else "expired"
    account.last_check_at = utcnow()
    await session.commit()
    return MessageOut(ok=ok, message=message)


@router.post("/logout", response_model=MessageOut)
async def logout(session: AsyncSession = Depends(get_session)) -> MessageOut:
    account = await _ensure_account(session)
    if ACCOUNT_FILE.exists():
        ACCOUNT_FILE.unlink()
    account.status = "logged_out"
    account.last_login_at = None
    await session.commit()
    return MessageOut(message="已清除本地登录态，如需发布请重新扫码登录")
