"""ORM 模型：搬运任务、任务条目、日志、抖音账号、键值配置。"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TaskStatus(str, enum.Enum):
    PENDING = "pending"        # 已创建，等待调度
    RUNNING = "running"        # 流水线执行中
    SUCCEEDED = "succeeded"    # 全部条目成功
    PARTIAL = "partial"        # 部分条目成功
    FAILED = "failed"          # 全部条目失败
    CANCELED = "canceled"      # 用户取消
    PAUSED = "paused"          # 用户暂停


TERMINAL_STATUSES = {
    TaskStatus.SUCCEEDED.value,
    TaskStatus.PARTIAL.value,
    TaskStatus.FAILED.value,
    TaskStatus.CANCELED.value,
}

# 流水线阶段（顺序即执行顺序）
PIPELINE_STAGES = [
    ("probe", "解析链接"),
    ("download", "下载视频与字幕"),
    ("subtitle", "字幕清洗与断句"),
    ("translate", "翻译字幕"),
    ("tts", "语音合成"),
    ("align", "时间轴对齐与合成"),
    ("metadata", "生成标题与话题"),
    ("publish", "发布到抖音"),
]
STAGE_LABELS = dict(PIPELINE_STAGES)
STAGE_ORDER = [key for key, _ in PIPELINE_STAGES]


class SourceType(str, enum.Enum):
    VIDEO = "video"
    PLAYLIST = "playlist"
    CHANNEL = "channel"


class Task(Base):
    """一次搬运请求。单个视频 = 1 个条目；合集/频道 = N 个条目。"""

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    source_url: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(32), default=SourceType.VIDEO.value)
    source_id: Mapped[str] = mapped_column(String(128), default="")
    author: Mapped[str] = mapped_column(String(256), default="")

    status: Mapped[str] = mapped_column(String(32), default=TaskStatus.PENDING.value, index=True)
    stage: Mapped[str] = mapped_column(String(32), default="")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str] = mapped_column(String(512), default="")
    error: Mapped[str] = mapped_column(Text, default="")

    total_items: Mapped[int] = mapped_column(Integer, default=0)
    done_items: Mapped[int] = mapped_column(Integer, default=0)
    failed_items: Mapped[int] = mapped_column(Integer, default=0)

    # 任务选项：配音音色、是否发布、定时发布、翻译风格等
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    # 来自哪个搬运计划（手动创建的任务为 None）
    plan_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    items: Mapped[list[TaskItem]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="TaskItem.idx", lazy="selectin"
    )
    logs: Mapped[list[TaskLog]] = relationship(
        back_populates="task", cascade="all, delete-orphan", lazy="noload"
    )

    __table_args__ = (Index("ix_tasks_status_created", "status", "created_at"),)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class TaskItem(Base):
    """任务中的一个视频条目，承载文件路径与各阶段产物。"""

    __tablename__ = "task_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    idx: Mapped[int] = mapped_column(Integer, default=0)

    video_id: Mapped[str] = mapped_column(String(64), default="")
    url: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str] = mapped_column(String(512), default="")
    title_zh: Mapped[str] = mapped_column(String(512), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str] = mapped_column(String(256), default="")
    duration: Mapped[float] = mapped_column(Float, default=0.0)
    thumbnail: Mapped[str] = mapped_column(Text, default="")
    upload_date: Mapped[str] = mapped_column(String(32), default="")
    view_count: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[str] = mapped_column(String(32), default=TaskStatus.PENDING.value, index=True)
    stage: Mapped[str] = mapped_column(String(32), default="")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str] = mapped_column(String(512), default="")
    error: Mapped[str] = mapped_column(Text, default="")

    # 产物路径（相对 data 目录，便于整体迁移）
    video_path: Mapped[str] = mapped_column(Text, default="")
    subtitle_source_path: Mapped[str] = mapped_column(Text, default="")
    subtitle_zh_path: Mapped[str] = mapped_column(Text, default="")
    dubbed_audio_path: Mapped[str] = mapped_column(Text, default="")
    output_path: Mapped[str] = mapped_column(Text, default="")
    cover_path: Mapped[str] = mapped_column(Text, default="")
    # 横封面（抖音要求 4:3）。平台对「竖封面」与「横封面」分别取图，
    # 只设竖封面会导致横版位缺失。
    cover_landscape_path: Mapped[str] = mapped_column(Text, default="")

    # 发布结果
    publish_status: Mapped[str] = mapped_column(String(32), default="")
    publish_url: Mapped[str] = mapped_column(Text, default="")
    publish_error: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 统计与中间数据：字幕条数、翻译 token、TTS 字符数、耗时明细
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    task: Mapped[Task] = relationship(back_populates="items")


class TaskLog(Base):
    __tablename__ = "task_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    level: Mapped[str] = mapped_column(String(16), default="info")
    stage: Mapped[str] = mapped_column(String(32), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    task: Mapped[Task] = relationship(back_populates="logs")


class DouyinAccount(Base):
    """抖音创作者账号登录态（Playwright storage_state）。"""

    __tablename__ = "douyin_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), default="default")
    nickname: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(32), default="logged_out")  # logged_in / logged_out / expired
    storage_state_path: Mapped[str] = mapped_column(Text, default="")
    is_default: Mapped[bool] = mapped_column(default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Setting(Base):
    """键值配置。密钥字段以 enc:v1: 前缀加密存储。"""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)
    is_secret: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (UniqueConstraint("key", name="uq_settings_key"),)


class PlanStatus(str, enum.Enum):
    IDLE = "idle"          # 待命（手动计划或等待到点）
    RUNNING = "running"    # 正在创建/执行本次任务
    ERROR = "error"        # 上次执行失败


class Plan(Base):
    """搬运计划：把「什么时候搬什么」预先存下来，到点自动建任务。

    与 Task 的关系：计划本身不承载流水线，只在触发时创建 Task（可留痕 plan_id）。
    """

    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256), default="")
    source_url: Mapped[str] = mapped_column(Text, default="")
    source_type: Mapped[str] = mapped_column(String(32), default=SourceType.VIDEO.value)
    author: Mapped[str] = mapped_column(String(256), default="")

    # 搬运范围：{"mode": "all|first_n|latest|range", "count": N, "start": N, "page_size": N,
    #           "selected_video_ids": [...], "ignore_uploaded": bool}
    selection: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 任务选项（音色、发布、画面等），与 Task.options 同结构
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 调度：{"type": "manual|interval|daily|weekly|once", "minutes": N, "times": ["08:00"],
    #        "weekdays": [0-6], "at": "ISO8601(UTC)"}
    schedule: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    enabled: Mapped[bool] = mapped_column(default=True, index=True)
    auto_start: Mapped[bool] = mapped_column(default=True)

    status: Mapped[str] = mapped_column(String(32), default=PlanStatus.IDLE.value, index=True)
    last_message: Mapped[str] = mapped_column(String(512), default="")
    last_error: Mapped[str] = mapped_column(Text, default="")

    # 下次触发时间（naive UTC；手动计划为 None）
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    run_count: Mapped[int] = mapped_column(Integer, default=0)
    last_task_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
