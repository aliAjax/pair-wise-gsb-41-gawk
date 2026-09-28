"""事件时效中止计算层（纯函数，不读写数据库）。

规则：
- 案件受理（created_at）后 15 天为原期限；
- 案件区域命中灾害事件、且事件时间与案件有效期相交时，时钟在相交区间内停表；
- 解除事件时按实际暂停时长顺延当前期限；
- 多个重叠事件的暂停区间先取并集再计时，重叠天数不重复增加。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

# 案件终态：进入终态后不再计算时效
TERMINAL = {"duplicate", "approved", "rejected", "closed"}

PROCESSING_DAYS = 15


def parse_ts(value: str | None) -> datetime | None:
    """解析数据库中的 ISO8601 时间字符串，无时区信息按 UTC 处理。"""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def parse_input_ts(value: Any, label: str) -> datetime:
    """解析接口传入的时间，允许 ISO8601（带或不带时区）。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s不能为空" % label)
    try:
        dt = parse_ts(value)
    except ValueError as exc:
        raise ValueError("%s不是有效时间" % label) from exc
    if dt is None:
        raise ValueError("%s不是有效时间" % label)
    return dt


def original_deadline(created_at: str) -> datetime:
    """受理后 15 天为原期限。"""
    created = parse_ts(created_at)
    if created is None:
        raise ValueError("受理时间无效")
    return created + timedelta(days=PROCESSING_DAYS)


def merge_intervals(intervals: Iterable[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """合并相交/相邻的时间区间，重叠部分只保留一份（去重的核心）。"""
    ordered = sorted((a, b) for a, b in intervals if b > a)
    merged: list[tuple[datetime, datetime]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def claim_pause_intervals(claim: Any, events: Iterable[Any], now: datetime | None = None) -> list[tuple[datetime, datetime]]:
    """计算单个案件实际命中的暂停区间。

    - 只统计与案件区域相同的事件；
    - 事件区间与 [受理时间, 结案时间/当前时间] 取交集；
    - 多个事件的交集再合并去重。
    """
    now = now or datetime.now(timezone.utc)
    created = parse_ts(claim["created_at"])
    if created is None:
        return []
    effective_end = now
    if claim["status"] in TERMINAL:
        # 终态案件只统计受理到结案之间的暂停时长
        boundary = parse_ts(claim["updated_at"])
        if boundary is not None:
            effective_end = boundary
    spans: list[tuple[datetime, datetime]] = []
    for ev in events:
        start = parse_ts(ev["started_at"])
        if start is None:
            continue
        # 未解除的事件暂停到当前时刻
        end = parse_ts(ev["ended_at"]) if ev["ended_at"] else now
        lo = max(start, created)
        hi = min(end, effective_end)
        if hi > lo:
            spans.append((lo, hi))
    return merge_intervals(spans)


def _segment_dict(start: datetime, end: datetime) -> dict[str, str]:
    return {"start": to_iso(start), "end": to_iso(end)}


def deadline_view(claim: Any, events: Iterable[Any], now: datetime | None = None) -> dict[str, Any]:
    """组装对外展示的时效视图：原期限、暂停区间、暂停时长、当前期限、是否逾期/停表。"""
    now = now or datetime.now(timezone.utc)
    events = list(events)
    original = original_deadline(claim["created_at"])
    created_ts = parse_ts(claim["created_at"])
    intervals = claim_pause_intervals(claim, events, now)
    paused = sum((end - start for start, end in intervals), timedelta())
    current = original + paused
    is_terminal = claim["status"] in TERMINAL
    is_suspended = claim["status"] == "suspended"

    # 已生效但未解除的事件（停表中）
    active_starts = [
        parse_ts(ev["started_at"])
        for ev in events
        if not ev["ended_at"] and parse_ts(ev["started_at"]) is not None and parse_ts(ev["started_at"]) <= now
    ]
    clock_stopped = bool(active_starts) or is_suspended

    if is_terminal:
        overdue = False
    elif clock_stopped and active_starts and created_ts is not None:
        # 停表期间逾期判定冻结在停表开始时刻：停表前已逾期的保持逾期，不被事件洗白
        freeze_at = min(active_starts)
        prior_paused = timedelta()
        for lo, hi in intervals:
            prior_paused += max(timedelta(), min(hi, freeze_at) - lo)
        overdue = freeze_at > original + prior_paused
    else:
        overdue = now > current
    return {
        "original_deadline": to_iso(original),
        "pause_segments": [_segment_dict(a, b) for a, b in intervals],
        "paused_days": round(paused.total_seconds() / 86400, 3),
        "paused_seconds": int(paused.total_seconds()),
        "current_deadline": to_iso(current),
        "suspended": is_suspended,
        "clock_stopped": bool(clock_stopped),
        "overdue": bool(overdue),
    }
