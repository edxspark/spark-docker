"""API 请求/响应模型。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    cover_landscape_path: str = ""
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
    # 干跑：完整上传并填好表单，但停在「发布」按钮前，不真正发出作品。
    # 用于发布前自检，避免第一次失败发生在视频已上传之后。
    dry_run: bool = False
    # 重新发布：对「已发布过」的条目再上传一次。
    # 必须显式传 true——抖音不会因为重传而替换旧作品，否则会静默多出一个作品。
    republish: bool = False

# --------------------------------------------------------------------------------------
# 搬运计划
# --------------------------------------------------------------------------------------


class PlanCreateRequest(BaseModel):
    name: str = ""
    source_url: str = Field(min_length=5)
    source_type: str = ""
    author: str = ""
    # 搬运范围：{"mode": ..., "count": N, "start": N, "end": N, "selected_video_ids": [...]}
    selection: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)
    # 调度：{"type": "manual|interval|daily|weekly|once", ...}
    schedule: dict[str, Any] = Field(default_factory=dict)
    # 注意默认值必须是 True 而不是 None：前端通常不显式传这两个字段，
    # 而 exclude_unset 会让 None 落到模型上，计划就变成「既没启用也没排期」
    enabled: bool = True
    auto_start: bool = True

    @field_validator("source_url", "name")
    @classmethod
    def _strip(cls, v: str) -> str:
        return (v or "").strip()


class PlanUpdateRequest(PlanCreateRequest):
    """更新计划：所有字段可选，未提供的不改动。"""

    source_url: str | None = None  # type: ignore[assignment]
    name: str | None = None  # type: ignore[assignment]


class PlanOut(BaseModel):
    """计划的对外表示。

    `next_run_at` / `last_run_at` 在库里是"naive UTC"（与其它时间字段一致），
    如果直接返回给前端，浏览器会把它当成本地时间解析，于是"每天 08:00"显示成
    00:00（时区非 0 时整体偏移）。因此额外给出已经换算成本地时间的
    `next_run_at_local` / `last_run_at_local`，前端直接用这两个字段展示。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    source_url: str
    source_type: str
    author: str
    selection: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)
    schedule: dict[str, Any] = Field(default_factory=dict)
    schedule_text: str = ""
    enabled: bool
    auto_start: bool
    status: str
    last_message: str
    last_error: str
    next_run_at: datetime | None
    last_run_at: datetime | None
    run_count: int
    last_task_id: int | None
    created_at: datetime
    updated_at: datetime
    # 已换算成本机时区的展示用字符串（空字符串表示没有）
    next_run_at_local: str = ""
    last_run_at_local: str = ""

    @staticmethod
    def _local_text(value: datetime | None) -> str:
        if value is None:
            return ""
        if value.tzinfo is None:
            # 库里存的是 UTC，补上时区再转本地
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone().strftime("%Y-%m-%d %H:%M")

    @model_validator(mode="after")
    def _fill_local_text(self) -> PlanOut:
        self.next_run_at_local = self._local_text(self.next_run_at)
        self.last_run_at_local = self._local_text(self.last_run_at)
        return self


class PlanPageOut(BaseModel):
    """计划列表分页（不能复用 PageOut：那个模型的 items 是 TaskOut）。"""

    total: int
    page: int
    page_size: int
    items: list[PlanOut] = Field(default_factory=list)


class PlanRunRequest(BaseModel):
    """手动执行一次；probe_only 只返回候选条目，不创建任务。"""

    probe_only: bool = False
    selected_video_ids: list[str] | None = Field(default=None, max_length=5000)
    limit: int | None = Field(default=None, ge=1, le=500)
    ignore_uploaded: bool | None = None


class PlanRunResult(BaseModel):
    ok: bool = True
    created: int = 0
    task_id: int | None = None
    skipped: int = 0
    message: str = ""
    candidates: list[dict[str, Any]] = Field(default_factory=list)
