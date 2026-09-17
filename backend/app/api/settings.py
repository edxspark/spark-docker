"""系统配置接口：读取（脱敏）、保存、重置、连通性自检。"""

from __future__ import annotations

import contextlib
import logging
import time
from pathlib import Path

import httpx
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


@router.post("/intro/card")
async def preview_intro_card(
    payload: dict,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """预览「统一开头语」的科技感标题卡。

    传当前表单里的配置即可（未传的字段用已保存配置补），用于保存前先看效果。
    以 data URI 返回，避免额外暴露一个静态目录。
    """
    import base64

    from app.services import intro_card

    saved = await settings_store.get_section(session, "intro")
    body = payload.get("intro") if isinstance(payload.get("intro"), dict) else payload
    config = {**saved, **(body or {})}

    # 预览用 9:16 竖屏尺寸：抖音投放比例，也是这套卡片最常见的显示场景
    try:
        result = await intro_card.build_card_async(
            width=1080, height=1920, config=config, force=bool(payload.get("force"))
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"开头画面生成失败：{exc}") from exc

    try:
        data = result.path.read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"开头画面读取失败：{exc}") from exc

    return {
        "ok": True,
        "renderer": result.renderer,
        "width": result.width,
        "height": result.height,
        "image": "data:image/png;base64," + base64.b64encode(data).decode("ascii"),
        "message": f"已生成 {result.width}x{result.height} 开头画面（渲染器：{result.renderer}）",
    }


@router.get("/tts/chattts/speakers")
async def chattts_speakers(
    session: AsyncSession = Depends(get_session),
) -> dict:
    """探测本地 ChatTTS 服务，尽量取回可用说话人列表。

    不同封装暴露的接口不统一（ChatTTS-ui 是 /api/speakers 这类），
    因此这里做「尽力探测」：拿不到列表不算失败，返回 reachable=False + 提示，
    前端据此展示输入框而不是下拉框。
    """
    config = await settings_store.merged_tts(session)
    base = (config.get("chattts_base_url") or "http://127.0.0.1:9966").rstrip("/")
    timeout = min(8.0, float(config.get("timeout") or 8))

    candidates = ["/api/speakers", "/speakers", "/api/voice_list", "/api/voices"]
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        # 先确认服务在不在
        try:
            root = await client.get(base)
        except Exception as exc:  # noqa: BLE001
            return {
                "reachable": False,
                "base_url": base,
                "speakers": [],
                "message": f"连不上 {base}（{type(exc).__name__}: {exc}）。请先启动 ChatTTS 服务",
            }

        for path in candidates:
            try:
                response = await client.get(f"{base}{path}")
            except Exception:  # noqa: BLE001
                continue
            if response.status_code >= 400:
                continue
            try:
                data = response.json()
            except Exception:  # noqa: BLE001
                continue
            items = data.get("speakers") or data.get("data") or data.get("voices") or data
            if isinstance(items, list) and items:
                return {
                    "reachable": True,
                    "base_url": base,
                    "speakers": items[:200],
                    "message": f"服务可达，{len(items)} 个说话人（{path}）",
                }

        return {
            "reachable": True,
            "base_url": base,
            "speakers": [],
            "message": f"服务可达（HTTP {root.status_code}），但没找到说话人列表接口；"
            "可直接填说话人编号（-1 表示按固定种子随机，音色稳定）",
        }


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
            # 配置分三组存，测试按钮要测的是「当前通道」的完整参数
            tts = build_tts(TTSConfig(**await settings_store.merged_tts(session)))
            # ChatTTS 返回 wav，文件名跟上实际格式便于试听
            suffix = ".wav" if getattr(tts, "name", "") == "chattts" else ".mp3"
            out = settings.data_dir / "logs" / f"tts_test{suffix}"
            result = await tts.synthesize("这是一段语音合成测试。", out)
            elapsed = time.perf_counter() - started
            detail: dict = {
                "provider": tts.name,
                "voice": config.get("voice"),
                "characters": result.characters,
                "file": str(out),
            }
            describe = getattr(tts, "describe", None)
            if describe is not None:
                with contextlib.suppress(Exception):
                    detail["endpoint"] = await describe()
            return TestResult(
                ok=True,
                message=f"语音合成成功，耗时 {elapsed:.2f}s，音频时长 {result.duration:.2f}s",
                detail=detail,
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
