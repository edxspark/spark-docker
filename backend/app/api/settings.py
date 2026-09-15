"""系统配置接口：读取（脱敏）、保存、重置、连通性自检。"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db import get_session
from app.providers import build_downloader, build_publisher, build_translator, build_tts
from app.providers.tts.aliyun import ALIYUN_VOICES
from app.schemas import MessageOut, TestResult
from app.services.settings_store import (
    DEFAULT_CONFIG,
    DownloadConfig,
    PublishConfig,
    TranslatorConfig,
    TTSConfig,
    settings_store,
)
from app.utils import binaries
from app.utils import ffmpeg as ffmpeg_utils

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
async def get_settings(session: AsyncSession = Depends(get_session)) -> dict:
    meta = await settings_store.describe()
    return {"config": await settings_store.public(session), **meta}


@router.put("")
async def update_settings(
    patch: dict,
    session: AsyncSession = Depends(get_session),
) -> dict:
    try:
        await settings_store.update(session, patch)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"配置校验失败：{exc}") from exc
    meta = await settings_store.describe()
    return {"config": await settings_store.public(session), **meta}


@router.post("/reset", response_model=MessageOut)
async def reset_settings(
    section: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> MessageOut:
    if section and section not in DEFAULT_CONFIG:
        raise HTTPException(status_code=400, detail=f"未知配置分组：{section}")
    await settings_store.reset(session, section)
    return MessageOut(message=f"已恢复默认配置{'：' + section if section else ''}")


@router.get("/voices")
async def list_voices() -> dict:
    return {"voices": ALIYUN_VOICES}


@router.get("/runtime")
async def runtime_info(refresh: bool = False) -> dict:
    """运行环境自检：ffmpeg / yt-dlp / JS 运行时 / impersonation / playwright。

    下载链路依赖较多，缺任何一项都会在下载阶段失败，因此这里一次性列全，
    让用户能在跑任务前就发现问题。
    """
    if refresh:
        binaries.clear_cache()

    ffmpeg_path = ffmpeg_utils.resolve_binary(settings.ffmpeg_bin) or ""
    ffprobe_path = ffmpeg_utils.resolve_binary(settings.ffprobe_bin) or ""
    runtime = binaries.resolve_js_runtime()
    info: dict = {
        "ffmpeg": {"available": bool(ffmpeg_path), "path": ffmpeg_path},
        "ffprobe": {"available": bool(ffprobe_path), "path": ffprobe_path},
        "js_runtime": {
            "available": runtime is not None,
            "name": runtime[0] if runtime else "",
            "path": runtime[1] if runtime else "",
        },
        "data_dir": str(settings.data_dir),
        "python": "",
    }
    try:
        import sys

        info["python"] = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    except Exception:  # noqa: BLE001
        pass
    try:
        import yt_dlp

        info["yt_dlp"] = {"available": True, "version": yt_dlp.version.__version__}
    except Exception:  # noqa: BLE001
        info["yt_dlp"] = {"available": False, "version": ""}

    # YouTube 提取的两个关键可选依赖
    try:
        import curl_cffi  # noqa: F401

        info["impersonation"] = {"available": True, "note": "curl-cffi 已安装，支持 TLS 指纹伪装"}
    except Exception:  # noqa: BLE001
        info["impersonation"] = {
            "available": False,
            "note": "缺少 curl-cffi，YouTube 可能拒绝请求：uv pip install curl-cffi",
        }
    try:
        import importlib.util

        has_ejs = importlib.util.find_spec("yt_dlp_ejs") is not None
        info["ejs"] = {
            "available": has_ejs,
            "note": "yt-dlp-ejs 已安装" if has_ejs else "缺少 yt-dlp-ejs，可能拿不到全部清晰度：uv pip install yt-dlp-ejs",
        }
    except Exception:  # noqa: BLE001
        info["ejs"] = {"available": False, "note": ""}

    try:
        import playwright  # noqa: F401

        info["playwright"] = {"available": True}
    except Exception:  # noqa: BLE001
        info["playwright"] = {"available": False}
    return info


@router.post("/test/{section}", response_model=TestResult)
async def test_section(
    section: str,
    session: AsyncSession = Depends(get_session),
) -> TestResult:
    """对某个配置分组做一次真实调用自检。"""
    config = await settings_store.get_section(session, section)
    started = time.perf_counter()

    if section == "translator":
        try:
            translator = build_translator(TranslatorConfig(**config))
            result = await translator.translate(["Hello world, this is a connectivity test."])
            elapsed = time.perf_counter() - started
            return TestResult(
                ok=True,
                message=f"翻译接口连通，耗时 {elapsed:.2f}s",
                detail={"provider": translator.name, "sample": result.texts[:1], "usage": result.usage},
            )
        except Exception as exc:  # noqa: BLE001
            return TestResult(ok=False, message=f"翻译接口测试失败：{exc}")

    if section == "tts":
        try:
            tts = build_tts(TTSConfig(**config))
            out = settings.data_dir / "logs" / "tts_test.mp3"
            result = await tts.synthesize("这是一段语音合成测试。", out)
            elapsed = time.perf_counter() - started
            return TestResult(
                ok=True,
                message=f"语音合成成功，耗时 {elapsed:.2f}s，音频时长 {result.duration:.2f}s",
                detail={
                    "provider": tts.name,
                    "voice": config.get("voice"),
                    "characters": result.characters,
                    "file": str(out),
                },
            )
        except Exception as exc:  # noqa: BLE001
            return TestResult(ok=False, message=f"语音合成测试失败：{exc}")

    if section == "download":
        if not ffmpeg_utils.ffmpeg_available():
            return TestResult(ok=False, message="未检测到 ffmpeg，请先安装（brew install ffmpeg）")
        try:
            downloader = build_downloader(DownloadConfig(**config))
            probe = await downloader.probe("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
            elapsed = time.perf_counter() - started
            return TestResult(
                ok=True,
                message=f"yt-dlp 可用，测试解析耗时 {elapsed:.2f}s",
                detail={
                    "title": probe.title,
                    "duration": probe.entries[0].duration if probe.entries else 0,
                    "source_type": probe.source_type,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return TestResult(ok=False, message=f"下载链路测试失败（可能需要代理）：{exc}")

    if section == "publish":
        try:
            publisher = build_publisher(PublishConfig(**config), settings.auth_dir / "douyin_default.json")
            check = getattr(publisher, "check_login", None)
            if check is None:
                return TestResult(ok=True, message=f"{publisher.name} 发布器就绪（无需登录）")
            ok, message = await check()
            elapsed = time.perf_counter() - started
            return TestResult(
                ok=ok,
                message=f"{message}（耗时 {elapsed:.1f}s）",
                detail={"provider": publisher.name, "account_file": str(settings.auth_dir / "douyin_default.json")},
            )
        except Exception as exc:  # noqa: BLE001
            return TestResult(ok=False, message=f"发布账号检查失败：{exc}")

    raise HTTPException(status_code=400, detail=f"不支持自检的分组：{section}")


@router.get("/paths")
async def data_paths() -> dict:
    """展示数据目录结构，方便用户找到产物。"""
    return {
        "root": str(settings.data_dir),
        "downloads": str(settings.downloads_dir),
        "outputs": str(settings.outputs_dir),
        "subtitles": str(settings.subtitles_dir),
        "audio": str(settings.audio_dir),
        "covers": str(settings.covers_dir),
        "auth": str(settings.auth_dir),
        "exists": {name: Path(path).exists() for name, path in {
            "downloads": settings.downloads_dir,
            "outputs": settings.outputs_dir,
        }.items()},
    }
