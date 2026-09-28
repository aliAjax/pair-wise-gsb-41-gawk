"""Catastrophe insurance claim triage and settlement service."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import clocks
from storage import ClaimStore, DEFAULT_DB

ROOT = Path(__file__).resolve().parent
TERMINAL = clocks.TERMINAL
EVENT_PERILS = {"typhoon", "earthquake", "flood", "other"}
TRANSITIONS = {
    "received": {"triaged"},
    "triaged": {"assigned", "escalated"},
    "assigned": {"survey", "escalated"},
    "survey": {"review", "escalated"},
    "review": {"approved", "rejected", "escalated"},
    "escalated": {"assigned", "review", "rejected"},
}


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def now_ts() -> datetime:
    return datetime.now(timezone.utc)


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
    def __init__(self, db_path: str = DEFAULT_DB):
        self.store = ClaimStore(db_path)
        self.db_path = str(db_path)

    def connect(self) -> sqlite3.Connection:
        return self.store.connect()

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

    def _guard_not_suspended(self, claim: sqlite3.Row) -> None:
        if claim["status"] == "suspended":
            raise DomainError("灾害事件时效中止期间案件暂停办理，解除或重开后可继续", 409)

    def _clock_for(self, conn: sqlite3.Connection, claim: Any) -> dict[str, Any]:
        events = [dict(e) for e in self.store.region_events(conn, claim["region"])]
        return clocks.deadline_view(dict(claim) if isinstance(claim, sqlite3.Row) else claim, events)

    def _enrich_clocks(self, conn: sqlite3.Connection, claims: list[sqlite3.Row]) -> list[dict[str, Any]]:
        events_by_region = {
            region: [dict(e) for e in self.store.region_events(conn, region)]
            for region in {c["region"] for c in claims}
        }
        enriched: list[dict[str, Any]] = []
        for row in claims:
            claim = dict(row)
            claim["clock"] = clocks.deadline_view(claim, events_by_region.get(claim["region"], []))
            enriched.append(claim)
        return enriched

    # ---------- 灾害事件：登记 / 解除 / 查询 ----------
    def register_event(self, actor: str, role: str, code: str, name: str, peril_type: str,
                       region: str, started_at: str, note: str = "") -> dict[str, Any]:
        """主管登记灾害事件，区域内在办案件立即挂起并停表。"""
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "登记灾害事件")
        code, name, region = (str(code or "").strip(), str(name or "").strip(), str(region or "").strip())
        if not code or not name or not region:
            raise DomainError("事件编号、名称和受灾区域不能为空")
        peril_type = str(peril_type or "").strip().lower()
        if peril_type not in EVENT_PERILS:
            raise DomainError("灾种必须是 typhoon/earthquake/flood/other")
        try:
            start = clocks.parse_input_ts(started_at, "开始时间")
        except ValueError as exc:
            raise DomainError(str(exc)) from exc
        if start > now_ts():
            raise DomainError("开始时间不能晚于当前时间")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if self.store.get_event_by_code(conn, code):
                raise DomainError("事件编号已存在", 409)
            event_id = self.store.insert_event(conn, code, name, peril_type, region,
                                               clocks.to_iso(start), actor, str(note or "").strip(), utcnow())
            self._audit(conn, None, actor, "event.registered",
                        {"event_id": event_id, "code": code, "region": region, "started_at": clocks.to_iso(start)})
            suspended: list[dict[str, Any]] = []
            for claim in self.store.suspendable_claims(conn, region):
                self.store.suspend_claim(conn, claim["id"], utcnow())
                self._audit(conn, claim["id"], actor, "claim.auto_suspended",
                            {"event_id": event_id, "code": code, "previous_status": claim["status"]})
                suspended.append({"claim_id": claim["id"], "claim_no": claim["claim_no"], "previous_status": claim["status"]})
            event = dict(self.store.get_event(conn, event_id))
            event["suspended_claims"] = suspended
            return event

    def lift_event(self, actor: str, role: str, event_id: int, ended_at: str | None = None) -> dict[str, Any]:
        """主管解除事件：按实际暂停时长顺延；区域无其他生效事件时重开案件。"""
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "解除灾害事件")
        try:
            event_id = int(event_id)
        except (TypeError, ValueError) as exc:
            raise DomainError("事件ID无效") from exc
        end = now_ts()
        if ended_at:
            try:
                end = clocks.parse_input_ts(ended_at, "解除时间")
            except ValueError as exc:
                raise DomainError(str(exc)) from exc
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            event = self.store.get_event(conn, event_id)
            if not event:
                raise DomainError("灾害事件不存在", 404)
            if event["status"] != "active":
                raise DomainError("事件已解除，不能重复操作", 409)
            start = clocks.parse_ts(event["started_at"])
            if start is not None and end < start:
                raise DomainError("解除时间不能早于事件开始时间")
            self.store.mark_event_lifted(conn, event_id, clocks.to_iso(end), actor)
            self._audit(conn, None, actor, "event.lifted",
                        {"event_id": event_id, "code": event["code"], "region": event["region"],
                         "ended_at": clocks.to_iso(end)})
            reopened: list[dict[str, Any]] = []
            if not self.store.has_other_active_event(conn, event["region"], event_id):
                for claim in self.store.resumable_claims_from(conn, event["region"], event_id):
                    self.store.resume_claim(conn, claim["id"], utcnow())
                    row = self._claim(conn, claim["id"])
                    view = self._clock_for(conn, row)
                    self._audit(conn, claim["id"], actor, "claim.reopened",
                                {"event_id": event_id, "code": event["code"],
                                 "paused_seconds": view["paused_seconds"], "paused_days": view["paused_days"],
                                 "current_deadline": view["current_deadline"]})
                    reopened.append({"claim_id": claim["id"], "claim_no": claim["claim_no"],
                                     "paused_days": view["paused_days"], "current_deadline": view["current_deadline"]})
            result = dict(self.store.get_event(conn, event_id))
            result["reopened_claims"] = reopened
            return result

    def reopen_claim(self, actor: str, role: str, claim_id: int) -> dict[str, Any]:
        """手动重开：仅在区域内事件均已解除时允许，重开后可继续办理，期限已按暂停时长顺延。"""
        actor = actor_id(actor)
        require_role(role, {"supervisor"}, "重开案件")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            if claim["status"] != "suspended":
                raise DomainError("只有中止中的案件可以重开", 409)
            active = [dict(e) for e in conn.execute(
                "SELECT * FROM disaster_events WHERE region=? AND status='active'", (claim["region"],)
            ).fetchall()]
            if active:
                raise DomainError("受灾区域内仍有未解除事件，不能重开", 409)
            self.store.resume_claim(conn, claim_id, utcnow())
            row = self._claim(conn, claim_id)
            view = self._clock_for(conn, row)
            self._audit(conn, claim_id, actor, "claim.reopened",
                        {"manual": True, "paused_seconds": view["paused_seconds"],
                         "current_deadline": view["current_deadline"]})
            result = dict(row)
            result["clock"] = view
            return result

    def list_events(self, role: str = "viewer") -> list[dict[str, Any]]:
        if role not in {"intake", "supervisor", "adjuster", "surveyor", "auditor"}:
            raise DomainError("角色无权查看事件", 403)
        with self.connect() as conn:
            return [dict(r) for r in self.store.list_events(conn)]

    def claim_detail(self, actor: str, role: str, claim_id: int) -> dict[str, Any]:
        if role not in {"intake", "supervisor", "adjuster", "surveyor", "auditor"}:
            raise DomainError("角色无权查看案件详情", 403)
        with self.connect() as conn:
            claim = self._claim(conn, claim_id)
            if role in {"adjuster", "surveyor"} and claim["assignee"] != actor and claim["surveyor"] != actor:
                raise DomainError("只能查看分配给自己的案件", 403)
            events = [dict(e) for e in self.store.region_events(conn, claim["region"])]
            data = dict(claim)
            data["clock"] = clocks.deadline_view(data, events)
            data["events"] = events
            data["evidence"] = [dict(r) for r in conn.execute(
                "SELECT * FROM evidence WHERE claim_id=? ORDER BY id", (claim_id,)
            ).fetchall()]
            data["survey_notes"] = [dict(r) for r in conn.execute(
                "SELECT * FROM survey_notes WHERE claim_id=? ORDER BY id", (claim_id,)
            ).fetchall()]
            data["payments"] = [dict(r) for r in conn.execute(
                "SELECT * FROM payments WHERE claim_id=? ORDER BY id", (claim_id,)
            ).fetchall()]
            data["timeline"] = [dict(r) for r in conn.execute(
                "SELECT * FROM timeline WHERE claim_id=? ORDER BY id", (claim_id,)
            ).fetchall()]
            return data

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
            else:
                # 受理时区域已有生效灾害事件：直接进入时效中止
                active = conn.execute(
                    "SELECT * FROM disaster_events WHERE region=? AND status='active' ORDER BY id",
                    (region.strip(),),
                ).fetchall()
                if active:
                    self.store.suspend_claim(conn, cur.lastrowid, utcnow())
                    for ev in active:
                        self._audit(conn, cur.lastrowid, actor, "claim.auto_suspended",
                                    {"event_id": ev["id"], "code": ev["code"], "previous_status": "received"})
            result = dict(self._claim(conn, cur.lastrowid))
            result["clock"] = self._clock_for(conn, result)
            return result

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
            self._guard_not_suspended(claim)
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
            result = dict(self._claim(conn, claim_id))
            result["clock"] = self._clock_for(conn, result)
            return result

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
            self._guard_not_suspended(claim)
            if claim["status"] not in {"triaged", "escalated", "assigned"}:
                raise DomainError("当前状态不能分配", 409)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            conn.execute(
                """UPDATE claims SET assignee=?,surveyor=?,status='assigned',version=version+1,updated_at=?
                   WHERE id=? AND version=?""",
                (assignee, surveyor.strip() if surveyor else None, utcnow(), claim_id, expected_version),
            )
            self._audit(conn, claim_id, actor, "claim.assigned", {"assignee": assignee, "surveyor": surveyor})
            result = dict(self._claim(conn, claim_id))
            result["clock"] = self._clock_for(conn, result)
            return result

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
            self._guard_not_suspended(claim)
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
                        "UPDATE claims SET fraud_score=MAX(fraud_score,0.95),status='escalated',version=version+1,updated_at=? WHERE id=? AND status<>'duplicate' AND status<>'suspended'",
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
            self._guard_not_suspended(claim)
            if claim["status"] not in {"assigned", "escalated"}:
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
            result = dict(self._claim(conn, claim_id))
            result["clock"] = self._clock_for(conn, result)
            return result

    def submit_review(self, actor: str, role: str, claim_id: int, expected_version: int) -> dict[str, Any]:
        actor = actor_id(actor)
        require_role(role, {"adjuster", "surveyor"}, "提交核损")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claim = self._claim(conn, claim_id)
            self._guard_not_suspended(claim)
            if claim["status"] != "survey":
                raise DomainError("只有已查勘案件可以提交核损", 409)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            if not conn.execute("SELECT 1 FROM survey_notes WHERE claim_id=?", (claim_id,)).fetchone():
                raise DomainError("缺少查勘记录", 409)
            conn.execute("UPDATE claims SET status='review',version=version+1,updated_at=? WHERE id=?", (utcnow(), claim_id))
            self._audit(conn, claim_id, actor, "claim.review_submitted", {})
            result = dict(self._claim(conn, claim_id))
            result["clock"] = self._clock_for(conn, result)
            return result

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
            self._guard_not_suspended(claim)
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
            result = dict(self._claim(conn, claim_id))
            result["clock"] = self._clock_for(conn, result)
            return result

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
            self._guard_not_suspended(claim)
            if claim["status"] != "review":
                raise DomainError("只有待复核案件可以最终核定", 409)
            if claim["version"] != int(expected_version):
                raise DomainError("案件已变化，请刷新后重试", 409)
            if claim["duplicate_of"]:
                raise DomainError("重复报案不能核定赔付", 409)
            if claim["fraud_score"] >= 0.8 and decision == "approve":
                raise DomainError("高风险案件未解除风险标记，不能赔付", 409)
            if decision == "approve" and payout > claim["estimated_loss"]:
                raise DomainError("核定金额不能超过预估损失")
            if decision == "reject" and not reason.strip():
                raise DomainError("拒赔必须填写理由", 409)
            status = "approved" if decision == "approve" else "rejected"
            conn.execute(
                "UPDATE claims SET status=?,final_payout=?,version=version+1,updated_at=? WHERE id=? AND version=?",
                (status, payout if decision == "approve" else 0, utcnow(), claim_id, expected_version),
            )
            self._audit(conn, claim_id, actor, "claim.finalized", {"decision": decision, "payout": payout, "reason": reason.strip()})
            result = dict(self._claim(conn, claim_id))
            result["clock"] = self._clock_for(conn, result)
            return result

    def queue(self, role: str = "viewer", actor: str = "") -> list[dict[str, Any]]:
        if role not in {"intake", "supervisor", "adjuster", "surveyor", "auditor"}:
            raise DomainError("角色无权查看理赔队列", 403)
        with self.connect() as conn:
            if role in {"adjuster", "surveyor"}:
                rows = conn.execute(
                    "SELECT * FROM claims WHERE (assignee=? OR surveyor=?) AND status NOT IN ('approved','rejected','duplicate') ORDER BY priority_score DESC,created_at",
                    (actor, actor),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM claims ORDER BY priority_score DESC,created_at").fetchall()
            return self._enrich_clocks(conn, rows)

    def state(self, actor: str = "", role: str = "viewer") -> dict[str, Any]:
        allowed = role in {"intake", "supervisor", "adjuster", "surveyor", "auditor"}
        if not allowed:
            return {"claims": [], "evidence": [], "payments": [], "timeline": [], "events": [], "access_limited": True}
        with self.connect() as conn:
            if role in {"adjuster", "surveyor"}:
                rows = conn.execute(
                    "SELECT * FROM claims WHERE assignee=? OR surveyor=? ORDER BY priority_score DESC,id DESC", (actor, actor)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM claims ORDER BY priority_score DESC,id DESC").fetchall()
            claims = self._enrich_clocks(conn, rows)
            ids = [c["id"] for c in claims]
            if ids:
                marks = ",".join("?" for _ in ids)
                evidence = [dict(r) for r in conn.execute("SELECT * FROM evidence WHERE claim_id IN (%s) ORDER BY id DESC" % marks, ids).fetchall()]
                payments = [dict(r) for r in conn.execute("SELECT * FROM payments WHERE claim_id IN (%s) ORDER BY id DESC" % marks, ids).fetchall()]
                timeline = [dict(r) for r in conn.execute("SELECT * FROM timeline WHERE claim_id IN (%s) ORDER BY id DESC LIMIT 300" % marks, ids).fetchall()]
            else:
                evidence, payments, timeline = [], [], []
            events = [dict(r) for r in self.store.list_events(conn)]
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

    def _send_static(self, rel_path: str) -> bool:
        static_root = (ROOT / "static").resolve()
        target = (static_root / rel_path).resolve()
        if static_root not in target.parents or not target.is_file():
            self._send(404, {"error": "资源不存在"})
            return True
        body = target.read_bytes()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
        }.get(target.suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if path in {"/", "/index.html"}:
                self._send_static("index.html")
                return
            if path.startswith("/static/"):
                self._send_static(path[len("/static/"):])
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
                self._send(200, {"events": self.service.list_events(role)})
            elif path.startswith("/api/claims/") and path.endswith("/detail"):
                actor, role = self._headers()
                claim_id = int(path[len("/api/claims/"):-len("/detail")])
                self._send(200, self.service.claim_detail(actor, role, claim_id))
            else:
                self._send(404, {"error": "接口不存在"})
        except DomainError as exc:
            self._send(exc.status, {"error": str(exc)})
        except ValueError:
            self._send(400, {"error": "案件ID无效"})

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
            elif path == "/api/events":
                result = self.service.register_event(actor, role, **data)
            elif path == "/api/events/lift":
                result = self.service.lift_event(actor, role, **data)
            elif path == "/api/claims/reopen":
                result = self.service.reopen_claim(actor, role, **data)
            else:
                raise DomainError("接口不存在", 404)
            self._send(201, result)
        except DomainError as exc:
            self._send(exc.status, {"error": str(exc)})
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
