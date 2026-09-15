"""成片预览（内嵌播放）的接口测试。

关键点：<video> 要能拖动进度条，服务端必须支持 HTTP Range。
Starlette 的 FileResponse 提供了该能力，这里用真实请求把它固化下来，
避免将来换成 StreamingResponse 或自己读文件时静默退化。
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import delete

from app.core.config import settings
from app.db import SessionLocal, init_db
from app.models import Task, TaskItem, TaskStatus


@pytest.fixture
async def client(tmp_root):
    await init_db()
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
async def ready_item(tmp_root, sample_video):
    """造一个已有成片的已完成任务。"""
    task_id, item_id = 9001, 9001
    async with SessionLocal() as session:
        await session.execute(delete(TaskItem).where(TaskItem.task_id == task_id))
        await session.execute(delete(Task).where(Task.id == task_id))
        await session.commit()

        output_dir = settings.outputs_dir / str(task_id)
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / "demo.mp4"
        output.write_bytes(sample_video.read_bytes())

        session.add(
            Task(
                id=task_id,
                title="预览测试任务",
                source_url="https://www.youtube.com/watch?v=x",
                source_type="video",
                status=TaskStatus.SUCCEEDED.value,
                total_items=1,
                done_items=1,
            )
        )
        session.add(
            TaskItem(
                id=item_id,
                task_id=task_id,
                idx=0,
                video_id="demo",
                url="https://www.youtube.com/watch?v=x",
                title="预览测试",
                status=TaskStatus.SUCCEEDED.value,
                output_path=str(output.relative_to(settings.data_dir)),
            )
        )
        await session.commit()
    yield task_id, item_id, output
    async with SessionLocal() as session:
        await session.execute(delete(TaskItem).where(TaskItem.task_id == task_id))
        await session.execute(delete(Task).where(Task.id == task_id))
        await session.commit()


class TestInlinePlayback:
    async def test_inline_disposition_for_playback(self, client, ready_item):
        task_id, item_id, _ = ready_item
        response = await client.get(
            f"/api/tasks/{task_id}/items/{item_id}/file",
            params={"kind": "output", "inline": True},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("video/")
        # inline 才能在页面内嵌播放；attachment 会触发下载
        assert response.headers["content-disposition"].startswith("inline")

    async def test_attachment_by_default(self, client, ready_item):
        task_id, item_id, _ = ready_item
        response = await client.get(
            f"/api/tasks/{task_id}/items/{item_id}/file", params={"kind": "output"}
        )
        assert response.status_code == 200
        assert response.headers["content-disposition"].startswith("attachment")

    async def test_accept_ranges_advertised(self, client, ready_item):
        task_id, item_id, _ = ready_item
        response = await client.get(
            f"/api/tasks/{task_id}/items/{item_id}/file",
            params={"kind": "output", "inline": True},
        )
        assert response.headers.get("accept-ranges") == "bytes", "未声明支持 Range，浏览器无法拖动进度"

    async def test_range_request_returns_partial_content(self, client, ready_item):
        """拖动进度条的核心：必须返回 206 与正确的 Content-Range。"""
        task_id, item_id, output = ready_item
        total = output.stat().st_size

        response = await client.get(
            f"/api/tasks/{task_id}/items/{item_id}/file",
            params={"kind": "output", "inline": True},
            headers={"Range": "bytes=0-1023"},
        )
        assert response.status_code == 206
        assert response.headers["content-range"] == f"bytes 0-1023/{total}"
        assert len(response.content) == 1024

    async def test_range_from_middle(self, client, ready_item):
        task_id, item_id, output = ready_item
        total = output.stat().st_size
        start = total // 2
        response = await client.get(
            f"/api/tasks/{task_id}/items/{item_id}/file",
            params={"kind": "output", "inline": True},
            headers={"Range": f"bytes={start}-"},
        )
        assert response.status_code == 206
        assert response.headers["content-range"].startswith(f"bytes {start}-")
        assert len(response.content) == total - start

    async def test_unsatisfiable_range_returns_416(self, client, ready_item):
        task_id, item_id, output = ready_item
        response = await client.get(
            f"/api/tasks/{task_id}/items/{item_id}/file",
            params={"kind": "output", "inline": True},
            headers={"Range": f"bytes={output.stat().st_size + 10}-"},
        )
        assert response.status_code == 416


class TestMissingArtifacts:
    async def test_unknown_kind_is_rejected(self, client, ready_item):
        task_id, item_id, _ = ready_item
        response = await client.get(
            f"/api/tasks/{task_id}/items/{item_id}/file", params={"kind": "not-a-kind"}
        )
        assert response.status_code == 422

    async def test_missing_product_returns_404(self, client, ready_item):
        task_id, item_id, _ = ready_item
        # 该条目没有封面
        response = await client.get(
            f"/api/tasks/{task_id}/items/{item_id}/file", params={"kind": "cover"}
        )
        assert response.status_code == 404

    async def test_unknown_item_returns_404(self, client, ready_item):
        task_id, _, _ = ready_item
        response = await client.get(f"/api/tasks/{task_id}/items/999999/file")
        assert response.status_code == 404


class TestRepublish:
    """重新发布：对已发布过的条目再上传一次。

    抖音不会用新上传替换旧作品，因此必须显式确认，否则会静默多出一个作品。
    """

    async def test_published_item_requires_explicit_republish(self, client, ready_item):
        from sqlalchemy import select

        task_id, item_id, _ = ready_item
        async with SessionLocal() as session:
            item = (
                await session.execute(select(TaskItem).where(TaskItem.id == item_id))
            ).scalars().first()
            assert item is not None
            item.publish_status = "published"
            item.publish_url = "https://www.douyin.com/video/old"
            await session.commit()

        # 不带 republish：应当拒绝，避免悄悄多出一个作品
        response = await client.post(f"/api/tasks/{task_id}/items/{item_id}/publish", json={})
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "已发布过" in detail and "republish" in detail

    async def test_failed_item_can_publish_without_flag(self, client, ready_item):
        """失败过的条目属于正常重试，不应要求额外确认。"""
        from sqlalchemy import select

        task_id, item_id, _ = ready_item
        async with SessionLocal() as session:
            item = (
                await session.execute(select(TaskItem).where(TaskItem.id == item_id))
            ).scalars().first()
            assert item is not None
            item.publish_status = "failed"
            await session.commit()

        # 不带 republish 时应能进入发布流程（mock 发布器会成功）
        response = await client.post(f"/api/tasks/{task_id}/items/{item_id}/publish", json={})
        assert response.status_code == 200
        assert response.json()["publish_status"] == "published"


class TestPublishHistory:
    """每次发布尝试都要留痕：重发是常见操作，需要能回看历次结果。"""

    def test_records_attempt_with_kind_and_result(self):
        from app.api.tasks import _record_publish_attempt
        from app.models import TaskItem

        item = TaskItem()
        _record_publish_attempt(item, kind="publish", success=True, message="首次成功", url="u1")
        _record_publish_attempt(item, kind="republish", success=False, message="重发失败")
        history = (item.stats or {})["publish_history"]
        assert len(history) == 2
        assert history[0]["kind"] == "publish" and history[0]["success"] is True
        assert history[1]["kind"] == "republish" and history[1]["success"] is False
        assert history[1]["url"] == ""

    def test_history_is_capped(self):
        from app.api.tasks import _PUBLISH_HISTORY_LIMIT, _record_publish_attempt
        from app.models import TaskItem

        item = TaskItem()
        for i in range(_PUBLISH_HISTORY_LIMIT + 5):
            _record_publish_attempt(item, kind="publish", success=True, message=f"第{i}次")
        history = (item.stats or {})["publish_history"]
        assert len(history) == _PUBLISH_HISTORY_LIMIT, "历史应被截断，避免 stats 无限膨胀"
        # 保留的是最近的若干次
        assert history[-1]["message"] == f"第{_PUBLISH_HISTORY_LIMIT + 4}次"

    def test_history_survives_missing_stats(self):
        from app.api.tasks import _record_publish_attempt
        from app.models import TaskItem

        item = TaskItem()
        item.stats = None
        _record_publish_attempt(item, kind="publish", success=True, message="x")
        assert len((item.stats or {})["publish_history"]) == 1
