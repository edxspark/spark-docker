"""API 请求/响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import STAGE_LABELS, STAGE_ORDER


class TaskOptions(BaseModel):
    """任务级选项：覆盖系统配置中的对应项。None 表示跟随系统配置。"""

    voice: str | None = None
    auto_publish: bool | None = None
    schedule_offset_minutes: int | None = Field(default=None, ge=0, le=14 * 24 * 60)
    schedule_at: str | None = None
    target_aspect: Literal["original", "9:16", "16:9"] | None = None
    burn_subtitles: bool | None = None
    subtitle_mode: Literal["bilingual", "zh", "en"] | None = None
    subtitle_alignment: Literal["bottom", "middle", "top"] | None = None
    original_audio: Literal["remove", "keep"] | None = None
    bgm_volume: float | None = Field(default=None, ge=0.0, le=2.0)
    description: str | None = None
    tags: list[str] | None = None


class CreateTaskRequest(BaseModel):
    url: str = Field(min_length=5)
    title: str = ""
    # 合集精确挑中的条目标识（按顺序即条目顺序）。提供时优先于 start_index/max_items，
    # 前端弹窗让用户逐条勾选合集条目后即用它提交，避免「一下子创建很多任务」。
    # 特殊值 "all" 表示用户在弹窗里显式确认了「全选」。
    selected_video_ids: list[str] | None = Field(default=None, max_length=5000)
    # 合集只搬运前 N 个（0 或 null 表示全部）
    max_items: int | None = Field(default=None, ge=0, le=500)
    # 从第几个开始（1 起）
    start_index: int = Field(default=1, ge=1)
    options: TaskOptions = Field(default_factory=TaskOptions)
    # 是否立即执行
    auto_start: bool = True

    @field_validator("url")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class ProbeEntry(BaseModel):
    video_id: str = ""
    url: str = ""
    title: str = ""
    author: str = ""
    duration: float = 0.0
    thumbnail: str = ""
    upload_date: str = ""
    view_count: int = 0


class ProbeResponse(BaseModel):
    source_type: str
    title: str = ""
    author: str = ""
    source_id: str = ""
    total: int = 0
    entries: list[ProbeEntry] = Field(default_factory=list)


class TaskItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    idx: int
    video_id: str
    url: str
    title: str
    title_zh: str
    author: str
    duration: float
    thumbnail: str
    status: str
    stage: str
    progress: float
    message: str
    error: str
    video_path: str
    subtitle_source_path: str
    subtitle_zh_path: str
    dubbed_audio_path: str
    output_path: str
    cover_path: str
    publish_status: str
    publish_url: str
    publish_error: str
    published_at: datetime | None
    stats: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    source_url: str
    source_type: str
    source_id: str
    author: str
    status: str
    stage: str
    progress: float
    message: str
    error: str
    total_items: int
    done_items: int
    failed_items: int
    options: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class TaskDetailOut(TaskOut):
    items: list[TaskItemOut] = Field(default_factory=list)


class TaskLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    item_id: int | None
    level: str
    stage: str
    message: str
    created_at: datetime


class PageOut(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[TaskOut] = Field(default_factory=list)


class StageMeta(BaseModel):
    key: str
    label: str


class PipelineMeta(BaseModel):
    stages: list[StageMeta]
    statuses: list[str]


PIPELINE_META = PipelineMeta(
    stages=[StageMeta(key=k, label=STAGE_LABELS[k]) for k in STAGE_ORDER],
    statuses=["pending", "running", "succeeded", "partial", "failed", "canceled", "paused"],
)


class StatsOverview(BaseModel):
    total_tasks: int = 0
    running_tasks: int = 0
    succeeded_tasks: int = 0
    failed_tasks: int = 0
    total_videos: int = 0
    published_videos: int = 0
    total_sentences: int = 0
    total_characters: int = 0
    total_tokens: int = 0
    disk_usage_mb: float = 0.0
    recent_tasks: list[TaskOut] = Field(default_factory=list)
    daily: list[dict[str, Any]] = Field(default_factory=list)


class TestResult(BaseModel):
    ok: bool
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class MessageOut(BaseModel):
    ok: bool = True
    message: str = ""


class PublishItemRequest(BaseModel):
    """手动发布参数。

    手动发布默认为「立刻上传」：系统配置里的定时延迟只对自动发布生效，
    否则用户点「立即发布」却被静默排到几小时之后，与所见不符。
    需要定时发布时显式传 immediate=false。
    """

    immediate: bool = True
