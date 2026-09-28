"""Catastrophe insurance claim triage and settlement service."""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import suspension
from suspension import DomainValueError, deadline_snapshot, iso, parse_ts

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "catastrophe_claims.db"
TERMINAL = {"duplicate", "approved", "rejected", "closed"}
VIEWER_ROLES = {"intake", "supervisor", "adjuster", "surveyor", "auditor"}
TRANSITIONS = {
    "received": {"triaged"},
    "triaged": {"assigned", "escalated"},
    "assigned": {"survey", "escalated"},
    "survey": {"review", "escalated"},
    "review": {"approved", "rejected", "escalated"},
    "escalated": {"assigned", "review", "rejected"},
    # 已结案件经主管重开后回到办理轨道，时效按原受理时间继续计算
    "approved": {"reopened"},
    "rejected": {"reopened"},
    "closed": {"reopened"},
    "reopened": {"assigned", "survey", "review", "escalated"},
}


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def require_role(role: str, allowed: set[str], action: str) -> None:
    if role not in allowed:
        raise DomainError("角色无权执行：%s" % action, 403)


def actor_id(actor: str) -> str:
    actor = (actor or "").strip()
    if not actor:
        raise DomainError("缺少操作人")
    return actor


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


def coordinate(value: Any, label: str, low: float, high: float) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise DomainError("%s必须是数值" % label) from exc
    if not low <= value <= high:
        raise DomainError("%s超出有效范围" % label)
    return value


class CatastropheClaimService:
    def __init__(self, db_path: str | os.PathLike[str] = DEFAULT_DB):
        self.db_path = str(db_path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_no TEXT NOT NULL UNIQUE,
                    event_id TEXT NOT NULL,
                    region TEXT NOT NULL,
                    peril_type TEXT NOT NULL,
                    policy_no TEXT NOT NULL,
                    claimant_ref TEXT NOT NULL,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    estimated_loss REAL NOT NULL,
                    urgent_need INTEGER NOT NULL DEFAULT 0,
                    fraud_score REAL NOT NULL DEFAULT 0,
                    priority_score REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'received',
                    assignee TEXT,
                    surveyor TEXT,
                    lodging_required INTEGER NOT NULL DEFAULT 0,
                    remote_review INTEGER NOT NULL DEFAULT 0,
                    emergency_advance REAL NOT NULL DEFAULT 0,
                    final_payout REAL,
                    duplicate_of INTEGER REFERENCES claims(id),
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    sha256 TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    source TEXT NOT NULL,
                    submitter TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'received',
                    created_at TEXT NOT NULL,
                    UNIQUE(claim_id,sha256)
                );
                CREATE TABLE IF NOT EXISTS survey_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    surveyor TEXT NOT NULL,
                    damage_ratio REAL NOT NULL,
                    findings TEXT NOT NULL,
                    recommendation TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    kind TEXT NOT NULL,
                    amount REAL NOT NULL,
                    approved_by TEXT NOT NULL,
                    reference TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS timeline (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER REFERENCES claims(id),
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS suspension_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_no TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    regions TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_claims_queue ON claims(status, priority_score DESC, created_at);
                CREATE INDEX IF NOT EXISTS idx_evidence_hash ON evidence(sha256);
                CREATE INDEX IF NOT EXISTS idx_suspension_open ON suspension_events(ended_at, started_at);
                """
            )

    def _audit(self, conn: sqlite3.Connection, claim_id: int | None, actor: str, action: str, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO timeline(claim_id,actor,action,details,created_at) VALUES(?,?,?,?,?)",
            (claim_id, actor, action, json.dumps(details, ensure_ascii=False, sort_keys=True), utcnow()),
        )

    def _claim(self, conn: sqlite3.Connection, claim_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        if not row:
            raise DomainError("理赔案件不存在", 404)
        return row

    # ---- 事件时效中止 ----

    @staticmethod
    def _event_regions(value: Any) -> list[str]:
        if isinstance(value, str):
            items = [value]
        elif isinstance(value, (list, tuple)):
            items = list(value)
        else:
            raise DomainError("受灾区域必须是字符串或字符串数组")
        regions: list[str] = []
        for item in items:
            text = str(item or "").strip()
            if text and text not in regions:
                regions.append(text)
        if not regions:
            raise DomainError("受灾区域不能为空")
        return regions

    @staticmethod
    def _region_match(region: str, regions: list[str]) -> bool:
        # 精确匹配为主，同时允许前缀式的辖区包含（如“宁波市”覆盖“宁波市海曙区”）
        target = region.strip()
        return any(target == item or target.startswith(item) for item in regions)

    def _load_events(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute("SELECT * FROM suspension_events ORDER BY started_at,id").fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            event = dict(row)
            event["regions"] = json.loads(row["regions"])
            events.append(event)
        return events

    def _events_for_claim(self, events: list[dict[str, Any]], region: str) -> list[dict[str, Any]]:
        return [e for e in events if self._region_match(region, e["regions"])]

    def _with_deadline(self, conn: sqlite3.Connection, claim: sqlite3.Row | dict[str, Any],
                       events: list[dict[str, Any]] | None = None, now: datetime | None = None) -> dict[str, Any]:
        """在案件数据上补充原期限、暂停区间与当前期限。"""
        data = dict(claim)
        if events is None:
            events = self._load_events(conn)
        relevant = self._events_for_claim(events, data["region"])
        moment = iso(now) if now else None
        data["deadline"] = deadline_snapshot(
            data["created_at"], relevant, moment, terminal=data["status"] in TERMINAL
        )
        return data

    def register_suspension_event(self, actor: str, role: str, event_no: str, name: str,
                                  kind: str, regions: Any, started_at: str,
                                  ended_at: str | None = None) -> dict[str, Any]:
        """主管登记灾害事件：受灾区域内案件自开始时间起停表。"""
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "登记时效中止事件")
        event_no = (event_no or "").strip()
        name = (name or "").strip()
        kind = (kind or "").strip()
        if not event_no or not name or not kind:
            raise DomainError("事件编号、名称和类型不能为空")
        region_list = self._event_regions(regions)
        start = parse_ts(started_at)
        end = parse_ts(ended_at) if ended_at else None
        now = datetime.now(timezone.utc)
        if start > now:
            raise DomainError("事件开始时间不能晚于当前时间")
        if end is not None and end <= start:
            raise DomainError("事件结束时间必须晚于开始时间")
        if end is not None and end > now:
            raise DomainError("事件结束时间不能晚于当前时间")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    """INSERT INTO suspension_events(event_no,name,kind,regions,started_at,ended_at,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (event_no, name, kind, json.dumps(region_list, ensure_ascii=False),
                     iso(start), iso(end) if end else None, actor, utcnow(), utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("事件编号已存在", 409) from exc
            event_id = cur.lastrowid
            affected_count = self._count_affected(conn, region_list)
            self._audit(
                conn, None, actor, "suspension.registered",
                {"event_no": event_no, "name": name, "kind": kind, "regions": region_list,
                 "started_at": iso(start), "ended_at": iso(end) if end else None,
                 "in_scope_claims": affected_count},
            )
            row = conn.execute("SELECT * FROM suspension_events WHERE id=?", (event_id,)).fetchone()
            event = dict(row)
            event["regions"] = region_list
            event["affected_claims"] = affected_count
            return event

    def _count_affected(self, conn: sqlite3.Connection, regions: list[str]) -> int:
        rows = conn.execute("SELECT region FROM claims WHERE status<>'duplicate'").fetchall()
        return sum(1 for row in rows if self._region_match(row["region"], regions))

    def lift_suspension_event(self, actor: str, role: str, event_no: str,
                              ended_at: str | None = None) -> dict[str, Any]:
        """主管解除事件：按实际暂停时长顺延，重叠区间由并集算法自动去重。"""
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "解除时效中止事件")
        end = parse_ts(ended_at) if ended_at else datetime.now(timezone.utc)
        now = datetime.now(timezone.utc)
        if end > now:
            raise DomainError("解除时间不能晚于当前时间")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM suspension_events WHERE event_no=?", ((event_no or "").strip(),)).fetchone()
            if not row:
                raise DomainError("中止事件不存在", 404)
            if row["ended_at"]:
                raise DomainError("该事件已解除，不能重复解除", 409)
            start = parse_ts(row["started_at"])
            if end <= start:
                raise DomainError("解除时间必须晚于事件开始时间")
            conn.execute(
                "UPDATE suspension_events SET ended_at=?,updated_at=? WHERE id=?",
                (iso(end), utcnow(), row["id"]),
            )
            regions = json.loads(row["regions"])
            events = self._load_events(conn)
            affected_rows = conn.execute(
                "SELECT * FROM claims WHERE status NOT IN ('duplicate')"
            ).fetchall()
            affected = []
            for claim in affected_rows:
                if not self._region_match(claim["region"], regions):
                    continue
                detail = self._with_deadline(conn, claim, events)
                affected.append({
                    "claim_id": claim["id"], "claim_no": claim["claim_no"],
                    "original_deadline": detail["deadline"]["original_deadline"],
                    "current_deadline": detail["deadline"]["current_deadline"],
                    "paused_seconds": detail["deadline"]["paused_seconds"],
                })
            self._audit(
                conn, None, actor, "suspension.lifted",
                {"event_no": row["event_no"], "started_at": row["started_at"],
                 "ended_at": iso(end), "affected_claims": affected},
            )
            new_row = conn.execute("SELECT * FROM suspension_events WHERE id=?", (row["id"],)).fetchone()
            event = dict(new_row)
            event["regions"] = regions
            event["affected_claims"] = affected
            return event

    def list_suspension_events(self, role: str = "viewer") -> list[dict[str, Any]]:
        if role not in VIEWER_ROLES:
            raise DomainError("角色无权查看中止事件", 403)
        with self.connect() as conn:
            events = self._load_events(conn)
        for event in events:
            event["active"] = event["ended_at"] is None
        return events

    def reopen_claim(self, actor: str, role: str, claim_id: int, reason: str) -> dict[str, Any]:
        """主管重开已结案件，受理时间不变，已累积的暂停顺延继续生效。"""
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "重开案件")
        if not reason or not reason.strip():
            raise DomainError("重开原因不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] not in {"approved", "rejected", "closed"}:
                raise DomainError("只有已结束案件可以重开", 409)
            conn.execute(
                "UPDATE claims SET status='reopened',version=version+1,updated_at=? WHERE id=?",
                (utcnow(), claim_id),
            )
            self._audit(conn, claim_id, actor, "claim.reopened", {"reason": reason.strip()})
            return self._with_deadline(conn, self._claim(conn, claim_id))

    def claim_detail(self, actor: str, role: str, claim_id: int) -> dict[str, Any]:
        if role not in VIEWER_ROLES:
            raise DomainError("角色无权查看案件详情", 403)
        try:
            claim_id = int(claim_id)
        except (TypeError, ValueError) as exc:
            raise DomainError("案件编号必须是整数") from exc
        with self.connect() as conn:
            claim = self._claim(conn, claim_id)
            if role in {"adjuster", "surveyor"} and actor not in (claim["assignee"], claim["surveyor"]):
                raise DomainError("只能查看分配给自己的案件", 403)
            events = self._load_events(conn)
            detail = self._with_deadline(conn, claim, events)
            evidence = [dict(r) for r in conn.execute(
                "SELECT * FROM evidence WHERE claim_id=? ORDER BY id", (claim_id,)
            ).fetchall()]
            surveys = [dict(r) for r in conn.execute(
                "SELECT * FROM survey_notes WHERE claim_id=? ORDER BY id", (claim_id,)
            ).fetchall()]
            payments = [dict(r) for r in conn.execute(
                "SELECT * FROM payments WHERE claim_id=? ORDER BY id", (claim_id,)
            ).fetchall()]
            timeline = [dict(r) for r in conn.execute(
                "SELECT * FROM timeline WHERE claim_id=? ORDER BY id", (claim_id,)
            ).fetchall()]
            related = [
                {"event_no": e["event_no"], "name": e["name"], "kind": e["kind"],
                 "regions": e["regions"], "started_at": e["started_at"],
                 "ended_at": e["ended_at"], "active": e["ended_at"] is None}
                for e in self._events_for_claim(events, claim["region"])
            ]
        detail["evidence"] = evidence
        detail["survey_notes"] = surveys
        detail["payments"] = payments
        detail["timeline"] = timeline
        detail["suspension_events"] = related
        return detail

    def create_claim(self, actor: str, role: str, claim_no: str, event_id: str,
                     region: str, peril_type: str, policy_no: str, claimant_ref: str,
                     latitude: float, longitude: float, estimated_loss: float,
                     urgent_need: bool = False, lodging_required: bool = False) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"intake", "supervisor"}, "创建报案")
        values = [claim_no, event_id, region, peril_type, policy_no, claimant_ref]
        if not all(str(v).strip() for v in values):
            raise DomainError("案件必需字段不能为空")
        lat = coordinate(latitude, "纬度", -90, 90)
        lon = coordinate(longitude, "经度", -180, 180)
        try:
            estimated_loss = float(estimated_loss)
        except (TypeError, ValueError) as exc:
            raise DomainError("预估损失必须是数值") from exc
        if estimated_loss < 0:
            raise DomainError("预估损失不能为负数")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = utcnow()
            duplicate_of = None
            candidates = conn.execute(
                """SELECT * FROM claims WHERE event_id=? AND policy_no=? AND status<>'duplicate'
                   ORDER BY id DESC LIMIT 50""",
                (event_id.strip(), policy_no.strip()),
            ).fetchall()
            for row in candidates:
                within_time = abs((datetime.fromisoformat(now) - datetime.fromisoformat(row["created_at"])).total_seconds()) <= 172800
                loss_close = abs(row["estimated_loss"] - estimated_loss) <= max(1000.0, row["estimated_loss"] * 0.1)
                if within_time and loss_close and haversine_km(lat, lon, row["latitude"], row["longitude"]) <= 3.0:
                    duplicate_of = row["id"]
                    break
            status = "duplicate" if duplicate_of else "received"
            try:
                cur = conn.execute(
                    """INSERT INTO claims(claim_no,event_id,region,peril_type,policy_no,claimant_ref,latitude,longitude,
                       estimated_loss,urgent_need,lodging_required,status,duplicate_of,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (claim_no.strip(), event_id.strip(), region.strip(), peril_type.strip(), policy_no.strip(),
                     claimant_ref.strip(), lat, lon, estimated_loss, int(bool(urgent_need)), int(bool(lodging_required)),
                     status, duplicate_of, actor, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("报案编号已存在", 409) from exc
            self._audit(conn, cur.lastrowid, actor, "claim.created", {"duplicate_of": duplicate_of})
            if duplicate_of:
                self._audit(conn, duplicate_of, actor, "claim.duplicate_detected", {"new_claim": claim_no.strip()})
            return dict(self._claim(conn, cur.lastrowid))

    def triage_claim(self, actor: str, role: str, claim_id: int, expected_version: int,
                     fraud_score: float = 0.0, remote_review: bool = False) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "案件分级")
        try:
            fraud_score = float(fraud_score)
        except (TypeError, ValueError) as exc:
            raise DomainError("欺诈评分必须是数值") from exc
        if not 0 <= fraud_score <= 1:
            raise DomainError("欺诈评分应在 0 到 1 之间")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] != "received":
                raise DomainError("只有待分级案件可以分级", 409)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            priority = min(100.0, claim["estimated_loss"] / 100000.0 * 25 + (40 if claim["urgent_need"] else 0) + fraud_score * 20 + (10 if claim["lodging_required"] else 0))
            new_status = "escalated" if fraud_score >= 0.8 else "triaged"
            conn.execute(
                "UPDATE claims SET fraud_score=?,priority_score=?,remote_review=?,status=?,version=version+1,updated_at=? WHERE id=?",
                (fraud_score, priority, int(bool(remote_review)), new_status, utcnow(), claim_id),
            )
            self._audit(conn, claim_id, actor, "claim.triaged", {"priority": priority, "status": new_status})
            return dict(self._claim(conn, claim_id))

    def assign_claim(self, actor: str, role: str, claim_id: int, assignee: str,
                     expected_version: int, surveyor: str | None = None) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "分配案件")
        assignee = assignee.strip()
        if not assignee:
            raise DomainError("查勘负责人不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] not in {"triaged", "escalated", "assigned", "reopened"}:
                raise DomainError("当前状态不能分配", 409)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            conn.execute(
                """UPDATE claims SET assignee=?,surveyor=?,status='assigned',version=version+1,updated_at=?
                   WHERE id=? AND version=?""",
                (assignee, surveyor.strip() if surveyor else None, utcnow(), claim_id, expected_version),
            )
            self._audit(conn, claim_id, actor, "claim.assigned", {"assignee": assignee, "surveyor": surveyor})
            return dict(self._claim(conn, claim_id))

    def add_evidence(self, actor: str, role: str, claim_id: int, sha256: str,
                     filename: str, source: str) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"intake", "adjuster", "surveyor", "supervisor"}, "添加损失证据")
        sha256 = sha256.strip().lower()
        if len(sha256) != 64 or any(ch not in "0123456789abcdef" for ch in sha256):
            raise DomainError("证据哈希必须是 64 位 SHA-256 十六进制")
        if not filename.strip() or not source.strip():
            raise DomainError("证据文件名和来源不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] in TERMINAL:
                raise DomainError("已结束案件不能添加证据", 409)
            existing = conn.execute("SELECT * FROM evidence WHERE claim_id=? AND sha256=?", (claim_id, sha256)).fetchone()
            if existing:
                return dict(existing)
            cur = conn.execute(
                "INSERT INTO evidence(claim_id,sha256,filename,source,submitter,created_at) VALUES(?,?,?,?,?,?)",
                (claim_id, sha256, filename.strip(), source.strip(), actor, utcnow()),
            )
            hash_claims = [r["claim_id"] for r in conn.execute(
                "SELECT DISTINCT claim_id FROM evidence WHERE sha256=?", (sha256,)
            ).fetchall()]
            suspicious = len(hash_claims) >= 3
            if suspicious:
                for cid in hash_claims:
                    conn.execute(
                        "UPDATE claims SET fraud_score=MAX(fraud_score,0.95),status='escalated',version=version+1,updated_at=? WHERE id=? AND status<>'duplicate'",
                        (utcnow(), cid),
                    )
                self._audit(conn, claim_id, actor, "evidence.bulk_reuse_detected", {"sha256": sha256, "claim_ids": hash_claims})
            self._audit(conn, claim_id, actor, "evidence.added", {"evidence_id": cur.lastrowid, "suspicious": suspicious})
            return {"evidence": dict(conn.execute("SELECT * FROM evidence WHERE id=?", (cur.lastrowid,)).fetchone()), "bulk_reuse": suspicious, "affected_claims": hash_claims}

    def record_survey(self, actor: str, role: str, claim_id: int, damage_ratio: float,
                      findings: str, recommendation: str, expected_version: int) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"adjuster", "surveyor"}, "录入查勘结果")
        try:
            damage_ratio = float(damage_ratio)
        except (TypeError, ValueError) as exc:
            raise DomainError("损失比例必须是数值") from exc
        if not 0 <= damage_ratio <= 1 or not findings.strip() or not recommendation.strip():
            raise DomainError("损失比例或查勘内容无效")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] not in {"assigned", "escalated", "reopened"}:
                raise DomainError("当前状态不能录入查勘", 409)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            if claim["assignee"] != actor and claim["surveyor"] != actor:
                raise DomainError("只有被分配的查勘人员可以录入结果", 403)
            if claim["status"] == "escalated" and claim["fraud_score"] >= 0.8:
                raise DomainError("高风险案件须先完成复核降险，不能直接提交查勘", 409)
            conn.execute(
                "INSERT INTO survey_notes(claim_id,surveyor,damage_ratio,findings,recommendation,created_at) VALUES(?,?,?,?,?,?)",
                (claim_id, actor, damage_ratio, findings.strip(), recommendation.strip(), utcnow()),
            )
            conn.execute("UPDATE claims SET status='survey',version=version+1,updated_at=? WHERE id=?", (utcnow(), claim_id))
            self._audit(conn, claim_id, actor, "survey.recorded", {"damage_ratio": damage_ratio, "recommendation": recommendation})
            return dict(self._claim(conn, claim_id))

    def submit_review(self, actor: str, role: str, claim_id: int, expected_version: int) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"adjuster", "surveyor"}, "提交核损")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] not in {"survey", "reopened"}:
                raise DomainError("只有已查勘案件可以提交核损", 409)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            if not conn.execute("SELECT 1 FROM survey_notes WHERE claim_id=?", (claim_id,)).fetchone():
                raise DomainError("缺少查勘记录", 409)
            conn.execute("UPDATE claims SET status='review',version=version+1,updated_at=? WHERE id=?", (utcnow(), claim_id))
            self._audit(conn, claim_id, actor, "claim.review_submitted", {"reopened": claim["status"] == "reopened"})
            return dict(self._claim(conn, claim_id))

    def emergency_advance(self, actor: str, role: str, claim_id: int, amount: float,
                          expected_version: int, reference: str) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "批准紧急预付")
        try:
            amount = float(amount)
        except (TypeError, ValueError) as exc:
            raise DomainError("预付金额必须是数值") from exc
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            if not claim["urgent_need"]:
                raise DomainError("非紧急案件不能预付", 409)
            if claim["status"] in {"duplicate", "approved", "rejected", "closed"}:
                raise DomainError("当前案件状态不能预付", 409)
            if claim["fraud_score"] >= 0.8:
                raise DomainError("高风险案件不能预付", 409)
            limit = claim["estimated_loss"] * 0.2
            if amount <= 0 or amount > limit:
                raise DomainError("预付金额必须大于0且不超过预估损失的20%", 409)
            if claim["emergency_advance"] + amount > limit:
                raise DomainError("累计预付超过上限", 409)
            try:
                conn.execute(
                    "INSERT INTO payments(claim_id,kind,amount,approved_by,reference,created_at) VALUES(?,?,?,?,?,?)",
                    (claim_id, "emergency_advance", amount, actor, reference.strip(), utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("付款参考号已存在", 409) from exc
            conn.execute("UPDATE claims SET emergency_advance=emergency_advance+?,version=version+1,updated_at=? WHERE id=?", (amount, utcnow(), claim_id))
            self._audit(conn, claim_id, actor, "payment.emergency_advance", {"amount": amount, "reference": reference})
            return dict(self._claim(conn, claim_id))

    def finalize_claim(self, actor: str, role: str, claim_id: int, decision: str,
                       payout: float, expected_version: int, reason: str = "") -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "最终核定")
        if decision not in {"approve", "reject"}:
            raise DomainError("核定决定无效")
        try:
            payout = float(payout)
        except (TypeError, ValueError) as exc:
            raise DomainError("核定金额必须是数值") from exc
        if payout < 0:
            raise DomainError("核定金额不能为负数")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] != "review":
                raise DomainError("只有待复核案件可以最终核定", 409)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            if claim["duplicate_of"]:
                raise DomainError("重复报案不能核定赔付", 409)
            if claim["fraud_score"] >= 0.8 and decision == "approve":
                raise DomainError("高风险案件未解除风险标记，不能赔付", 409)
            if decision == "approve" and payout > claim["estimated_loss"]:
                raise DomainError("核定金额不能超过预估损失", 409)
            if decision == "reject" and not reason.strip():
                raise DomainError("拒赔必须填写理由", 409)
            status = "approved" if decision == "approve" else "rejected"
            conn.execute(
                "UPDATE claims SET status=?,final_payout=?,version=version+1,updated_at=? WHERE id=? AND version=?",
                (status, payout if decision == "approve" else 0, utcnow(), claim_id, expected_version),
            )
            self._audit(conn, claim_id, actor, "claim.finalized", {"decision": decision, "payout": payout, "reason": reason.strip()})
            return dict(self._claim(conn, claim_id))

    def queue(self, role: str = "viewer", actor: str = "") -> list[dict[str, Any]]:
        if role not in VIEWER_ROLES:
            raise DomainError("角色无权查看理赔队列", 403)
        with self.connect() as conn:
            events = self._load_events(conn)
            if role in {"adjuster", "surveyor"}:
                rows = conn.execute(
                    "SELECT * FROM claims WHERE (assignee=? OR surveyor=?) AND status NOT IN ('approved','rejected','duplicate') ORDER BY priority_score DESC,created_at",
                    (actor, actor),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM claims ORDER BY priority_score DESC,created_at").fetchall()
            # 停表中的案件不占逾期压力，其余按当前期限先后排序
            result = [self._with_deadline(conn, r, events) for r in rows]
        result.sort(key=lambda c: (c["deadline"]["suspended"], c["deadline"]["current_deadline"]))
        return result

    def state(self, actor: str = "", role: str = "viewer") -> dict[str, Any]:
        if role not in VIEWER_ROLES:
            return {"claims": [], "evidence": [], "payments": [], "timeline": [], "events": [], "access_limited": True}
        with self.connect() as conn:
            all_events = self._load_events(conn)
            if role in {"adjuster", "surveyor"}:
                rows = conn.execute(
                    "SELECT * FROM claims WHERE assignee=? OR surveyor=? ORDER BY priority_score DESC,id DESC", (actor, actor)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM claims ORDER BY priority_score DESC,id DESC").fetchall()
            claims = [self._with_deadline(conn, r, all_events) for r in rows]
            ids = [c["id"] for c in claims]
            if ids:
                marks = ",".join("?" for _ in ids)
                evidence = [dict(r) for r in conn.execute("SELECT * FROM evidence WHERE claim_id IN (%s) ORDER BY id DESC" % marks, ids).fetchall()]
                payments = [dict(r) for r in conn.execute("SELECT * FROM payments WHERE claim_id IN (%s) ORDER BY id DESC" % marks, ids).fetchall()]
                timeline = [dict(r) for r in conn.execute("SELECT * FROM timeline WHERE claim_id IN (%s) ORDER BY id DESC LIMIT 300" % marks, ids).fetchall()]
            else:
                evidence, payments, timeline = [], [], []
            global_events = [dict(r) for r in conn.execute(
                "SELECT * FROM timeline WHERE action LIKE 'suspension.%' ORDER BY id DESC LIMIT 100"
            ).fetchall()]
            timeline = global_events + timeline
            events = []
            for event in all_events:
                item = dict(event)
                item["active"] = item["ended_at"] is None
                events.append(item)
        return {"claims": claims, "evidence": evidence, "payments": payments,
                "timeline": timeline, "events": events, "access_limited": False}

    def seed_demo(self) -> dict[str, Any]:
        with self.connect() as conn:
            if conn.execute("SELECT COUNT(*) AS c FROM claims").fetchone()["c"]:
                return {"seeded": False, "reason": "已有数据"}
        c1 = self.create_claim("intake-demo", "intake", "CLM-DEMO-001", "TY2026", "沿海A区", "洪水", "P-1001", "R-01", 30.1, 121.2, 500000, True, True)
        self.create_claim("intake-demo", "intake", "CLM-DEMO-002", "TY2026", "沿海A区", "洪水", "P-1002", "R-02", 30.2, 121.3, 240000, False, False)
        return {"seeded": True, "first_claim_id": c1["id"]}


class ApiHandler(BaseHTTPRequestHandler):
    service: CatastropheClaimService

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _headers(self) -> tuple[str, str]:
        return self.headers.get("X-User", ""), self.headers.get("X-Role", "viewer")

    def _json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise DomainError("请求体过大", 413)
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DomainError("请求体不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise DomainError("JSON 请求体必须是对象")
        return value

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path in {"/", "/index.html"}:
                body = (ROOT / "static" / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/health":
                self._send(200, {"status": "ok", "service": "catastrophe-claims"})
            elif path == "/api/state":
                self._send(200, self.service.state(*self._headers()))
            elif path == "/api/queue":
                actor, role = self._headers()
                self._send(200, {"queue": self.service.queue(role, actor)})
            elif path == "/api/events":
                _, role = self._headers()
                self._send(200, {"events": self.service.list_suspension_events(role)})
            elif path.startswith("/api/claims/") and path.endswith("/detail"):
                actor, role = self._headers()
                claim_id = path[len("/api/claims/"):-len("/detail")]
                self._send(200, self.service.claim_detail(actor, role, claim_id))
            else:
                self._send(404, {"error": "接口不存在"})
        except DomainError as exc:
            self._send(exc.status, {"error": str(exc)})
        except DomainValueError as exc:
            self._send(400, {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            path, data, (actor, role) = urlparse(self.path).path, self._json(), self._headers()
            if path == "/api/claims":
                result = self.service.create_claim(actor, role, **data)
            elif path == "/api/claims/triage":
                result = self.service.triage_claim(actor, role, **data)
            elif path == "/api/claims/assign":
                result = self.service.assign_claim(actor, role, **data)
            elif path == "/api/evidence":
                result = self.service.add_evidence(actor, role, **data)
            elif path == "/api/claims/survey":
                result = self.service.record_survey(actor, role, **data)
            elif path == "/api/claims/submit-review":
                result = self.service.submit_review(actor, role, **data)
            elif path == "/api/claims/emergency-advance":
                result = self.service.emergency_advance(actor, role, **data)
            elif path == "/api/claims/finalize":
                result = self.service.finalize_claim(actor, role, **data)
            elif path == "/api/events/suspensions":
                result = self.service.register_suspension_event(actor, role, **data)
            elif path == "/api/events/lift":
                result = self.service.lift_suspension_event(actor, role, **data)
            elif path == "/api/claims/reopen":
                result = self.service.reopen_claim(actor, role, **data)
            else:
                raise DomainError("接口不存在", 404)
            self._send(201, result)
        except DomainError as exc:
            self._send(exc.status, {"error": str(exc)})
        except DomainValueError as exc:
            self._send(400, {"error": str(exc)})
        except (KeyError, TypeError, ValueError) as exc:
            self._send(400, {"error": "请求参数错误: %s" % exc})
        except Exception as exc:
            self._send(500, {"error": "服务器内部错误", "detail": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def serve(service: CatastropheClaimService, host: str, port: int) -> None:
    ApiHandler.service = service
    server = ThreadingHTTPServer((host, port), ApiHandler)
    print("Catastrophe claim service listening on http://%s:%s" % (host, port))
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="巨灾保险理赔调度服务")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8207)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    service = CatastropheClaimService(args.db)
    if args.init:
        print(json.dumps(service.seed_demo() if args.seed else {"initialized": True, "db": args.db}, ensure_ascii=False))
        return
    serve(service, args.host, args.port)


if __name__ == "__main__":
    main()
