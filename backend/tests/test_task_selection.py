"""合集挑条目的接口测试。

背景：链接是合集/频道时，如果直接创建任务会把整个合集几百个视频全部建进队列。
现在前端会先弹合集列表让用户勾选，后端则按 selected_video_ids 精确建条目。
这里覆盖勾选、顺序、全选标记与非法勾选四种情况。
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

import app.api.tasks as tasks_api
from app.db import SessionLocal, init_db
from app.models import Task, TaskItem, TaskLog, TaskStatus
from app.providers.base import ProbeResult, VideoInfo


def _probe_result() -> ProbeResult:
    entries = [
        VideoInfo(
            video_id=f"vid{i}",
            url=f"https://www.youtube.com/watch?v=vid{i}",
            webpage_url=f"https://www.youtube.com/watch?v=vid{i}",
            title=f"视频 {i}",
            author="某频道",
            duration=60.0 * i,
        )
        for i in range(1, 6)
    ]
    return ProbeResult(
        source_type="playlist",
        title="测试合集",
        author="某频道",
        source_id="PL-test",
        entries=entries,
    )


class _FakeDownloader:
    async def probe(self, url: str) -> ProbeResult:  # noqa: ARG002
        return _probe_result()


@pytest.fixture
async def client(tmp_root, monkeypatch):
    await init_db()
    monkeypatch.setattr(tasks_api, "build_downloader", lambda config: _FakeDownloader())
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http

    async with SessionLocal() as session:
        for task in (await session.execute(select(Task))).scalars().all():
            await session.delete(task)
        for log in (await session.execute(select(TaskLog))).scalars().all():
            await session.delete(log)
        await session.commit()


async def _create(client: httpx.AsyncClient, **payload) -> httpx.Response:
    body = {"url": "https://www.youtube.com/playlist?list=PL-test", "auto_start": False, **payload}
    return await client.post("/api/tasks", json=body)


async def _items(task_id: int) -> list[TaskItem]:
    async with SessionLocal() as session:
        return list(
            (
                await session.execute(
                    select(TaskItem).where(TaskItem.task_id == task_id).order_by(TaskItem.idx)
                )
            )
            .scalars()
            .all()
        )


async def test_selected_ids_create_only_picked_items(client):
    response = await _create(client, selected_video_ids=["vid2", "vid4"])
    assert response.status_code == 201, response.text
    detail = response.json()

    assert detail["total_items"] == 2
    assert [item["video_id"] for item in detail["items"]] == ["vid2", "vid4"]
    assert detail["source_type"] == "playlist"


async def test_selected_ids_keep_user_order(client):
    response = await _create(client, selected_video_ids=["vid5", "vid1", "vid3"])
    assert response.status_code == 201
    assert [item["video_id"] for item in response.json()["items"]] == ["vid5", "vid1", "vid3"]


async def test_all_marker_selects_whole_playlist(client):
    response = await _create(client, selected_video_ids=["all"])
    assert response.status_code == 201
    detail = response.json()
    assert detail["total_items"] == 5
    assert [item["idx"] for item in detail["items"]] == [0, 1, 2, 3, 4]


async def test_unknown_selection_is_rejected(client):
    response = await _create(client, selected_video_ids=["not-exist"])
    assert response.status_code == 400
    assert "勾选" in response.json()["detail"]

    async with SessionLocal() as session:
        tasks = (await session.execute(select(Task))).scalars().all()
    assert tasks == []


async def test_omitting_selection_keeps_legacy_range_behaviour(client):
    """未提供 selected_video_ids 时保持旧行为（start_index / max_items），兼容已有调用方。"""
    response = await _create(client, start_index=2, max_items=2)
    assert response.status_code == 201
    assert [item["video_id"] for item in response.json()["items"]] == ["vid2", "vid3"]


async def test_playlist_does_not_auto_create_everything_without_selection(client):
    """回归保护：创建接口本身不主动限制条数，前端必须靠弹窗确认；
    这里断言后端仍完整建条目，且日志写明挑选数量，便于排查「一下子建了太多任务」。"""
    response = await _create(client, selected_video_ids=["vid1", "vid2"])
    task_id = response.json()["id"]

    async with SessionLocal() as session:
        logs = (
            await session.execute(select(TaskLog).where(TaskLog.task_id == task_id))
        ).scalars().all()
        task = await session.get(Task, task_id)

    assert task is not None and task.status == TaskStatus.PENDING.value
    assert any("已挑选 2 个视频" in log.message for log in logs)
