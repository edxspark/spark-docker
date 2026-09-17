"""搬运计划：调度时间计算、条目挑选去重、调度器触发。

重点覆盖三件容易出错的事：
1. 本地时间与 UTC 的换算（"每天 08:00" 必须是本地 08:00，不能按时区偏移跑偏）；
2. 「持续更新型」计划不能反复搬同一个视频（去重必须在数量限制之前生效）；
3. 到点触发只能触发一次（认领式推进 next_run_at，而不是每轮都重建任务）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.db import SessionLocal, init_db
from app.models import Plan, Task, TaskItem, TaskStatus
from app.providers.base import ProbeResult, VideoInfo
from app.services import plans as plans_service
from app.services import schedule as schedule_service
from app.services.plan_scheduler import plan_scheduler

pytestmark = pytest.mark.asyncio


def _local(naive_utc: datetime) -> datetime:
    """把 naive UTC 转成本地时间，便于断言。"""
    return naive_utc.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


# ---------------------------------------------------------------- 调度时间


async def test_manual_schedule_has_no_next_run():
    spec = schedule_service.normalize({"type": "manual"})
    assert spec == {"type": "manual"}
    assert schedule_service.next_run_at(spec) is None
    assert schedule_service.describe(spec) == "仅手动运行"


async def test_interval_schedule_uses_minutes():
    spec = schedule_service.normalize({"type": "interval", "minutes": 90})
    now = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)
    assert schedule_service.next_run_at(spec, now_utc=now) == datetime(2026, 1, 1, 4, 30)
    # 太小/太大的间隔会被夹到合理区间
    assert schedule_service.normalize({"type": "interval", "minutes": 1})["minutes"] == 5
    assert schedule_service.normalize({"type": "interval", "minutes": 10**6})["minutes"] == 60 * 24 * 30


async def test_daily_schedule_uses_local_wall_clock():
    spec = schedule_service.normalize({"type": "daily", "times": ["08:00", "20:30"]})
    assert spec["times"] == ["08:00", "20:30"]

    # 本地 07:00 时，下一次应是当天 08:00（本地）
    local_now = datetime.now().replace(hour=7, minute=0, second=0, microsecond=0)
    now_utc = local_now.astimezone(timezone.utc)
    nxt = schedule_service.next_run_at(spec, now_utc=now_utc)
    assert nxt is not None
    assert (_local(nxt).hour, _local(nxt).minute) == (8, 0)
    assert _local(nxt).date() == local_now.date()

    # 本地 21:00 时，下一次应是明天的 08:00
    late_utc = local_now.replace(hour=21).astimezone(timezone.utc)
    nxt2 = schedule_service.next_run_at(spec, now_utc=late_utc)
    assert (_local(nxt2).hour, _local(nxt2).minute) == (8, 0)
    assert _local(nxt2).date() == (local_now + timedelta(days=1)).date()


async def test_weekly_schedule_picks_requested_weekday():
    # 周一 08:00；从周三开始算，下一次应落到下周一
    spec = schedule_service.normalize({"type": "weekly", "times": ["08:00"], "weekdays": [0]})
    wednesday = datetime.now()
    while wednesday.weekday() != 2:
        wednesday += timedelta(days=1)
    wednesday = wednesday.replace(hour=12, minute=0, second=0, microsecond=0)
    nxt = schedule_service.next_run_at(spec, now_utc=wednesday.astimezone(timezone.utc))
    assert _local(nxt).weekday() == 0
    assert (_local(nxt).hour, _local(nxt).minute) == (8, 0)
    assert "周一" in schedule_service.describe(spec)


async def test_once_schedule_parses_iso_and_local():
    at_local = datetime.now().replace(microsecond=0) + timedelta(hours=3)
    spec = schedule_service.normalize({"type": "once", "at": at_local.isoformat()})
    nxt = schedule_service.next_run_at(spec)
    assert nxt is not None
    assert abs((nxt - at_local).total_seconds()) < 1  # naive 输入按本地时间理解

    # 带时区的 ISO 串：换算回 UTC 后应指向同一时刻
    with_tz = schedule_service.normalize({"type": "once", "at": at_local.astimezone(timezone.utc).isoformat()})
    nxt2 = schedule_service.next_run_at(with_tz)
    expected_utc = at_local.astimezone(timezone.utc).replace(tzinfo=None)
    assert abs((nxt2 - expected_utc).total_seconds()) < 1


async def test_unknown_schedule_falls_back_to_manual():
    assert schedule_service.normalize({"type": "每秒钟"})["type"] == "manual"
    assert schedule_service.normalize(None)["type"] == "manual"


# ---------------------------------------------------------------- 条目挑选


def _probe(video_ids: list[str]) -> ProbeResult:
    return ProbeResult(
        source_type="playlist",
        title="测试合集",
        author="ch",
        source_id="PL",
        entries=[
            VideoInfo(
                video_id=vid,
                url=f"https://www.youtube.com/watch?v={vid}",
                webpage_url=f"https://www.youtube.com/watch?v={vid}",
                title=f"视频 {vid}",
                duration=60.0,
            )
            for vid in video_ids
        ],
    )


async def test_latest_mode_skips_already_taken_items():
    """持续更新的合集：最新的 1 条已经搬过时，应该顺延到下一个没搬过的。"""
    probe = _probe(["v3", "v2", "v1"])
    selection = plans_service.selection_config({"mode": "latest", "count": 1, "ignore_uploaded": True})

    picked = plans_service.pick_entries(probe, selection, taken={"v3"})
    assert [entry.video_id for entry in picked] == ["v2"], "去重必须在数量限制之前生效"

    picked2 = plans_service.pick_entries(probe, selection, taken=set())
    assert [entry.video_id for entry in picked2] == ["v3"]

    picked3 = plans_service.pick_entries(probe, selection, taken={"v1", "v2", "v3"})
    assert picked3 == []


async def test_ignore_uploaded_can_be_disabled():
    probe = _probe(["v2", "v1"])
    selection = plans_service.selection_config({"mode": "latest", "count": 1, "ignore_uploaded": False})
    picked = plans_service.pick_entries(probe, selection, taken={"v2"})
    assert [entry.video_id for entry in picked] == ["v2"]


async def test_selected_and_range_modes():
    probe = _probe(["a", "b", "c", "d"])
    selected = plans_service.selection_config(
        {"mode": "selected", "selected_video_ids": ["c", "a"], "ignore_uploaded": False}
    )
    assert [e.video_id for e in plans_service.pick_entries(probe, selected)] == ["c", "a"]

    # 区间语义：start/end 都是 1 起、且含端点 —— "第 2 到第 3 个" 就是 b、c 两个
    ranged = plans_service.selection_config({"mode": "range", "start": 2, "end": 3, "ignore_uploaded": False})
    assert [e.video_id for e in plans_service.pick_entries(probe, ranged)] == ["b", "c"]

    # 区间模式不叠加数量上限（用户已经用区间表达意图）
    tail = plans_service.selection_config({"mode": "range", "start": 3, "ignore_uploaded": False})
    assert [e.video_id for e in plans_service.pick_entries(probe, tail)] == ["c", "d"]

    # 区间 + 去重：区间内已搬过的会自动跳过
    ranged_dup = plans_service.selection_config({"mode": "range", "start": 1, "ignore_uploaded": True})
    assert [e.video_id for e in plans_service.pick_entries(probe, ranged_dup, taken={"a", "c"})] == ["b", "d"]


# ---------------------------------------------------------------- 数据层


async def _make_plan(**overrides) -> int:
    await init_db()
    async with SessionLocal() as session:
        plan = Plan(
            name=overrides.get("name", "每天搬最新"),
            source_url=overrides.get("source_url", "https://www.youtube.com/playlist?list=PL-test"),
            source_type="playlist",
            selection=overrides.get("selection", {"mode": "latest", "count": 1, "ignore_uploaded": True}),
            options={},
            schedule=overrides.get("schedule", {"type": "daily", "times": ["08:00"]}),
            enabled=overrides.get("enabled", True),
            auto_start=overrides.get("auto_start", False),
            next_run_at=overrides.get("next_run_at"),
        )
        session.add(plan)
        await session.commit()
        return plan.id


async def _taken_ids() -> set[str]:
    from sqlalchemy import select

    async with SessionLocal() as session:
        rows = (await session.execute(select(TaskItem.video_id))).scalars().all()
    return set(rows)


async def _cleanup() -> None:
    from sqlalchemy import delete

    async with SessionLocal() as session:
        await session.execute(delete(TaskItem))
        await session.execute(delete(Task))
        await session.execute(delete(Plan))
        await session.commit()


async def test_taken_video_ids_counts_pending_and_done_but_not_failed():
    await init_db()
    async with SessionLocal() as session:
        task = Task(title="t", source_url="u", status=TaskStatus.PENDING.value, total_items=3)
        session.add(task)
        await session.flush()
        for index, (vid, status) in enumerate((("done1", "succeeded"), ("wait1", "pending"), ("fail1", "failed"))):
            session.add(
                TaskItem(task_id=task.id, idx=index, video_id=vid, url="u", title=vid, status=status)
            )
        await session.commit()

        taken = await plans_service.taken_video_ids(session, ["done1", "wait1", "fail1", "new1"])
    assert taken == {"done1", "wait1"}, "失败/取消的条目应该允许重搬"
    await _cleanup()


# ---------------------------------------------------------------- 调度器


async def test_scheduler_triggers_due_plan_once(monkeypatch):
    await _cleanup()
    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    plan_id = await _make_plan(next_run_at=past)

    # 用假的探测结果替代真实 yt-dlp
    async def fake_probe(session, url):
        return _probe(["fresh1", "old1"])

    monkeypatch.setattr(plans_service, "probe_source", fake_probe)

    result = await plan_scheduler.tick()
    assert result["scanned"] >= 1
    assert any(item.get("plan_id") == plan_id for item in result["executed"])

    async with SessionLocal() as session:
        plan = await session.get(Plan, plan_id)
        tasks = (await session.execute(__import__("sqlalchemy").select(Task).where(Task.plan_id == plan_id))).scalars().all()

    assert len(tasks) == 1, "到点只应触发一次"
    assert tasks[0].total_items == 1
    assert plan.next_run_at is not None, "执行后应排好下一次"
    assert plan.next_run_at > datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    assert plan.run_count == 1

    # 再扫一次：还没到下一次时间，不应再建任务
    again = await plan_scheduler.tick()
    assert not any(item.get("plan_id") == plan_id for item in again["executed"])


async def test_scheduler_skips_plan_when_everything_uploaded(monkeypatch):
    await _cleanup()
    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    plan_id = await _make_plan(next_run_at=past)

    async def fake_probe(session, url):
        return _probe(["only1"])

    monkeypatch.setattr(plans_service, "probe_source", fake_probe)

    # 先把唯一那条标记为已搬运
    async with SessionLocal() as session:
        task = Task(title="已搬", source_url="u", status=TaskStatus.PENDING.value, total_items=1)
        session.add(task)
        await session.flush()
        session.add(TaskItem(task_id=task.id, idx=0, video_id="only1", url="u", title="t", status="succeeded"))
        await session.commit()

    await plan_scheduler.tick()
    async with SessionLocal() as session:
        plan = await session.get(Plan, plan_id)
        created = (await session.execute(__import__("sqlalchemy").select(Task).where(Task.plan_id == plan_id))).scalars().all()

    assert created == [], "已全部搬过时不应再建任务"
    assert "没有新的可搬运" in plan.last_message
    await _cleanup()


async def test_once_plan_disables_itself_after_run(monkeypatch):
    await _cleanup()
    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    plan_id = await _make_plan(schedule={"type": "once", "at": past.isoformat()}, next_run_at=past)

    async def fake_probe(session, url):
        return _probe(["a1"])

    monkeypatch.setattr(plans_service, "probe_source", fake_probe)

    await plan_scheduler.tick()
    async with SessionLocal() as session:
        plan = await session.get(Plan, plan_id)

    assert plan.next_run_at is None
    assert plan.enabled is False, "一次性计划跑完应自动关闭，避免每轮扫描都命中"
    assert plan.run_count == 1
    await _cleanup()


async def test_disabled_plan_is_never_picked_up():
    await _cleanup()
    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    plan_id = await _make_plan(next_run_at=past, enabled=False)

    result = await plan_scheduler.tick()
    assert not any(item.get("plan_id") == plan_id for item in result["executed"])
    await _cleanup()


async def test_plan_error_is_recorded_and_does_not_break_loop(monkeypatch):
    await _cleanup()
    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    plan_id = await _make_plan(next_run_at=past)

    async def boom(session, url):
        raise RuntimeError("解析失败：网络不可达")

    monkeypatch.setattr(plans_service, "probe_source", boom)

    result = await plan_scheduler.tick()
    entry = next(item for item in result["executed"] if item["plan_id"] == plan_id)
    assert entry["ok"] is False

    async with SessionLocal() as session:
        plan = await session.get(Plan, plan_id)

    assert plan.status == "error"
    assert "网络不可达" in plan.last_error
    assert plan.next_run_at is not None, "失败也要排下一次，不能卡死"
    await _cleanup()


# ---------------------------------------------------------------- 接口 + 流水线


async def _resolved_plan_items(plan_id: int):
    from sqlalchemy import select

    async with SessionLocal() as session:
        task = (
            await session.execute(select(Task).where(Task.plan_id == plan_id).order_by(Task.id.desc()))
        ).scalars().first()
        if task is None:
            return None, []
        items = (await session.execute(select(TaskItem).where(TaskItem.task_id == task.id))).scalars().all()
    return task, list(items)


async def test_plan_runs_end_to_end_with_real_pipeline(monkeypatch, sample_video, sample_srt):
    """计划 → 建任务 → 真跑流水线（离线 stub 提供者），确认能产出成片。"""
    from app.core.config import settings
    from app.db import init_db
    from app.models import TaskStatus as TS
    from app.pipeline.runner import task_runner
    from app.services.settings_store import settings_store
    from tests.test_pipeline_e2e import StubDownloader

    import app.pipeline.runner as runner_module

    await init_db()
    async with SessionLocal() as session:
        await settings_store.update(
            session,
            {
                "translator": {"provider": "mock"},
                "tts": {"provider": "mock", "sample_rate": 24000, "concurrency": 2},
                "publish": {"provider": "mock", "auto_publish": False},
                "asr": {"provider": "mock", "enabled": True},
                "video": {"target_aspect": "original", "burn_subtitles": True, "preset": "ultrafast", "crf": 28},
                "intro": {"enabled": False},
                "general": {"max_concurrent_tasks": 1},
            },
        )

    stub = StubDownloader(sample_video, sample_srt)
    real_build_all = runner_module.build_all

    def fake_build_all(config, account_file):
        providers = real_build_all(config, account_file)
        providers["downloader"] = stub
        return providers

    monkeypatch.setattr(runner_module, "build_all", fake_build_all)

    async def fake_probe(session, url):
        return ProbeResult(
            source_type="video",
            title="计划端到端",
            author="ch",
            source_id="plan0001",
            entries=[
                VideoInfo(
                    video_id="plan0001",
                    url="https://www.youtube.com/watch?v=plan0001",
                    webpage_url="https://www.youtube.com/watch?v=plan0001",
                    title="计划端到端",
                    duration=12.0,
                )
            ],
        )

    monkeypatch.setattr(plans_service, "probe_source", fake_probe)

    plan_id = await _make_plan(
        name="端到端计划",
        selection={"mode": "all", "ignore_uploaded": True},
        schedule={"type": "manual"},
        auto_start=True,
    )

    async with SessionLocal() as session:
        plan = await session.get(Plan, plan_id)
        outcome = await plans_service.run_plan(session, plan, trigger="手动")
    assert outcome["created"] == 1

    task, items = await _resolved_plan_items(plan_id)
    assert task is not None and len(items) == 1

    await task_runner.submit(task.id)  # 幂等：run_plan 已经提交过
    from app.pipeline.runner import task_runner as runner2

    handle = runner2._handles.get(task.id)
    if handle and handle.task:
        await handle.task

    async with SessionLocal() as session:
        task = await session.get(Task, task.id)
        item = (await session.execute(
            __import__("sqlalchemy").select(TaskItem).where(TaskItem.task_id == task.id)
        )).scalars().first()

    assert task.status == TS.SUCCEEDED.value, task.message
    assert item.output_path and (settings.data_dir / item.output_path).exists()
    # 阶段完成标记已落库：再次执行时这些阶段会被直接跳过。
    # （probe 因为条目的 title/duration 已由探测写入，本轮本身就是「跳过」，
    #   所以这里断言的是真正实跑过的阶段。）
    completed = (item.stats or {}).get("stages_completed", [])
    for stage in ("download", "translate", "tts", "align", "publish"):
        assert stage in completed, f"{stage} 未记录完成标记：{completed}"

    await _cleanup()


# ---------------------------------------------------------------- 接口层


async def test_plan_api_crud_and_dedupe(monkeypatch, tmp_root):
    """接口闭环：创建 → 列表 → 预演 → 手动运行 → 去重 → 记录 → 暂停/删除。"""
    import httpx

    from app.main import app

    await _cleanup()
    await init_db()

    async def fake_probe(session, url):
        return _probe(["a1", "a2", "a3", "a4"])

    monkeypatch.setattr(plans_service, "probe_source", fake_probe)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        created = await http.post(
            "/api/plans",
            json={
                "name": "最新两个",
                "source_url": "https://www.youtube.com/playlist?list=PL-test",
                "schedule": {"type": "daily", "times": ["09:00"]},
                "selection": {"mode": "latest", "count": 2, "ignore_uploaded": True},
                "auto_start": False,
            },
        )
        assert created.status_code == 201, created.text
        plan = created.json()
        assert plan["schedule_text"].startswith("每天")
        assert plan["next_run_at"], "定时计划必须有下次执行时间"

        listed = await http.get("/api/plans")
        assert listed.status_code == 200, listed.text
        assert listed.json()["total"] == 1

        preview = await http.post(f"/api/plans/{plan['id']}/run", json={"probe_only": True})
        assert preview.status_code == 200
        assert preview.json()["created"] == 0
        assert len(preview.json()["candidates"]) == 4

        first = await http.post(f"/api/plans/{plan['id']}/run", json={})
        assert first.json()["created"] == 2

        # 第二次运行：前两个已建过，应顺延到 a3/a4
        second = await http.post(f"/api/plans/{plan['id']}/run", json={})
        assert second.json()["created"] == 2
        assert second.json()["task_id"] != first.json()["task_id"]
        async with SessionLocal() as session:
            from sqlalchemy import select

            task = await session.get(Task, second.json()["task_id"])
            items = (
                await session.execute(select(TaskItem).where(TaskItem.task_id == task.id).order_by(TaskItem.idx))
            ).scalars().all()
        assert [item.video_id for item in items] == ["a3", "a4"]

        # 第三次：全搬完了，不应再建任务
        third = await http.post(f"/api/plans/{plan['id']}/run", json={})
        assert third.json()["created"] == 0
        assert third.json()["task_id"] is None

        history = await http.get(f"/api/plans/{plan['id']}/history")
        assert history.status_code == 200
        assert history.json()["total"] == 2

        paused = await http.post(f"/api/plans/{plan['id']}/toggle", params={"enabled": "false"})
        assert paused.json()["enabled"] is False
        assert paused.json()["next_run_at"] is None

        removed = await http.delete(f"/api/plans/{plan['id']}")
        assert removed.status_code == 200
        assert (await http.get("/api/plans")).json()["total"] == 0

    await _cleanup()


async def test_plan_api_rejects_invalid_schedule():
    import httpx

    from app.main import app

    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        bad = await http.post(
            "/api/plans",
            json={
                "name": "缺时间",
                "source_url": "https://www.youtube.com/watch?v=x",
                "schedule": {"type": "once"},
            },
        )
        assert bad.status_code == 400
        assert "执行时间" in bad.json()["detail"]


async def test_plan_out_exposes_local_time_text():
    """接口必须给出换算到本机时区的时间串，否则前端会把 UTC 当本地时间显示。"""
    from datetime import datetime, timezone

    from app.api.plans import _to_out

    plan = Plan(
        id=7,
        name="每天 08:00",
        source_url="u",
        source_type="video",
        selection={},
        options={},
        schedule={"type": "daily", "times": ["08:00"]},
        enabled=True,
        auto_start=True,
        status="idle",
        last_message="",
        last_error="",
        # 库里存的是本机 08:00 对应的 UTC 时刻
        next_run_at=datetime(2026, 9, 18, 0, 0),
        run_count=0,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    out = _to_out(plan)

    expected = (
        datetime(2026, 9, 18, 0, 0).replace(tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    )
    assert out.next_run_at_local == expected
    # 至少要带上小时（避免只比对到分钟导致时区偏移被忽略）
    assert ":" in out.next_run_at_local
    assert out.last_run_at_local == ""
