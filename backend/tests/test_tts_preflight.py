"""建任务前的语音合成预检。

事故：ChatTTS 服务在任务跑到一半时已经不在了——它原来是前台进程，终端一关 /
Ctrl+C / IDE 停止运行就会被带走，而流水线照样下载、语音识别、翻译，
一路跑到「语音合成」阶段才失败。实测任务 9116 白做了 2 分 38 秒，然后整个任务失败。

本地服务在不在是 1 秒钟就能问清楚的事，没有理由等跑完前三步才发现。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.tasks import _require_tts_ready


def _config(provider: str = "chattts") -> dict:
    return {"tts": {"provider": provider}, "tts_chattts": {}, "tts_aliyun": {}}


@pytest.mark.asyncio
async def test_unreachable_chattts_blocks_task_creation(monkeypatch):
    """服务连不上时必须直接拒绝建任务，而不是让它跑到一半再失败。"""

    class DeadTTS:
        name = "chattts"
        endpoint = "http://127.0.0.1:9966/tts"

        async def probe(self):
            return False, "ConnectError: All connection attempts failed"

    monkeypatch.setattr("app.api.tasks.build_tts", lambda _cfg: DeadTTS())

    with pytest.raises(HTTPException) as err:
        await _require_tts_ready(_config())

    assert err.value.status_code == 400
    detail = err.value.detail
    # 报错必须能照做：给出启动命令，而不是只说「连不上」
    assert "chattts.sh --daemon" in detail
    assert "chattts.sh --status" in detail
    assert "http://127.0.0.1:9966/tts" in detail
    # 说明为什么在开工前拦下来
    assert "语音识别" in detail or "下载" in detail


@pytest.mark.asyncio
async def test_reachable_chattts_allows_task_creation(monkeypatch):
    class LiveTTS:
        name = "chattts"
        endpoint = "http://127.0.0.1:9966/tts"

        async def probe(self):
            return True, "HTTP 200"

    monkeypatch.setattr("app.api.tasks.build_tts", lambda _cfg: LiveTTS())
    await _require_tts_ready(_config())  # 不抛异常即通过


@pytest.mark.asyncio
async def test_probe_timeout_is_treated_as_unreachable(monkeypatch):
    """探活挂住不能把建任务接口也拖住。"""

    class HangingTTS:
        name = "chattts"
        endpoint = "http://127.0.0.1:9966/tts"

        async def probe(self):
            import asyncio

            await asyncio.sleep(30)
            return True, "never"

    monkeypatch.setattr("app.api.tasks.build_tts", lambda _cfg: HangingTTS())
    monkeypatch.setattr("app.api.tasks.asyncio.wait_for", _instant_timeout)

    with pytest.raises(HTTPException) as err:
        await _require_tts_ready(_config())
    assert err.value.status_code == 400
    assert "连不上" in err.value.detail


async def _instant_timeout(coro, timeout):  # noqa: ARG001
    coro.close()
    raise TimeoutError("probe timed out")


@pytest.mark.asyncio
async def test_non_chattts_provider_skips_probe(monkeypatch):
    """阿里云不该在这里探活：它需要真实合成调用（要计费），凭证问题由提供者自己报。"""

    def boom(_cfg):
        raise AssertionError("非 chattts 通道不应构建 TTS 或探活")

    monkeypatch.setattr("app.api.tasks.build_tts", boom)
    await _require_tts_ready(_config("aliyun"))
    await _require_tts_ready(_config("mock"))


@pytest.mark.asyncio
async def test_broken_tts_config_does_not_block_creation(monkeypatch):
    """TTS 配置本身不完整时不在这里拦——交给负责该阶段的代码报出准确原因。"""

    def boom(_cfg):
        raise ValueError("配置缺字段")

    monkeypatch.setattr("app.api.tasks.build_tts", boom)
    await _require_tts_ready(_config())


def test_chattts_script_supports_daemon_modes():
    """脚本必须提供后台/守护相关的模式，否则「服务被终端带走」只能靠用户自觉。"""
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "scripts" / "chattts.sh"
    text = script.read_text(encoding="utf-8")
    for mode in ("--daemon", "--status", "--stop", "--logs", "--install-agent"):
        assert mode in text, f"chattts.sh 缺少 {mode}"
    # 后台启动必须用 nohup 让进程忽略 SIGHUP，否则关终端照样被杀
    assert "nohup" in text
    # launchd 那套是防「进程意外退出」的根治手段
    assert "KeepAlive" in text
