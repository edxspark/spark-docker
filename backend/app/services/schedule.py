"""搬运计划的调度时间计算。

约定
----
- 用户在界面上填的是**本机时区**的时间（"每天 08:00" 指的是本地 08:00），
  因此这里用 `astimezone()` 在本地时区与 UTC 之间换算。
- 落库统一为 **naive UTC**（与 Task.started_at 等字段一致，避免 SQLite 时区不一致）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

SCHEDULE_TYPES = ("manual", "interval", "daily", "weekly", "once")

# 周计划用的星期编号（0 = 周一），与前端一致
WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _local_now(now_utc: datetime | None = None) -> datetime:
    """当前本地时间（naive）。"""
    current = now_utc or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone().replace(tzinfo=None)


def _to_utc_naive(local_dt: datetime) -> datetime:
    return local_dt.astimezone(timezone.utc).replace(tzinfo=None)


def _as_local(value: datetime) -> datetime:
    """把「无时区的时间」理解成本地时间（前端提交的正是本地时间）。

    直接用 as_utc 会把本地时间当 UTC，时区非 0 时整体偏移数小时——
    实测在东八区，"3 小时后执行"会被存成"11 小时后执行"。
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).astimezone()
    return value


def _parse_times(raw: Any) -> list[tuple[int, int]]:
    """把 ["08:00", "20:30"] 解析成 [(8, 0), (20, 30)]（去重并排序）。"""
    out: set[tuple[int, int]] = set()
    for item in raw or []:
        text = str(item).strip()
        if ":" not in text:
            continue
        hour_text, _, minute_text = text.partition(":")
        try:
            hour, minute = int(hour_text), int(minute_text)
        except ValueError:
            continue
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            out.add((hour, minute))
    return sorted(out)


def _parse_weekdays(raw: Any) -> list[int]:
    out: set[int] = set()
    for item in raw or []:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if 0 <= value <= 6:
            out.add(value)
    return sorted(out)


def normalize(schedule: dict[str, Any] | None) -> dict[str, Any]:
    """补齐/纠正用户提交的调度配置。"""
    data = dict(schedule or {})
    kind = str(data.get("type") or "manual")
    if kind not in SCHEDULE_TYPES:
        kind = "manual"

    result: dict[str, Any] = {"type": kind}
    if kind == "interval":
        minutes = data.get("minutes", 60)
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            minutes = 60
        result["minutes"] = max(5, min(minutes, 60 * 24 * 30))
    elif kind == "daily":
        times = _parse_times(data.get("times"))
        result["times"] = [f"{h:02d}:{m:02d}" for h, m in times] or ["08:00"]
    elif kind == "weekly":
        times = _parse_times(data.get("times"))
        weekdays = _parse_weekdays(data.get("weekdays"))
        result["times"] = [f"{h:02d}:{m:02d}" for h, m in times] or ["08:00"]
        result["weekdays"] = weekdays or [0]
    elif kind == "once":
        result["at"] = str(data.get("at") or "").strip()
    return result


def next_run_at(schedule: dict[str, Any], *, now_utc: datetime | None = None) -> datetime | None:
    """算下一次触发时间（naive UTC）；手动计划返回 None。"""
    kind = str(schedule.get("type") or "manual")
    current = _local_now(now_utc)

    if kind == "interval":
        minutes = int(schedule.get("minutes") or 60)
        return _to_utc_naive(current + timedelta(minutes=max(1, minutes)))

    if kind == "daily":
        times = _parse_times(schedule.get("times")) or [(8, 0)]
        for candidate in _daily_candidates(current, times):
            if candidate > current:
                return _to_utc_naive(candidate)
        return None

    if kind == "weekly":
        times = _parse_times(schedule.get("times")) or [(8, 0)]
        weekdays = _parse_weekdays(schedule.get("weekdays")) or [0]
        for offset in range(0, 8):
            day = current + timedelta(days=offset)
            if day.weekday() not in weekdays:
                continue
            for hour, minute in times:
                candidate = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if candidate > current:
                    return _to_utc_naive(candidate)
        return None

    if kind == "once":
        raw = str(schedule.get("at") or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        # 前端可能提交带时区的 ISO 串；不带时区时按本地时间理解
        return _to_utc_naive(_as_local(parsed))

    return None


def _daily_candidates(current: datetime, times: list[tuple[int, int]]) -> list[datetime]:
    """今天与明天的所有时间点，按先后排序。"""
    out: list[datetime] = []
    for day_offset in (0, 1):
        day = current + timedelta(days=day_offset)
        for hour, minute in times:
            out.append(day.replace(hour=hour, minute=minute, second=0, microsecond=0))
    return sorted(out)


def describe(schedule: dict[str, Any]) -> str:
    """把调度配置写成人看得懂的一句话（界面与日志共用）。"""
    kind = str(schedule.get("type") or "manual")
    if kind == "manual":
        return "仅手动运行"
    if kind == "interval":
        minutes = int(schedule.get("minutes") or 60)
        if minutes % 60 == 0:
            return f"每 {minutes // 60} 小时"
        return f"每 {minutes} 分钟"
    if kind == "daily":
        return "每天 " + "、".join(schedule.get("times") or ["08:00"])
    if kind == "weekly":
        days = "、".join(
            WEEKDAY_LABELS[value] for value in _parse_weekdays(schedule.get("weekdays"))
        ) or "周一"
        return f"{days} " + "、".join(schedule.get("times") or ["08:00"])
    if kind == "once":
        raw = str(schedule.get("at") or "").strip()
        if not raw:
            return "定时一次（未设置时间）"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return "定时一次"
        return "定时一次 · " + _as_local(parsed).strftime("%m-%d %H:%M")
    return "仅手动运行"


def is_due(plan, *, now_utc: datetime | None = None) -> bool:
    """计划是否到点（按 naive UTC 比较）。"""
    if not plan.enabled or plan.next_run_at is None:
        return False
    current = now_utc or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return plan.next_run_at <= current.replace(tzinfo=None)
