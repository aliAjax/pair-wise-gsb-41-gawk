"""案件处理时效与巨灾事件中止的纯计算逻辑。

本模块不依赖数据库和 HTTP，只做时间运算，方便单独测试：

- 案件受理（created_at）后 :data:`DEADLINE_DAYS` 天为原期限；
- 受灾事件与案件寿命相交的区间为暂停区间，事件进行中区间右端取当前时间；
- 多个事件区间先做并集合并，重叠/相邻部分只计一次，避免重复顺延；
- 当前期限 = 原期限 + 各有效暂停区间时长之和。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

DEADLINE_DAYS = 15


def parse_ts(value: Any) -> datetime:
    """把 ISO 字符串转成带时区的 datetime；裸时间按 UTC 处理。"""
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise DomainValueError("时间格式无效，应为 ISO 8601（如 2026-09-28T08:00:00）") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class DomainValueError(ValueError):
    """时间字段解析失败时抛出，便于接口层统一转成 400。"""


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def _label(event: Mapping[str, Any]) -> dict[str, Any]:
    return {"event_no": event.get("event_no"), "name": event.get("name")}


def build_windows(events: Iterable[Mapping[str, Any]], created_at: Any) -> list[dict[str, Any]]:
    """计算事件在案件受理时间轴上实际覆盖的暂停区间（已做并集合并）。

    事件早于受理时间的部分被裁掉；结束时间为空表示事件仍在进行（右端开放）。
    """
    accepted = parse_ts(created_at)
    raw: list[tuple[datetime, datetime | None, Mapping[str, Any]]] = []
    for event in events:
        start = max(parse_ts(event["started_at"]), accepted)
        end = parse_ts(event["ended_at"]) if event.get("ended_at") else None
        if end is not None and end <= start:
            continue
        raw.append((start, end, event))
    raw.sort(key=lambda item: item[0])

    windows: list[dict[str, Any]] = []
    for start, end, event in raw:
        if windows:
            last = windows[-1]
            last_end: datetime | None = last["end"]  # None 表示开放区间
            # 开放区间可以吸收之后的一切；闭合区间在相邻或重叠时合并
            if last_end is None or start <= last_end:
                last["events"].append(_label(event))
                if end is None:
                    last["end"] = None
                elif last["end"] is not None and end > last["end"]:
                    last["end"] = end
                continue
        windows.append({"start": start, "end": end, "events": [_label(event)]})
    return windows


def deadline_snapshot(created_at: Any, events: Iterable[Mapping[str, Any]],
                      now: Any = None, terminal: bool = False) -> dict[str, Any]:
    """返回案件时效快照：原期限、暂停区间、累计暂停秒数与当前期限。

    停表算法按时间顺序遍历合并后的暂停区间：区间开始时剩余时长尚未走完
    （``start < current``），才把整个区间长度顺延到当前期限上；事件开始时
    时钟已经走完的，不产生救济。进行中的事件右端按 ``now`` 临时收口，因此
    事件不解除，当前期限会随时间持续后移，案件不会被判逾期。
    """
    accepted = parse_ts(created_at)
    moment = parse_ts(now) if now else datetime.now(timezone.utc)
    original = accepted + timedelta(days=DEADLINE_DAYS)
    current = original
    windows_out: list[dict[str, Any]] = []
    paused_seconds = 0.0
    suspended = False

    # 停表算法按时间顺序遍历合并后的暂停区间，逐段处理：
    # 1) 区间尚未开始（start >= now）：当前不产生顺延，以后发生时再生效；
    # 2) 区间开始时剩余时长已走完（start >= current）：不产生救济；
    # 3) 否则把区间内已经历的时长顺延到当前期限上。区间在期限到达后才结束时，
    #    期限会被顶到区间结束点，因此之后开始的重叠事件仍能继续顺延（级联）。
    # 进行中的事件右端按 now 临时收口：事件不解除，期限随时间持续后移，不判逾期。
    for window in build_windows(events, accepted):
        start, end = window["start"], window["end"]
        effective_end = end if end is not None else moment
        if effective_end <= start or start >= moment:
            continue
        if start >= current:
            continue
        seconds = (effective_end - start).total_seconds()
        current += timedelta(seconds=seconds)
        paused_seconds += seconds
        ongoing = end is None
        if ongoing and moment >= start:
            suspended = True
        windows_out.append({
            "start": iso(start),
            "end": iso(end),
            "ongoing": ongoing,
            "paused_seconds": int(seconds),
            "events": window["events"],
        })

    return {
        "accepted_at": iso(accepted),
        "deadline_days": DEADLINE_DAYS,
        "original_deadline": iso(original),
        "current_deadline": iso(current),
        "paused_seconds": int(paused_seconds),
        "suspended": suspended,
        "overdue": bool(not terminal and moment > current),
        "windows": windows_out,
    }
