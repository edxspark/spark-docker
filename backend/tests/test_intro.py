"""统一开头语：纯函数逻辑 + 端到端行为。

开头语会同时影响字幕与配音，这里覆盖三件容易出错的事：
1. 正片字幕是否被整体后移（否则开头语会盖住第一句）；
2. 续跑是否重复插入（字幕文件里已经有了就必须跳过）；
3. 「只播不显」时配音仍在、字幕被过滤。
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.db import SessionLocal, init_db
from app.models import Task, TaskItem, TaskLog, TaskStatus
from app.pipeline.runner import task_runner
from app.pipeline.stages.localize import tts_config_key
from app.services import intro as intro_service
from app.services.settings_store import settings_store
from app.services.subtitles import Cue

pytestmark = pytest.mark.asyncio

INTRO_TEXT = "欢迎来到Edx Spark，我们将为您提供高质量的AI创业、工作、创新、学习等资讯！"


def _settings(**overrides) -> dict:
    base = {"enabled": True, "text": INTRO_TEXT, "gap_seconds": 0.5, "show_in_subtitle": True}
    base.update(overrides)
    return base


# ---------------------------------------------------------------- 纯逻辑


async def test_apply_intro_shifts_original_cues():
    cues = [Cue(start=0.0, end=2.0, text="第一句"), Cue(start=2.0, end=4.0, text="第二句")]
    out = intro_service.apply_intro(cues, _settings(), duration=3.0)

    assert len(out) == 3
    assert intro_service.is_intro_cue(out[0])
    assert (out[0].start, out[0].end) == (0.0, 3.5)  # 3.0 配音 + 0.5 停顿
    assert [(c.start, c.end) for c in out[1:]] == [(3.5, 5.5), (5.5, 7.5)]
    # 原始列表不被就地修改
    assert [(c.start, c.end) for c in cues] == [(0.0, 2.0), (2.0, 4.0)]


async def test_subtitle_cues_can_hide_intro():
    cues = intro_service.apply_intro([Cue(start=0, end=1, text="正片")], _settings(), duration=2.0)

    visible = intro_service.subtitle_cues(cues, _settings())
    hidden = intro_service.subtitle_cues(cues, _settings(show_in_subtitle=False))

    assert intro_service.has_intro(visible)
    assert not intro_service.has_intro(hidden)
    assert [c.text for c in hidden] == ["正片"]


async def test_blank_text_is_treated_as_unconfigured():
    assert intro_service.intro_settings({"text": "   "})["text"] == ""
    assert intro_service.intro_settings({"enabled": False})["enabled"] is False


async def test_config_key_changes_with_text_and_visibility():
    base = intro_service.intro_config_key(_settings())
    assert base != intro_service.intro_config_key(_settings(text="换一句话"))
    assert base != intro_service.intro_config_key(_settings(show_in_subtitle=False))
    assert base != intro_service.intro_config_key(_settings(enabled=False))


async def test_tts_key_includes_intro_text():
    """开头语文案变化必须让配音指纹失效，否则续跑会继续用旧开头语。"""
    from types import SimpleNamespace

    def ctx_with(text: str):
        intro = {"enabled": True, "text": text}
        return SimpleNamespace(
            config=SimpleNamespace(
                # 只用到 intro 分组；tts 分组返回空字典即可
                merged=lambda section, _intro=intro: dict(_intro) if section == "intro" else {}
            )
        )

    assert tts_config_key(ctx_with("甲")) != tts_config_key(ctx_with("乙"))
    assert tts_config_key(ctx_with("甲")) == tts_config_key(ctx_with("甲"))


# ---------------------------------------------------------------- 端到端


async def _prepare(monkeypatch, sample_video, sample_srt, intro_config: dict):
    from tests.test_pipeline_e2e import StubDownloader

    import app.pipeline.runner as runner_module

    await init_db()
    async with SessionLocal() as session:
        await settings_store.update(
            session,
            {
                "translator": {"provider": "mock"},
                "tts": {"provider": "mock", "voice": "xiaoxian", "sample_rate": 24000, "concurrency": 2},
                "publish": {"provider": "mock", "auto_publish": False},
                "asr": {"provider": "mock", "enabled": True, "max_chunk_seconds": 10},
                "video": {"target_aspect": "original", "burn_subtitles": True, "preset": "ultrafast", "crf": 28},
                "general": {"max_concurrent_tasks": 1},
                "intro": intro_config,
            },
        )

    stub = StubDownloader(sample_video, sample_srt)
    real_build_all = runner_module.build_all

    def fake_build_all(config, account_file):
        providers = real_build_all(config, account_file)
        providers["downloader"] = stub
        return providers

    monkeypatch.setattr(runner_module, "build_all", fake_build_all)
    return stub


async def _create_task(url: str) -> int:
    async with SessionLocal() as session:
        task = Task(
            title="开头语测试",
            source_url=url,
            source_type="video",
            status=TaskStatus.PENDING.value,
            total_items=1,
            options={},
        )
        session.add(task)
        await session.flush()
        session.add(
            TaskItem(
                task_id=task.id,
                idx=0,
                video_id=url.rsplit("=", 1)[-1],
                url=url,
                title="Intro Test",
                author="ch",
                duration=12.0,
                status=TaskStatus.PENDING.value,
            )
        )
        await session.commit()
        return task.id


async def _run(task_id: int) -> None:
    handle = await task_runner.submit(task_id)
    assert handle.task is not None
    await handle.task


async def _load(task_id: int):
    from sqlalchemy import select

    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        items = (await session.execute(select(TaskItem).where(TaskItem.task_id == task_id))).scalars().all()
        logs = (await session.execute(select(TaskLog).where(TaskLog.task_id == task_id))).scalars().all()
    return task, list(items), list(logs)


async def _ffmpeg_ok() -> bool:
    from app.utils import ffmpeg as ffmpeg_utils

    return ffmpeg_utils.ffmpeg_available()


async def test_intro_is_spoken_and_subtitled(monkeypatch, sample_video, sample_srt):
    if not await _ffmpeg_ok():
        pytest.skip("未安装 ffmpeg")

    await _prepare(monkeypatch, sample_video, sample_srt, _settings())
    task_id = await _create_task("https://www.youtube.com/watch?v=intro0001")
    await _run(task_id)
    task, items, logs = await _load(task_id)
    item = items[0]

    assert task.status == TaskStatus.SUCCEEDED.value, task.message
    assert item.status == TaskStatus.SUCCEEDED.value

    zh_text = (settings.data_dir / item.subtitle_zh_path).read_text(encoding="utf-8")
    assert INTRO_TEXT in zh_text, "开头语没有写进中文字幕"
    first_block = zh_text.strip().split("\n\n")[0]
    assert "00:00:00" in first_block

    stats = item.stats
    assert stats["intro"]["text"] == INTRO_TEXT
    assert stats["intro"]["duration"] > 0
    # 开头语是字幕里的第一条，因此它已经算在 sentences 里，配音段数与句子数一一对应
    assert stats["translate"]["sentences"] == 6  # 5 句正片 + 1 句开头语
    assert stats["tts"]["segments"] == stats["translate"]["sentences"]
    assert any("已插入统一开头语" in log.message for log in logs)

    # 开头语在画面上只有中文一行：正片第一句英文不能被贴到它下面
    ass = (settings.data_dir / item.subtitle_zh_path).with_suffix(".ass")
    dialogues = [line for line in ass.read_text(encoding="utf-8").splitlines() if line.startswith("Dialogue")]
    intro_line = dialogues[0]
    assert INTRO_TEXT in intro_line
    assert "\\N" not in intro_line, f"开头语下面多了一行英文：{intro_line}"

    # 成片应比原片长：开头语 + 停顿把整条时间轴推后了
    from app.utils import ffmpeg as ffmpeg_utils

    media = await ffmpeg_utils.probe(settings.data_dir / item.output_path)
    assert media.duration == pytest.approx(12.0 + stats["intro"]["offset"], abs=0.8)


async def test_intro_is_not_inserted_twice_on_resume(monkeypatch, sample_video, sample_srt):
    if not await _ffmpeg_ok():
        pytest.skip("未安装 ffmpeg")

    await _prepare(monkeypatch, sample_video, sample_srt, _settings())
    task_id = await _create_task("https://www.youtube.com/watch?v=intro0002")
    await _run(task_id)
    task, items, _ = await _load(task_id)
    assert task.status == TaskStatus.SUCCEEDED.value
    first_sentences = items[0].stats["translate"]["sentences"]
    first_segments = items[0].stats["tts"]["segments"]

    # 再次执行：所有阶段应被跳过，开头语不会变成两条
    await _run(task_id)
    task, items, _ = await _load(task_id)
    assert task.status == TaskStatus.SUCCEEDED.value
    assert items[0].stats["translate"]["sentences"] == first_sentences
    assert items[0].stats["tts"]["segments"] == first_segments

    zh_text = (settings.data_dir / items[0].subtitle_zh_path).read_text(encoding="utf-8")
    assert zh_text.count(INTRO_TEXT) == 1, "续跑后开头语出现了多次"


async def test_intro_without_subtitle_still_dubbed(monkeypatch, sample_video, sample_srt):
    if not await _ffmpeg_ok():
        pytest.skip("未安装 ffmpeg")

    await _prepare(monkeypatch, sample_video, sample_srt, _settings(show_in_subtitle=False))
    task_id = await _create_task("https://www.youtube.com/watch?v=intro0003")
    await _run(task_id)
    task, items, _ = await _load(task_id)
    item = items[0]

    assert task.status == TaskStatus.SUCCEEDED.value, task.message
    # 配音照旧（分段里含开头语），但烧录字幕里没有它
    assert item.stats["tts"]["segments"] == item.stats["translate"]["sentences"] == 6
    ass = (settings.data_dir / item.subtitle_zh_path).with_suffix(".ass")
    assert ass.exists()
    assert INTRO_TEXT not in ass.read_text(encoding="utf-8")


async def test_disabled_intro_keeps_pipeline_unchanged(monkeypatch, sample_video, sample_srt):
    if not await _ffmpeg_ok():
        pytest.skip("未安装 ffmpeg")

    await _prepare(monkeypatch, sample_video, sample_srt, _settings(enabled=False))
    task_id = await _create_task("https://www.youtube.com/watch?v=intro0004")
    await _run(task_id)
    task, items, _ = await _load(task_id)
    item = items[0]

    assert task.status == TaskStatus.SUCCEEDED.value, task.message
    assert item.stats["tts"]["segments"] == item.stats["translate"]["sentences"]
    # 关闭时也要留下「已处理」标记（mode=disabled）：否则每次续跑都会重复撤一次
    assert (item.stats.get("intro") or {}).get("mode") == "disabled"
    assert not (item.stats.get("intro") or {}).get("offset")
    zh_text = (settings.data_dir / item.subtitle_zh_path).read_text(encoding="utf-8")
    assert INTRO_TEXT not in zh_text


async def test_changing_intro_text_rebuilds_audio(monkeypatch, sample_video, sample_srt):
    """改开头语文案后重跑：应重新合成开头语，且字幕里换成新文案。"""
    if not await _ffmpeg_ok():
        pytest.skip("未安装 ffmpeg")

    await _prepare(monkeypatch, sample_video, sample_srt, _settings())
    task_id = await _create_task("https://www.youtube.com/watch?v=intro0005")
    await _run(task_id)
    task, items, _ = await _load(task_id)
    assert task.status == TaskStatus.SUCCEEDED.value
    assert items[0].stats["intro"]["text"] == INTRO_TEXT

    async with SessionLocal() as session:
        await settings_store.update(session, {"intro": {"text": "这里是新的开头语。"}})

    await _run(task_id)
    task, items, _ = await _load(task_id)
    assert task.status == TaskStatus.SUCCEEDED.value
    assert items[0].stats["intro"]["text"] == "这里是新的开头语。"
    zh_text = (settings.data_dir / items[0].subtitle_zh_path).read_text(encoding="utf-8")
    assert "这里是新的开头语。" in zh_text
