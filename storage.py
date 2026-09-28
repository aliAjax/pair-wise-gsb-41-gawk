"""资料层：SQLite 建表、迁移与灾害事件数据访问。

只负责持久化，不包含时效计算规则（见 clocks.py）与流程编排（见 app.py）。
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catastrophe_claims.db")


class ClaimStore:
    def __init__(self, db_path: str | os.PathLike[str] = DEFAULT_DB):
        self.db_path = str(db_path)
        self.init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def init_schema(self) -> None:
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
                CREATE TABLE IF NOT EXISTS disaster_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    peril_type TEXT NOT NULL,
                    region TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    registered_by TEXT NOT NULL,
                    lifted_by TEXT,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_claims_queue ON claims(status, priority_score DESC, created_at);
                CREATE INDEX IF NOT EXISTS idx_evidence_hash ON evidence(sha256);
                CREATE INDEX IF NOT EXISTS idx_events_region ON disaster_events(region,status);
                """
            )
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(claims)").fetchall()}
        if "pre_suspend_status" not in cols:
            conn.execute("ALTER TABLE claims ADD COLUMN pre_suspend_status TEXT")

    # ---------- 灾害事件 ----------
    def insert_event(self, conn: sqlite3.Connection, code: str, name: str, peril_type: str,
                     region: str, started_at: str, actor: str, note: str, now: str) -> int:
        cur = conn.execute(
            """INSERT INTO disaster_events(code,name,peril_type,region,started_at,status,registered_by,note,created_at,updated_at)
               VALUES(?,?,?,?,?,'active',?,?,?,?)""",
            (code, name, peril_type, region, started_at, actor, note, now, now),
        )
        return cur.lastrowid

    def get_event(self, conn: sqlite3.Connection, event_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM disaster_events WHERE id=?", (event_id,)).fetchone()

    def get_event_by_code(self, conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM disaster_events WHERE code=?", (code,)).fetchone()

    def list_events(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM disaster_events ORDER BY id DESC").fetchall()

    def region_events(self, conn: sqlite3.Connection, region: str) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM disaster_events WHERE region=? ORDER BY started_at,id", (region,)
        ).fetchall()

    def mark_event_lifted(self, conn: sqlite3.Connection, event_id: int, ended_at: str, actor: str) -> None:
        conn.execute(
            "UPDATE disaster_events SET status='lifted',ended_at=?,lifted_by=?,updated_at=? WHERE id=?",
            (ended_at, actor, ended_at, event_id),
        )

    # ---------- 案件挂起 / 重开 ----------
    def suspendable_claims(self, conn: sqlite3.Connection, region: str) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM claims WHERE region=? AND status NOT IN ('duplicate','approved','rejected','closed','suspended')",
            (region,),
        ).fetchall()

    def resumable_claims_from(self, conn: sqlite3.Connection, region: str, exclude_event_id: int) -> list[sqlite3.Row]:
        """区域内因本次事件而挂起、且无其他生效事件的案件。"""
        return conn.execute(
            """SELECT c.* FROM claims c
               WHERE c.region=? AND c.status='suspended'
                 AND NOT EXISTS (
                     SELECT 1 FROM disaster_events e
                     WHERE e.region=c.region AND e.status='active' AND e.id<>?
                 )""",
            (region, exclude_event_id),
        ).fetchall()

    def suspend_claim(self, conn: sqlite3.Connection, claim_id: int, now: str) -> None:
        conn.execute(
            """UPDATE claims SET pre_suspend_status=status,status='suspended',version=version+1,updated_at=?
               WHERE id=?""",
            (now, claim_id),
        )

    def resume_claim(self, conn: sqlite3.Connection, claim_id: int, now: str) -> None:
        conn.execute(
            """UPDATE claims SET status=pre_suspend_status,pre_suspend_status=NULL,version=version+1,updated_at=?
               WHERE id=?""",
            (now, claim_id),
        )

    def has_other_active_event(self, conn: sqlite3.Connection, region: str, exclude_event_id: int) -> bool:
        row = conn.execute(
            "SELECT 1 FROM disaster_events WHERE region=? AND status='active' AND id<>? LIMIT 1",
            (region, exclude_event_id),
        ).fetchone()
        return row is not None

    def active_regions(self, conn: sqlite3.Connection) -> set[str]:
        return {r["region"] for r in conn.execute(
            "SELECT DISTINCT region FROM disaster_events WHERE status='active'"
        ).fetchall()}
