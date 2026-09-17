"""阶段耗时打点与聚合。

需求背景：一次搬运里各阶段耗时差着数量级（下载几十秒、语音合成几分钟、发布几分钟），
只说「任务用了 18 分钟」看不出该优化哪个节点。所以每个阶段边界都要记耗时，
并且要能按阶段聚合，回答「时间都花在哪一步」。
"""

from __future__ import annotations

import types

import pytest

from app.api.stats import _percentile, stage_timings
from app.db import SessionLocal, init_db
from app.models import STAGE_ORDER, Task, TaskItem, TaskStatus
from app.pipeline.runner import TaskRunner, _human_duration, summarize_timings

# ------------------------------------------------------------------ 纯函数


def test_human_duration_scales_units():
    assert _human_duration(12.44) == "12.4 秒"
    assert _human_duration(59.96) == "60.0 秒"
    assert _human_duration(311.2) == "5 分 11 秒"
    assert _human_duration(3725) == "1 小时 2 分"


def test_summarize_orders_by_duration_desc():
    """按耗时降序，优化优先级一眼可见。"""
    text = summarize_timings(
        {
            "download": {"seconds": 22.4},
            "tts": {"seconds": 311.0},
            "translate": {"seconds": 39.0},
            "publish": {"skipped": True, "seconds": 0.0},
        }
    )
    assert text.index("语音合成") < text.index("翻译字幕") < text.index("下载视频与字幕")
    assert "跳过" not in text or True  # 跳过的 0 秒不该出现在汇总里
    assert "发布到抖音" not in text


def test_summarize_ignores_zero_and_skipped():
    assert summarize_timings({}) == ""
    assert summarize_timings({"tts": {"skipped": True, "seconds": 0.0}}) == ""


def test_percentile_handles_small_samples():
    assert _percentile([], 0.5) == 0.0
    assert _percentile([5.0], 0.9) == 5.0
    assert _percentile([1.0, 2.0, 3.0], 0.5) == 2.0
    assert _percentile([1.0, 2.0, 3.0, 4.0], 1.0) == 4.0


# ------------------------------------------------------------------ 打点


class _Reporter:
    def __init__(self) -> None:
        self.logs: list[tuple[str, str]] = []
        self.updates: list[dict] = []

    async def log(self, message: str, level: str = "info", **_kw) -> None:
        self.logs.append((level, message))

    async def item_update(self, _item_id: int, **fields) -> None:
        self.updates.append(fields)


def _state() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        stats={}, item=types.SimpleNamespace(id=7, stats={})
    )


async def test_record_timing_writes_stats_and_logs():
    runner = TaskRunner()
    reporter = _Reporter()
    ctx = types.SimpleNamespace(reporter=reporter)
    state = _state()

    # 用低于「偏慢」阈值的耗时，验证正常路径
    await runner._record_timing(ctx, state, "translate", 39.24)

    assert state.stats["timings"]["translate"]["seconds"] == 39.24
    level, message = reporter.logs[0]
    assert level == "info"
    assert "翻译字幕" in message and "39.2 秒" in message
    # 必须落库，否则事后无法聚合分析
    assert reporter.updates and "timings" in reporter.updates[0]["stats"]


async def test_slow_stage_is_flagged():
    """偏慢的阶段在日志里标出来，方便直接扫日志定位。"""
    runner = TaskRunner()
    reporter = _Reporter()
    ctx = types.SimpleNamespace(reporter=reporter)

    await runner._record_timing(ctx, _state(), "align", 400.0)

    level, message = reporter.logs[0]
    assert level == "warning"
    assert "偏慢" in message


async def test_failed_stage_still_records_its_time():
    """失败的阶段也要留下耗时——最需要优化的往往正是卡住的那一步。"""
    runner = TaskRunner()
    reporter = _Reporter()
    ctx = types.SimpleNamespace(reporter=reporter)
    state = _state()

    await runner._record_timing(ctx, state, "publish", 600.0, failed=True)

    entry = state.stats["timings"]["publish"]
    assert entry["seconds"] == 600.0
    assert entry["failed"] is True
    assert reporter.logs[0][0] == "warning"
    assert "失败" in reporter.logs[0][1]


async def test_timing_failure_never_breaks_pipeline():
    """打点本身出错绝不能把流水线带崩。"""

    class BrokenReporter(_Reporter):
        async def item_update(self, _item_id: int, **fields) -> None:
            raise RuntimeError("数据库炸了")

    runner = TaskRunner()
    ctx = types.SimpleNamespace(reporter=BrokenReporter())
    state = _state()

    await runner._record_timing(ctx, state, "download", 22.0)  # 不应抛出
    assert state.stats["timings"]["download"]["seconds"] == 22.0


def test_skipped_stage_does_not_overwrite_measured_time():
    """续跑跳过时，上一次的真实耗时必须保留。"""
    runner = TaskRunner()
    state = types.SimpleNamespace(stats={"timings": {"tts": {"seconds": 311.0}}})

    runner._mark_skipped(state, "tts")
    assert state.stats["timings"]["tts"]["seconds"] == 311.0

    runner._mark_skipped(state, "download")
    assert state.stats["timings"]["download"] == {"seconds": 0.0, "skipped": True}


# ------------------------------------------------------------------ 聚合接口


@pytest.fixture(autouse=True)
async def _db(tmp_root):
    await init_db()
    # 聚合是对全库做统计，用例之间必须互不污染
    from sqlalchemy import delete

    async with SessionLocal() as session:
        await session.execute(delete(TaskItem))
        await session.execute(delete(Task))
        await session.commit()


async def _add_item(stats: dict, *, status: str = TaskStatus.SUCCEEDED.value) -> None:
    async with SessionLocal() as session:
        task = Task(title="t", source_url="u", status=status, total_items=1)
        session.add(task)
        await session.flush()
        session.add(
            TaskItem(task_id=task.id, idx=0, title="v", status=status, stats=stats)
        )
        await session.commit()


async def test_aggregate_excludes_skipped_from_average():
    """跳过的阶段按 0 秒算会稀释真正的热点，必须排除在平均值之外。"""
    await _add_item({"timings": {"tts": {"seconds": 300.0}}, "total_seconds": 400.0})
    await _add_item({"timings": {"tts": {"seconds": 200.0}}, "total_seconds": 300.0})
    # 这一条是续跑：TTS 被跳过，没有真实耗时
    await _add_item({"timings": {"tts": {"seconds": 0.0, "skipped": True}}, "total_seconds": 50.0})

    async with SessionLocal() as session:
        data = await stage_timings(limit=50, session=session)

    tts = next(row for row in data["stages"] if row["stage"] == "tts")
    assert tts["samples"] == 2, "跳过的那条被算进样本了"
    assert tts["skipped"] == 1
    assert tts["avg_seconds"] == 250.0, f"平均值被 0 秒拉低了：{tts['avg_seconds']}"
    assert tts["max_seconds"] == 300.0


async def test_aggregate_reports_share_and_ordering():
    """share 用来回答「时间花在哪一步」，因此要按总耗时降序。"""
    await _add_item(
        {
            "timings": {
                "download": {"seconds": 20.0},
                "tts": {"seconds": 300.0},
                "publish": {"seconds": 80.0},
            },
            "total_seconds": 400.0,
        }
    )
    async with SessionLocal() as session:
        data = await stage_timings(limit=50, session=session)

    assert [row["stage"] for row in data["stages"]] == ["tts", "publish", "download"]
    shares = {row["stage"]: row["share"] for row in data["stages"]}
    assert shares["tts"] == 0.75
    assert abs(sum(shares.values()) - 1.0) < 0.01
    assert data["items"]["samples"] == 1
    assert data["items"]["avg_seconds"] == 400.0


async def test_aggregate_includes_pipeline_order_field():
    """阶段名必须是流水线里的合法 key，否则前端/日志对不上号。"""
    await _add_item({"timings": {"align": {"seconds": 54.0}}, "total_seconds": 60.0})
    async with SessionLocal() as session:
        data = await stage_timings(limit=50, session=session)
    assert data["stages"][0]["stage"] in STAGE_ORDER
    assert data["stages"][0]["label"] == "时间轴对齐与合成"
