"""端到端流水线测试。

用真实 ffmpeg 做媒体处理，但下载器、翻译、语音合成与发布均替换为
不依赖外部网络的实现（stub / mock），因此在离线环境也能完整验证：
字幕 → 翻译 → 配音 → 时间轴对齐 → 混音 → 烧录字幕 → 渲染成片 → 发布 的整条链路。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.core.config import settings
from app.db import SessionLocal, init_db
from app.models import Task, TaskItem, TaskLog, TaskStatus
from app.pipeline.runner import task_runner
from app.providers.base import DownloadResult, ProbeResult, PublishResult, VideoInfo
from app.services.settings_store import settings_store

pytestmark = pytest.mark.asyncio


class StubDownloader:
    """把本地夹具视频当作"已下载"的结果返回。"""

    name = "stub"

    def __init__(self, video: Path, subtitle: Path | None = None) -> None:
        self.video = video
        self.subtitle = subtitle
        self.download_calls = 0

    async def probe(self, url: str) -> ProbeResult:
        return ProbeResult(
            source_type="video",
            title="Offline Test Video",
            author="Test Channel",
            source_id="test0001",
            entries=[
                VideoInfo(
                    video_id="test0001",
                    url=url,
                    title="Offline Test Video",
                    description="A video used by the automated pipeline test.",
                    author="Test Channel",
                    duration=12.0,
                )
            ],
        )

    async def download(self, item: VideoInfo, output_dir: Path, *, progress=None, cancel_check=None):
        self.download_calls += 1
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / f"{item.video_id}.mp4"
        shutil.copy2(self.video, target)
        if progress:
            progress(50.0, "下载中 50%")
            progress(100.0, "下载完成，正在合并音视频…")
        return DownloadResult(
            video_path=target,
            subtitle_path=self.subtitle,
            thumbnail_path=None,
            info=item,
            subtitle_kind="manual" if self.subtitle else "none",
        )

    async def fetch_thumbnail(self, item: VideoInfo, out_path: Path):
        return None


@pytest.fixture
async def prepared(tmp_root, sample_video, sample_srt, monkeypatch):
    """准备数据库、Mock 配置，并把提供者替换为离线实现。"""
    await init_db()

    async with SessionLocal() as session:
        await settings_store.update(
            session,
            {
                "translator": {"provider": "mock", "api_key": ""},
                "tts": {"provider": "mock", "voice": "xiaoxian", "sample_rate": 24000, "concurrency": 2},
                "publish": {"provider": "mock", "auto_publish": True, "default_tags": ["测试", "搬运"]},
                "asr": {"provider": "mock", "enabled": True, "max_chunk_seconds": 10},
                "video": {
                    "target_aspect": "original",
                    "burn_subtitles": True,
                    "keep_bgm": True,
                    "bgm_volume": 0.1,
                    "max_speedup": 1.5,
                    "preset": "ultrafast",
                    "crf": 28,
                },
                "general": {"max_concurrent_tasks": 1},
            },
        )

    stub = StubDownloader(sample_video, sample_srt)

    import app.pipeline.runner as runner_module

    real_build_all = runner_module.build_all

    def fake_build_all(config, account_file):
        providers = real_build_all(config, account_file)
        providers["downloader"] = stub
        return providers

    monkeypatch.setattr(runner_module, "build_all", fake_build_all)
    return stub


async def _create_task(url: str = "https://www.youtube.com/watch?v=test0001") -> int:
    async with SessionLocal() as session:
        task = Task(
            title="离线端到端测试",
            source_url=url,
            source_type="video",
            source_id="test0001",
            author="Test Channel",
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
                video_id="test0001",
                url=url,
                title="Offline Test Video",
                author="Test Channel",
                duration=12.0,
                status=TaskStatus.PENDING.value,
            )
        )
        await session.commit()
        return task.id


async def _run_to_completion(task_id: int) -> None:
    handle = await task_runner.submit(task_id)
    assert handle.task is not None
    await handle.task


async def _load(task_id: int) -> tuple[Task, list[TaskItem], list[TaskLog]]:
    from sqlalchemy import select

    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        items = (
            await session.execute(select(TaskItem).where(TaskItem.task_id == task_id))
        ).scalars().all()
        logs = (
            await session.execute(select(TaskLog).where(TaskLog.task_id == task_id))
        ).scalars().all()
    assert task is not None
    return task, list(items), list(logs)


async def test_pipeline_end_to_end(prepared, sample_video):
    """完整链路：下载 → 断句 → 翻译 → 配音 → 对齐混音 → 渲染 → 发布。"""
    from app.utils import ffmpeg as ffmpeg_utils

    if not ffmpeg_utils.ffmpeg_available():
        pytest.skip("未安装 ffmpeg")

    task_id = await _create_task()
    await _run_to_completion(task_id)
    task, items, logs = await _load(task_id)

    assert task.status == TaskStatus.SUCCEEDED.value, f"任务失败：{task.message}\n{item_errors(items)}"
    assert task.done_items == 1 and task.failed_items == 0
    assert task.progress == 100.0
    assert task.finished_at is not None

    item = items[0]
    assert item.status == TaskStatus.SUCCEEDED.value

    # 各阶段产物是否落盘
    for attribute in ("video_path", "subtitle_source_path", "subtitle_zh_path", "dubbed_audio_path", "output_path"):
        relative = getattr(item, attribute)
        assert relative, f"缺少产物路径：{attribute}"
        assert (settings.data_dir / relative).exists(), f"产物文件不存在：{relative}"

    # 中文字幕内容应来自 Mock 翻译
    zh_text = (settings.data_dir / item.subtitle_zh_path).read_text(encoding="utf-8")
    assert "示例译文" in zh_text or "。" in zh_text

    # 成片应包含音轨（配音混音后的 AAC）
    output = settings.data_dir / item.output_path
    media = await ffmpeg_utils.probe(output)
    assert media.has_video and media.has_audio
    assert media.duration == pytest.approx(12.0, abs=0.6)

    # 发布结果（Mock 发布器）
    assert item.publish_status == "published"
    assert item.publish_url.startswith("https://www.douyin.com/video/mock-")
    assert item.published_at is not None

    # 统计信息应被记录，供仪表盘聚合
    assert item.stats.get("sentences", 0) >= 3
    assert item.stats["tts"]["segments"] == item.stats["translate"]["sentences"]
    assert item.stats["output"]["dubbed"] is True
    assert item.stats["subtitle_source"]["kind"] == "manual"

    # 日志应覆盖各阶段
    stages = {log.stage for log in logs}
    for stage in ("download", "subtitle", "translate", "tts", "align", "publish"):
        assert stage in stages, f"缺少阶段日志：{stage}"


async def test_pipeline_is_resumable(prepared):
    """重复执行同一任务时应跳过已完成的阶段（不重复下载）。"""
    if not (await _ffmpeg_ok()):
        pytest.skip("未安装 ffmpeg")

    task_id = await _create_task("https://www.youtube.com/watch?v=resume0001")
    await _run_to_completion(task_id)
    task, items, _ = await _load(task_id)
    assert task.status == TaskStatus.SUCCEEDED.value

    calls_after_first_run = prepared.download_calls

    # 再次执行：所有阶段都应被判定为已完成
    await _run_to_completion(task_id)
    assert prepared.download_calls == calls_after_first_run, "续跑时不应重复下载"


async def test_publish_can_be_skipped(prepared):
    """关闭自动发布时，成片产出但发布被跳过。"""
    if not (await _ffmpeg_ok()):
        pytest.skip("未安装 ffmpeg")


    task_id = await _create_task("https://www.youtube.com/watch?v=skip0001")
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        task.options = {"auto_publish": False}
        await session.commit()

    await _run_to_completion(task_id)
    task, items, _ = await _load(task_id)
    assert task.status == TaskStatus.SUCCEEDED.value
    assert items[0].output_path
    assert items[0].publish_status == "skipped"
    assert items[0].publish_url == ""


async def test_stage_failure_marks_item_failed(prepared, monkeypatch, sample_video):
    """提供者报错时，条目应被标记失败且任务整体状态为失败。"""
    if not (await _ffmpeg_ok()):
        pytest.skip("未安装 ffmpeg")

    import app.pipeline.stages.localize as localize

    async def boom(ctx, state):
        from app.providers.base import ProviderError

        raise ProviderError("模拟合成失败")

    monkeypatch.setattr(localize, "stage_tts", boom)
    import app.pipeline.runner as runner_module

    patched = [(name, boom if name == "tts" else fn) for name, fn in runner_module.STAGES]
    monkeypatch.setattr(runner_module, "STAGES", patched)

    task_id = await _create_task("https://www.youtube.com/watch?v=fail0001")
    await _run_to_completion(task_id)
    task, items, logs = await _load(task_id)

    assert task.status == TaskStatus.FAILED.value
    assert task.failed_items == 1
    assert items[0].status == TaskStatus.FAILED.value
    assert "语音合成" in items[0].message or "模拟合成失败" in items[0].error
    assert any(log.level == "error" for log in logs)


async def test_task_cancel_stops_pipeline(prepared, monkeypatch):
    """取消事件置位后，条目应终止并标记为已取消。"""
    if not (await _ffmpeg_ok()):
        pytest.skip("未安装 ffmpeg")

    import asyncio

    async def slow_then_raise(ctx, state):
        await asyncio.sleep(0.4)
        ctx.reporter.cancel_event.set()
        ctx.reporter.raise_if_canceled()

    import app.pipeline.runner as runner_module

    patched = [(name, slow_then_raise if name == "download" else fn) for name, fn in runner_module.STAGES]
    monkeypatch.setattr(runner_module, "STAGES", patched)

    task_id = await _create_task("https://www.youtube.com/watch?v=cancel0001")
    await _run_to_completion(task_id)
    task, items, _ = await _load(task_id)

    assert task.status == TaskStatus.CANCELED.value
    assert items[0].status == TaskStatus.CANCELED.value


async def test_mock_tts_and_translator_do_not_need_credentials():
    """Mock 提供者在没有密钥的情况下也能工作。"""
    from app.providers import build_translator, build_tts
    from app.services.settings_store import TranslatorConfig, TTSConfig

    translator = build_translator(TranslatorConfig(provider="mock", api_key=""))
    result = await translator.translate(["hello world", "today we learn"])
    assert len(result.texts) == 2
    assert all(text.strip() for text in result.texts)

    tts = build_tts(TTSConfig(provider="mock", sample_rate=16000))
    out = settings.data_dir / "logs" / "mock_tts_test.mp3"
    synthesis = await tts.synthesize("这是一句测试。", out)
    assert out.exists() and synthesis.duration > 0


async def test_mock_publisher_requires_existing_video(tmp_root):
    from app.providers.base import ProviderError, PublishRequest
    from app.providers.publisher.douyin import MockPublisher

    publisher = MockPublisher()
    with pytest.raises(ProviderError):
        await publisher.publish(PublishRequest(video_path=tmp_root / "missing.mp4", title="t"))

    real = tmp_root / "fixtures" / "sample.mp4"
    if not real.exists():
        pytest.skip("夹具视频不存在")
    result: PublishResult = await publisher.publish(
        PublishRequest(video_path=real, title="标题", tags=["a", "b"])
    )
    assert result.success and result.work_url


async def _ffmpeg_ok() -> bool:
    from app.utils import ffmpeg as ffmpeg_utils

    return ffmpeg_utils.ffmpeg_available()


def item_errors(items: list[TaskItem]) -> str:
    return "\n".join(f"  - {item.title}: {item.error}" for item in items if item.error)


async def test_asr_fallback_produces_dubbing_when_no_subtitle(prepared, sample_video):
    """核心场景：视频没有字幕时，用语音识别生成原文，再翻译配音并替换音轨。

    这正是用户反馈的问题：原先无字幕视频会直接跳过翻译与配音，成片只有原声。
    """
    if not (await _ffmpeg_ok()):
        pytest.skip("未安装 ffmpeg")

    # 把下载器换成「没有字幕」的版本
    import app.pipeline.runner as runner_module

    prefixed = runner_module.build_all

    def fake_build_all(config, account_file):
        providers = prefixed(config, account_file)
        providers["downloader"] = StubDownloader(sample_video, subtitle=None)
        return providers

    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    mp.setattr(runner_module, "build_all", fake_build_all)
    try:
        task_id = await _create_task("https://www.youtube.com/watch?v=asr0001")
        await _run_to_completion(task_id)
    finally:
        mp.undo()

    task, items, logs = await _load(task_id)
    item = items[0]
    assert task.status == TaskStatus.SUCCEEDED.value, f"任务失败：{task.message}\n{item_errors(items)}"

    stats = item.stats or {}
    # 原文来自语音识别，而不是下载的字幕
    assert stats.get("subtitle_source", {}).get("kind") == "asr", stats.get("subtitle_source")
    assert stats.get("asr", {}).get("segments", 0) > 0
    assert not stats.get("no_subtitle"), "不应再落到「无字幕保留原声」分支"

    # 有中文字幕、有配音音轨、成片替换了音轨
    assert item.subtitle_source_path and (settings.data_dir / item.subtitle_source_path).exists()
    assert item.subtitle_zh_path and (settings.data_dir / item.subtitle_zh_path).exists()
    assert item.dubbed_audio_path and (settings.data_dir / item.dubbed_audio_path).exists()
    assert item.output_path and (settings.data_dir / item.output_path).exists()
    assert stats["output"]["dubbed"] is True, "成片没有使用配音音轨"
    assert stats["tts"]["segments"] == stats["translate"]["sentences"] > 0

    zh_text = (settings.data_dir / item.subtitle_zh_path).read_text(encoding="utf-8")
    assert zh_text.strip()

    assert any("语音识别" in log.message for log in logs), "缺少语音识别的日志"


async def test_asr_disabled_keeps_original_audio(prepared, sample_video):
    """关闭语音识别时，无字幕视频仍应产出成片（保留原声），不应失败。"""
    if not (await _ffmpeg_ok()):
        pytest.skip("未安装 ffmpeg")

    import pytest as _pytest

    import app.pipeline.runner as runner_module

    prefixed = runner_module.build_all

    def fake_build_all(config, account_file):
        providers = prefixed(config, account_file)
        providers["downloader"] = StubDownloader(sample_video, subtitle=None)
        return providers

    mp = _pytest.MonkeyPatch()
    mp.setattr(runner_module, "build_all", fake_build_all)
    try:
        async with SessionLocal() as session:
            await settings_store.update(session, {"asr": {"enabled": False}})
        task_id = await _create_task("https://www.youtube.com/watch?v=asr0002")
        await _run_to_completion(task_id)
    finally:
        mp.undo()
        async with SessionLocal() as session:
            await settings_store.update(session, {"asr": {"enabled": True}})

    task, items, _ = await _load(task_id)
    item = items[0]
    assert task.status == TaskStatus.SUCCEEDED.value, item_errors(items)
    assert (item.stats or {}).get("no_subtitle") is True
    assert item.output_path, "关闭 ASR 后仍应产出成片"
    assert not item.publish_error


async def test_interrupted_asr_is_retried_not_skipped(prepared, sample_video):
    """回归：语音识别被中断后，重试必须重新识别，而不是因为「试过」就跳过。

    实际发生过：服务重载打断识别，重试却因标记「已尝试」而跳过，
    结果任务显示成功但成片永远没有中文配音。
    """
    if not (await _ffmpeg_ok()):
        pytest.skip("未安装 ffmpeg")

    from sqlalchemy import select

    task_id = await _create_task("https://www.youtube.com/watch?v=asr0003")
    async with SessionLocal() as session:
        item = (
            await session.execute(select(TaskItem).where(TaskItem.task_id == task_id))
        ).scalars().first()
        assert item is not None
        # 模拟「识别进行到一半被中断」的现场
        item.stats = {"no_subtitle": True}
        await session.commit()

    # 换成无字幕下载器，让流程必须走 ASR
    import pytest as _pytest

    import app.pipeline.runner as runner_module

    prefixed = runner_module.build_all

    def fake_build_all(config, account_file):
        providers = prefixed(config, account_file)
        providers["downloader"] = StubDownloader(sample_video, subtitle=None)
        return providers

    mp = _pytest.MonkeyPatch()
    mp.setattr(runner_module, "build_all", fake_build_all)
    try:
        await _run_to_completion(task_id)
    finally:
        mp.undo()

    task, items, _ = await _load(task_id)
    item = items[0]
    assert (item.stats or {}).get("subtitle_source", {}).get("kind") == "asr", (
        "中断后重试跳过了语音识别，成片将没有中文配音"
    )
    assert item.dubbed_audio_path, "没有生成配音音轨"
    assert item.dubbed_audio_path and (settings.data_dir / item.dubbed_audio_path).exists()


async def test_definitive_asr_failure_is_not_retried(prepared, sample_video, monkeypatch):
    """确定性失败（如识别服务不可用）应被标记，避免每次重试都白跑一遍昂贵识别。"""
    if not (await _ffmpeg_ok()):
        pytest.skip("未安装 ffmpeg")

    import app.pipeline.runner as runner_module

    prefixed = runner_module.build_all

    def broken_factory_holder(config, account_file):
        providers = prefixed(config, account_file)
        providers["downloader"] = StubDownloader(sample_video, subtitle=None)

        def broken_factory():
            from app.providers.base import NotConfiguredError

            raise NotConfiguredError("模拟：未配置语音识别凭证")

        providers["asr_factory"] = broken_factory
        return providers

    monkeypatch.setattr(runner_module, "build_all", broken_factory_holder)
    task_id = await _create_task("https://www.youtube.com/watch?v=asr0004")
    await _run_to_completion(task_id)

    task, items, logs = await _load(task_id)
    item = items[0]
    # 任务本身仍应成功（保留原声产出成片），只是没有配音
    assert task.status == TaskStatus.SUCCEEDED.value, item_errors(items)
    stats = item.stats or {}
    assert stats.get("asr_done") is True, "确定性失败应被标记，避免重复识别"
    assert "未配置语音识别凭证" in (stats.get("asr_error") or "")
    assert item.output_path
    assert any("语音识别不可用" in log.message for log in logs)
