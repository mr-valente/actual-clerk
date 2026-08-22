from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any

ACTIVE_STATUSES = ("queued", "running", "retry_wait")
TERMINAL_STATUSES = ("completed", "failed", "needs_review", "cancelled")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    trigger TEXT NOT NULL DEFAULT 'manual',
    status TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'queued',
    params_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    progress_current INTEGER NOT NULL DEFAULT 0,
    progress_total INTEGER NOT NULL DEFAULT 0,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    next_run_at REAL NOT NULL,
    lease_until REAL,
    worker_id TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL
);

-- One active job per kind. A second sync request while a sync is running is a
-- duplicate, not a queue.
CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_active_kind
ON jobs(kind) WHERE status IN ('queued', 'running', 'retry_wait');
CREATE INDEX IF NOT EXISTS ix_jobs_claim ON jobs(status, next_run_at, created_at);
CREATE INDEX IF NOT EXISTS ix_jobs_created ON jobs(created_at DESC);

CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    level TEXT NOT NULL,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_job ON job_events(job_id, id DESC);

CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
    transaction_id TEXT NOT NULL,
    account_id TEXT NOT NULL DEFAULT '',
    account_name TEXT NOT NULL DEFAULT '',
    payee_name TEXT NOT NULL DEFAULT '',
    merchant_key TEXT NOT NULL DEFAULT '',
    transaction_date TEXT NOT NULL DEFAULT '',
    amount_cents INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    category_id TEXT,
    category_name TEXT NOT NULL DEFAULT '',
    proposed_category TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0,
    tags_json TEXT NOT NULL DEFAULT '[]',
    rationale_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    resolved_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_decision_open_transaction
ON decisions(transaction_id) WHERE status = 'needs_review';
CREATE INDEX IF NOT EXISTS ix_decisions_created ON decisions(created_at DESC);
CREATE INDEX IF NOT EXISTS ix_decisions_status ON decisions(status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_decisions_transaction ON decisions(transaction_id, created_at DESC);

-- Learned merchant -> category evidence produced by Clerk's own applied
-- decisions. Evidence read from Actual's history is recomputed each run and is
-- deliberately not stored here.
CREATE TABLE IF NOT EXISTS merchant_memory (
    merchant_key TEXT NOT NULL,
    category_id TEXT NOT NULL,
    category_name TEXT NOT NULL DEFAULT '',
    hits INTEGER NOT NULL DEFAULT 0,
    corrections INTEGER NOT NULL DEFAULT 0,
    last_seen REAL NOT NULL,
    PRIMARY KEY(merchant_key, category_id)
);

CREATE TABLE IF NOT EXISTS promoted_rules (
    id TEXT PRIMARY KEY,
    merchant_key TEXT NOT NULL,
    merchant_label TEXT NOT NULL DEFAULT '',
    category_id TEXT NOT NULL,
    category_name TEXT NOT NULL DEFAULT '',
    match_value TEXT NOT NULL DEFAULT '',
    observations INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'suggested',
    created_at REAL NOT NULL,
    resolved_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_promoted_rule_open
ON promoted_rules(merchant_key) WHERE status = 'suggested';
CREATE INDEX IF NOT EXISTS ix_promoted_rules_status ON promoted_rules(status, created_at DESC);

CREATE TABLE IF NOT EXISTS account_health (
    account_id TEXT PRIMARY KEY,
    account_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    snapshot_json TEXT NOT NULL DEFAULT '{}',
    since REAL NOT NULL,
    checked_at REAL NOT NULL
);

-- Only explicit opt-outs are stored; an account Clerk has never been told
-- about is monitored.
CREATE TABLE IF NOT EXISTS account_monitoring (
    account_id TEXT PRIMARY KEY,
    account_name TEXT NOT NULL DEFAULT '',
    monitored INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS health_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    account_name TEXT NOT NULL DEFAULT '',
    previous_status TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    notified INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_health_events_created ON health_events(created_at DESC);

CREATE TABLE IF NOT EXISTS digests (
    local_date TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL DEFAULT '{}',
    delivered INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    key TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class Database:
    def __init__(self, path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = threading.Lock()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        # WAL + NORMAL preserves crash-safe committed transactions while
        # avoiding a full filesystem sync for every recorded decision.
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA temp_store=MEMORY")
        return connection

    def initialize(self) -> None:
        with self._init_lock, self.connect() as connection:
            connection.executescript(SCHEMA)
            now = time.time()
            # A job that was running when the process died has no worker to
            # finish it, so hand it back to the queue.
            connection.execute(
                "UPDATE jobs SET status='queued', phase='recovered', worker_id=NULL, "
                "lease_until=NULL, next_run_at=?, updated_at=? WHERE status='running'",
                (now, now),
            )

    # ------------------------------------------------------------------ settings

    def get_setting(self, key: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, time.time()),
            )

    # ---------------------------------------------------------------- snapshots

    def set_snapshot(self, key: str, payload: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO snapshots(key,payload_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET payload_json=excluded.payload_json, "
                "updated_at=excluded.updated_at",
                (key, _json(payload), time.time()),
            )

    def get_snapshot(self, key: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload_json,updated_at FROM snapshots WHERE key=?", (key,)
            ).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload_json"])
        payload["snapshot_updated_at"] = row["updated_at"]
        return payload

    # --------------------------------------------------------------------- jobs

    def enqueue_job(
        self,
        kind: str,
        max_attempts: int,
        *,
        trigger: str = "manual",
        params: dict[str, Any] | None = None,
        delay_seconds: float = 0.0,
    ) -> tuple[dict[str, Any], bool]:
        now = time.time()
        job_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM jobs WHERE kind=? AND status IN ('queued','running','retry_wait') "
                "ORDER BY created_at DESC LIMIT 1",
                (kind,),
            ).fetchone()
            if existing:
                connection.commit()
                return self._job_row(existing), False
            connection.execute(
                "INSERT INTO jobs(id,kind,trigger,status,phase,params_json,max_attempts,"
                "next_run_at,created_at,updated_at) VALUES(?,?,?,'queued','queued',?,?,?,?,?)",
                (
                    job_id,
                    kind,
                    trigger,
                    _json(params or {}),
                    max_attempts,
                    now + delay_seconds,
                    now,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            connection.commit()
        self.add_event(job_id, "info", "enqueued", f"{kind} job queued ({trigger})")
        return self._job_row(row), True

    def claim_job(self, worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
        now = time.time()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE jobs SET status='queued', phase='recovered', worker_id=NULL, lease_until=NULL, "
                "next_run_at=?, updated_at=? WHERE status='running' AND lease_until IS NOT NULL "
                "AND lease_until < ?",
                (now, now, now),
            )
            row = connection.execute(
                "SELECT * FROM jobs WHERE status IN ('queued','retry_wait') AND next_run_at <= ? "
                "ORDER BY next_run_at, created_at LIMIT 1",
                (now,),
            ).fetchone()
            if not row:
                connection.commit()
                return None
            connection.execute(
                "UPDATE jobs SET status='running', phase='starting', attempt=attempt+1, worker_id=?, "
                "lease_until=?, started_at=COALESCE(started_at,?), updated_at=?, error_code=NULL, "
                "error_message=NULL WHERE id=?",
                (worker_id, now + lease_seconds, now, now, row["id"]),
            )
            claimed = connection.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            connection.commit()
        return self._job_row(claimed)

    def heartbeat(self, job_id: str, lease_seconds: int) -> None:
        now = time.time()
        with self.connect() as connection:
            connection.execute(
                "UPDATE jobs SET lease_until=?, updated_at=? WHERE id=? AND status='running'",
                (now + lease_seconds, now, job_id),
            )

    def update_job(
        self,
        job_id: str,
        *,
        phase: str | None = None,
        current: int | None = None,
        total: int | None = None,
    ) -> None:
        fields: list[str] = ["updated_at=?"]
        values: list[Any] = [time.time()]
        for column, value in (
            ("phase", phase),
            ("progress_current", current),
            ("progress_total", total),
        ):
            if value is not None:
                fields.append(f"{column}=?")
                values.append(value)
        values.append(job_id)
        with self.connect() as connection:
            connection.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id=?", values)

    def finish_job(
        self,
        job_id: str,
        *,
        status: str = "completed",
        phase: str = "complete",
        result: dict[str, Any] | None = None,
    ) -> None:
        now = time.time()
        with self.connect() as connection:
            connection.execute(
                "UPDATE jobs SET status=?, phase=?, result_json=?, lease_until=NULL, worker_id=NULL, "
                "completed_at=?, updated_at=? WHERE id=?",
                (status, phase, _json(result or {}), now, now, job_id),
            )

    def fail_or_retry(self, job_id: str, code: str, message: str, retryable: bool) -> str:
        now = time.time()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT attempt,max_attempts FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not row:
                connection.rollback()
                return "missing"
            should_retry = retryable and row["attempt"] < row["max_attempts"]
            if should_retry:
                delay = min(600, 15 * (2 ** max(0, row["attempt"] - 1)))
                status, phase, completed_at = "retry_wait", "retry_wait", None
                next_run = now + delay
            else:
                status, phase, completed_at = "failed", "failed", now
                next_run = now
            connection.execute(
                "UPDATE jobs SET status=?,phase=?,next_run_at=?,lease_until=NULL,worker_id=NULL,"
                "error_code=?,error_message=?,completed_at=?,updated_at=? WHERE id=?",
                (status, phase, next_run, code, message[:2000], completed_at, now, job_id),
            )
            connection.commit()
        self.add_event(
            job_id, "warning" if should_retry else "error", status, message, {"code": code}
        )
        return status

    def retry_job(self, job_id: str) -> dict[str, Any] | None:
        now = time.time()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["status"] not in TERMINAL_STATUSES:
                connection.rollback()
                return None
            active = connection.execute(
                "SELECT id FROM jobs WHERE kind=? AND status IN ('queued','running','retry_wait')",
                (row["kind"],),
            ).fetchone()
            if active:
                connection.rollback()
                return None
            connection.execute(
                "UPDATE jobs SET status='queued',phase='queued',attempt=0,next_run_at=?,lease_until=NULL,"
                "worker_id=NULL,error_code=NULL,error_message=NULL,completed_at=NULL,updated_at=? WHERE id=?",
                (now, now, job_id),
            )
            updated = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            connection.commit()
        self.add_event(job_id, "info", "retried", "Job manually queued for retry")
        return self._job_row(updated)

    def cancel_job(self, job_id: str) -> bool:
        now = time.time()
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET status='cancelled',phase='cancelled',completed_at=?,updated_at=? "
                "WHERE id=? AND status IN ('queued','retry_wait')",
                (now, now, job_id),
            )
        if cursor.rowcount:
            self.add_event(job_id, "info", "cancelled", "Job cancelled")
        return bool(cursor.rowcount)

    def get_job(self, job_id: str, *, include_events: bool = False) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            result = self._job_row(row)
            if include_events:
                result["events"] = [
                    self._event_row(item)
                    for item in connection.execute(
                        "SELECT * FROM job_events WHERE job_id=? ORDER BY id DESC LIMIT 100",
                        (job_id,),
                    ).fetchall()
                ]
        return result

    def list_jobs(
        self, *, status: str | None = None, kind: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM jobs"
        clauses: list[str] = []
        values: list[Any] = []
        if status == "active":
            clauses.append("status IN ('queued','running','retry_wait')")
        elif status:
            clauses.append("status=?")
            values.append(status)
        if kind:
            clauses.append("kind=?")
            values.append(kind)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        values.append(limit)
        with self.connect() as connection:
            return [self._job_row(row) for row in connection.execute(sql, values).fetchall()]

    def last_job(self, kind: str, status: str | None = None) -> dict[str, Any] | None:
        sql = "SELECT * FROM jobs WHERE kind=?"
        values: list[Any] = [kind]
        if status:
            sql += " AND status=?"
            values.append(status)
        sql += " ORDER BY COALESCE(completed_at, created_at) DESC LIMIT 1"
        with self.connect() as connection:
            row = connection.execute(sql, values).fetchone()
        return self._job_row(row) if row else None

    def add_event(
        self,
        job_id: str,
        level: str,
        event_type: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO job_events(job_id,level,event_type,message,data_json,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (job_id, level, event_type, message[:1000], _json(data or {}), time.time()),
            )

    @staticmethod
    def _job_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["params"] = json.loads(item.pop("params_json"))
        item["result"] = json.loads(item.pop("result_json"))
        return item

    @staticmethod
    def _event_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["data"] = json.loads(item.pop("data_json"))
        return item

    # ---------------------------------------------------------------- decisions

    def add_decision(self, decision: dict[str, Any]) -> str:
        decision_id = str(uuid.uuid4())
        now = time.time()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # Only one review may be open per transaction; a newer proposal
            # supersedes an older one rather than piling up.
            connection.execute(
                "UPDATE decisions SET status='superseded', resolved_at=? "
                "WHERE transaction_id=? AND status='needs_review'",
                (now, decision["transaction_id"]),
            )
            connection.execute(
                "INSERT INTO decisions(id,job_id,transaction_id,account_id,account_name,payee_name,"
                "merchant_key,transaction_date,amount_cents,source,status,category_id,category_name,"
                "proposed_category,confidence,tags_json,rationale_json,created_at,resolved_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    decision_id,
                    decision.get("job_id"),
                    decision["transaction_id"],
                    decision.get("account_id", ""),
                    decision.get("account_name", ""),
                    decision.get("payee_name", ""),
                    decision.get("merchant_key", ""),
                    decision.get("transaction_date", ""),
                    int(decision.get("amount_cents", 0)),
                    decision.get("source", "unknown"),
                    decision.get("status", "applied"),
                    decision.get("category_id"),
                    decision.get("category_name", ""),
                    decision.get("proposed_category", ""),
                    float(decision.get("confidence", 0.0)),
                    _json(decision.get("tags", [])),
                    _json(decision.get("rationale", {})),
                    now,
                    now if decision.get("status") != "needs_review" else None,
                ),
            )
            connection.commit()
        return decision_id

    def list_decisions(
        self, *, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM decisions"
        values: list[Any] = []
        if status:
            sql += " WHERE status=?"
            values.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        values.append(limit)
        with self.connect() as connection:
            return [self._decision_row(row) for row in connection.execute(sql, values).fetchall()]

    def get_decision(self, decision_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM decisions WHERE id=?", (decision_id,)
            ).fetchone()
        return self._decision_row(row) if row else None

    def resolve_decision(self, decision_id: str, status: str) -> dict[str, Any] | None:
        """Atomically close an open review so two clicks cannot both apply it."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM decisions WHERE id=? AND status='needs_review'", (decision_id,)
            ).fetchone()
            if not row:
                connection.rollback()
                return None
            connection.execute(
                "UPDATE decisions SET status=?, resolved_at=? WHERE id=?",
                (status, time.time(), decision_id),
            )
            connection.commit()
        return self._decision_row(row)

    def reopen_decision(self, decision_id: str) -> bool:
        """Return a claimed review to the queue after a failed write."""
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE decisions SET status='needs_review', resolved_at=NULL "
                "WHERE id=? AND status!='needs_review'",
                (decision_id,),
            )
        return bool(cursor.rowcount)

    def open_review_transaction_ids(self) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT transaction_id FROM decisions WHERE status='needs_review'"
            ).fetchall()
        return {row["transaction_id"] for row in rows}

    @staticmethod
    def _decision_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["tags"] = json.loads(item.pop("tags_json"))
        item["rationale"] = json.loads(item.pop("rationale_json"))
        return item

    # ----------------------------------------------------------------- memory

    def record_memory(
        self, merchant_key: str, category_id: str, category_name: str, *, correction: bool = False
    ) -> None:
        if not merchant_key or not category_id:
            return
        now = time.time()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO merchant_memory(merchant_key,category_id,category_name,hits,corrections,last_seen) "
                "VALUES(?,?,?,1,?,?) ON CONFLICT(merchant_key,category_id) DO UPDATE SET "
                "hits=merchant_memory.hits+1, corrections=merchant_memory.corrections+excluded.corrections, "
                "category_name=excluded.category_name, last_seen=excluded.last_seen",
                (merchant_key, category_id, category_name, 1 if correction else 0, now),
            )

    def memory_for(self, merchant_key: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM merchant_memory WHERE merchant_key=? ORDER BY hits DESC",
                (merchant_key,),
            ).fetchall()
        return [dict(row) for row in rows]

    def memory_size(self) -> int:
        with self.connect() as connection:
            return connection.execute(
                "SELECT COUNT(DISTINCT merchant_key) FROM merchant_memory"
            ).fetchone()[0]

    def forget_merchant(self, merchant_key: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM merchant_memory WHERE merchant_key=?", (merchant_key,)
            )

    # --------------------------------------------------------- promoted rules

    def suggest_rule(
        self,
        *,
        merchant_key: str,
        merchant_label: str,
        category_id: str,
        category_name: str,
        match_value: str,
        observations: int,
    ) -> str | None:
        """Record a rule suggestion, ignoring merchants already suggested or declined."""
        rule_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id,status FROM promoted_rules WHERE merchant_key=? "
                "AND status IN ('suggested','created','declined') LIMIT 1",
                (merchant_key,),
            ).fetchone()
            if existing:
                connection.commit()
                return None
            connection.execute(
                "INSERT INTO promoted_rules(id,merchant_key,merchant_label,category_id,category_name,"
                "match_value,observations,status,created_at) VALUES(?,?,?,?,?,?,?,'suggested',?)",
                (
                    rule_id,
                    merchant_key,
                    merchant_label,
                    category_id,
                    category_name,
                    match_value,
                    observations,
                    time.time(),
                ),
            )
            connection.commit()
        return rule_id

    def list_rule_suggestions(
        self, *, status: str = "suggested", limit: int = 100
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM promoted_rules WHERE status=? ORDER BY observations DESC, created_at DESC "
                "LIMIT ?",
                (status, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_rule_suggestion(self, rule_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM promoted_rules WHERE id=? AND status='suggested'", (rule_id,)
            ).fetchone()
            if not row:
                connection.rollback()
                return None
            connection.execute(
                "UPDATE promoted_rules SET status='claimed' WHERE id=?", (rule_id,)
            )
            connection.commit()
        return dict(row)

    def resolve_rule_suggestion(self, rule_id: str, status: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE promoted_rules SET status=?, resolved_at=? WHERE id=?",
                (status, time.time(), rule_id),
            )

    # ------------------------------------------------------------------ health

    def record_health(self, snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Persist the latest per-account health and return genuine transitions."""
        now = time.time()
        transitions: list[dict[str, Any]] = []
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for snapshot in snapshots:
                account_id = snapshot["account_id"]
                status = snapshot["status"]
                previous = connection.execute(
                    "SELECT status,since FROM account_health WHERE account_id=?", (account_id,)
                ).fetchone()
                changed = previous is None or previous["status"] != status
                since = now if changed else previous["since"]
                connection.execute(
                    "INSERT INTO account_health(account_id,account_name,status,detail,snapshot_json,"
                    "since,checked_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET "
                    "account_name=excluded.account_name,status=excluded.status,detail=excluded.detail,"
                    "snapshot_json=excluded.snapshot_json,since=excluded.since,checked_at=excluded.checked_at",
                    (
                        account_id,
                        snapshot.get("account_name", ""),
                        status,
                        snapshot.get("detail", ""),
                        _json(snapshot),
                        since,
                        now,
                    ),
                )
                if changed:
                    connection.execute(
                        "INSERT INTO health_events(account_id,account_name,previous_status,status,"
                        "detail,created_at) VALUES(?,?,?,?,?,?)",
                        (
                            account_id,
                            snapshot.get("account_name", ""),
                            previous["status"] if previous else "",
                            status,
                            snapshot.get("detail", ""),
                            now,
                        ),
                    )
                    transitions.append(
                        {
                            **snapshot,
                            "previous_status": previous["status"] if previous else "",
                        }
                    )
            connection.commit()
        return transitions

    def set_monitoring(self, account_id: str, monitored: bool, account_name: str = "") -> None:
        """Record whether Clerk should watch an account's bank connection."""
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO account_monitoring(account_id,account_name,monitored,updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET monitored=excluded.monitored,"
                "account_name=CASE WHEN excluded.account_name != '' THEN excluded.account_name "
                "ELSE account_monitoring.account_name END, updated_at=excluded.updated_at",
                (account_id, account_name, 1 if monitored else 0, time.time()),
            )

    def unmonitored_account_ids(self) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT account_id FROM account_monitoring WHERE monitored=0"
            ).fetchall()
        return {row["account_id"] for row in rows}

    def health_snapshots(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM account_health ORDER BY account_name"
            ).fetchall()
        result = []
        for row in rows:
            item = json.loads(row["snapshot_json"])
            item.update(
                {
                    "account_id": row["account_id"],
                    "account_name": row["account_name"],
                    "status": row["status"],
                    "detail": row["detail"],
                    "since": row["since"],
                    "checked_at": row["checked_at"],
                }
            )
            result.append(item)
        return result

    def prune_health(self, account_ids: list[str]) -> None:
        """Drop health rows for accounts that no longer exist in Actual."""
        with self.connect() as connection:
            if not account_ids:
                connection.execute("DELETE FROM account_health")
                return
            placeholders = ",".join("?" for _ in account_ids)
            connection.execute(
                f"DELETE FROM account_health WHERE account_id NOT IN ({placeholders})", account_ids
            )

    def list_health_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM health_events ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_health_events_notified(self, event_ids: list[int]) -> None:
        if not event_ids:
            return
        placeholders = ",".join("?" for _ in event_ids)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE health_events SET notified=1 WHERE id IN ({placeholders})", event_ids
            )

    # ----------------------------------------------------------------- digests

    def claim_digest(self, local_date: str) -> bool:
        """Reserve today's digest exactly once, even across restarts."""
        with self.connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO digests(local_date,created_at) VALUES(?,?)",
                    (local_date, time.time()),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def complete_digest(
        self, local_date: str, payload: dict[str, Any], *, delivered: bool, error: str = ""
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE digests SET payload_json=?, delivered=?, error=? WHERE local_date=?",
                (_json(payload), 1 if delivered else 0, error[:500], local_date),
            )

    def release_digest(self, local_date: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM digests WHERE local_date=?", (local_date,))

    def latest_digest(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM digests WHERE delivered=1 ORDER BY local_date DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        return item

    # -------------------------------------------------------------- dashboard

    def counts(self) -> dict[str, int]:
        with self.connect() as connection:
            jobs = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status,COUNT(*) AS count FROM jobs GROUP BY status"
                ).fetchall()
            }
            needs_review = connection.execute(
                "SELECT COUNT(*) FROM decisions WHERE status='needs_review'"
            ).fetchone()[0]
            applied_today = connection.execute(
                "SELECT COUNT(*) FROM decisions WHERE status='applied' AND created_at >= ?",
                (time.time() - 86_400,),
            ).fetchone()[0]
            rule_suggestions = connection.execute(
                "SELECT COUNT(*) FROM promoted_rules WHERE status='suggested'"
            ).fetchone()[0]
            degraded = connection.execute(
                "SELECT COUNT(*) FROM account_health WHERE status NOT IN ('ok','not_linked','muted')"
            ).fetchone()[0]
        return {
            "active_jobs": sum(jobs.get(status, 0) for status in ACTIVE_STATUSES),
            "failed_jobs": jobs.get("failed", 0),
            "needs_review": needs_review,
            "applied_today": applied_today,
            "rule_suggestions": rule_suggestions,
            "degraded_accounts": degraded,
        }
