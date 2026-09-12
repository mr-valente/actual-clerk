from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
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
    resolved_at REAL,
    -- What Actual later showed about this decision: '' until looked at,
    -- then standing, corrected, cleared, or gone.
    observed TEXT NOT NULL DEFAULT '',
    observed_at REAL,
    observed_category_id TEXT NOT NULL DEFAULT '',
    observed_category_name TEXT NOT NULL DEFAULT ''
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

-- A balance response can lead its matching transaction list for one polling
-- cycle. Keep the observation here until repeated checks prove it is a real
-- mismatch; account_health remains the stable, user-visible state.
CREATE TABLE IF NOT EXISTS health_candidates (
    account_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    checks INTEGER NOT NULL DEFAULT 1,
    remote_balance_date TEXT NOT NULL DEFAULT '',
    first_seen REAL NOT NULL,
    checked_at REAL NOT NULL
);

-- One delivery per scheduled time, not per day. Both are "once a day" while
-- the time stands, but moving the time asks for a delivery at the new time
-- rather than silently spending the day on the old one -- which is also what
-- makes the scheduled path testable without waiting for tomorrow.
CREATE TABLE IF NOT EXISTS digests (
    local_date TEXT NOT NULL,
    scheduled_for TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    receipt_json TEXT NOT NULL DEFAULT '{}',
    delivered INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    PRIMARY KEY (local_date, scheduled_for)
);

CREATE TABLE IF NOT EXISTS snapshots (
    key TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- Bank connections Clerk manages itself (Plaid). Actual never sees these:
-- from its side a Clerk-synced account is a manual account, and the link
-- lives here. The access token is stored like every other Clerk secret.
CREATE TABLE IF NOT EXISTS plaid_items (
    item_id TEXT PRIMARY KEY,
    environment TEXT NOT NULL DEFAULT 'sandbox',
    institution_id TEXT NOT NULL DEFAULT '',
    institution_name TEXT NOT NULL DEFAULT '',
    access_token TEXT NOT NULL,
    cursor TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ok',
    last_error TEXT NOT NULL DEFAULT '',
    last_refresh_at REAL,
    last_sync_at REAL,
    last_successful_update TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- One row per Actual account whose bank feed Clerk delivers. `cutover_date`
-- is the first date Clerk imports from the provider; earlier history belongs
-- to whatever fed the account before.
CREATE TABLE IF NOT EXISTS bank_links (
    actual_account_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    item_id TEXT NOT NULL DEFAULT '',
    external_account_id TEXT NOT NULL,
    external_name TEXT NOT NULL DEFAULT '',
    mask TEXT NOT NULL DEFAULT '',
    account_type TEXT NOT NULL DEFAULT '',
    account_subtype TEXT NOT NULL DEFAULT '',
    institution TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    cutover_date TEXT NOT NULL DEFAULT '',
    last_import_at REAL,
    last_error TEXT NOT NULL DEFAULT '',
    -- What Actual linked the account to before Clerk took the feed over, so
    -- the road back needs no guesswork.
    previous_provider TEXT NOT NULL DEFAULT '',
    previous_external_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_bank_links_external
ON bank_links(provider, external_account_id);

-- Every time Clerk gives an existing Actual row a new provider id (a
-- pending-to-posted swap, or a SimpleFIN row adopted at cutover).
CREATE TABLE IF NOT EXISTS import_adoptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actual_account_id TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    previous_imported_id TEXT NOT NULL DEFAULT '',
    imported_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_import_adoptions_created ON import_adoptions(created_at DESC);

-- Card-app notifications forwarded by the phone companion. A source is one
-- app on one phone, pointed at the Actual account whose charges it announces.
CREATE TABLE IF NOT EXISTS notification_sources (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL DEFAULT '',
    device_name TEXT NOT NULL DEFAULT '',
    package_name TEXT NOT NULL,
    app_label TEXT NOT NULL DEFAULT '',
    actual_account_id TEXT NOT NULL DEFAULT '',
    account_name TEXT NOT NULL DEFAULT '',
    sample_title TEXT NOT NULL DEFAULT '',
    sample_text TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    last_seen_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_sources_device_package
ON notification_sources(device_id, package_name);

-- One anticipated charge per notification. Never written into Actual: it
-- counts as spent until the bank feed delivers the matching transaction,
-- at which point it is settled against that row and leaves the report.
CREATE TABLE IF NOT EXISTS anticipated_charges (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES notification_sources(id) ON DELETE CASCADE,
    actual_account_id TEXT NOT NULL DEFAULT '',
    notification_key TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'charge',
    amount_cents INTEGER NOT NULL DEFAULT 0,
    merchant TEXT NOT NULL DEFAULT '',
    merchant_key TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    noticed_at REAL NOT NULL,
    noticed_date TEXT NOT NULL,
    -- A provisional category, from memory or taught by hand, so a charge for
    -- an already-budgeted bill draws on that bill rather than on free money.
    category_id TEXT NOT NULL DEFAULT '',
    category_name TEXT NOT NULL DEFAULT '',
    category_source TEXT NOT NULL DEFAULT '',
    category_confidence REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open',
    matched_transaction_id TEXT NOT NULL DEFAULT '',
    matched_payee TEXT NOT NULL DEFAULT '',
    matched_date TEXT NOT NULL DEFAULT '',
    match_reason TEXT NOT NULL DEFAULT '',
    resolved_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_anticipated_notification
ON anticipated_charges(source_id, notification_key);
CREATE INDEX IF NOT EXISTS ix_anticipated_status ON anticipated_charges(status, noticed_at DESC);
CREATE INDEX IF NOT EXISTS ix_anticipated_noticed ON anticipated_charges(noticed_at DESC);

-- A card app and the bank name the same shop differently ("Valve" on the
-- phone, "Steam" on the statement). Learned when an anticipation settles, or
-- taught by hand, so the next notification matches by merchant and inherits
-- the merchant's category.
CREATE TABLE IF NOT EXISTS merchant_aliases (
    alias_key TEXT PRIMARY KEY,
    merchant_key TEXT NOT NULL,
    alias_label TEXT NOT NULL DEFAULT '',
    merchant_label TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'settled',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- What the user has declared: this merchant belongs in that category. A rule
-- is the user's word, so it carries no confidence column: it is 1.0 by
-- definition and applies without thresholds. Only a person creates one, by
-- hand, by accepting a proposal, or by importing it from Actual.
CREATE TABLE IF NOT EXISTS merchant_rules (
    id TEXT PRIMARY KEY,
    merchant_key TEXT NOT NULL,
    account_id TEXT NOT NULL DEFAULT '',
    match TEXT NOT NULL DEFAULT 'exact',
    category_id TEXT NOT NULL,
    category_name TEXT NOT NULL DEFAULT '',
    merchant_label TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'user',
    status TEXT NOT NULL DEFAULT 'active',
    actual_rule_id TEXT NOT NULL DEFAULT '',
    actual_rule_json TEXT NOT NULL DEFAULT '',
    actual_status TEXT NOT NULL DEFAULT '',
    applied_count INTEGER NOT NULL DEFAULT 0,
    disputed_count INTEGER NOT NULL DEFAULT 0,
    last_applied_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_merchant_rules_live
ON merchant_rules(merchant_key, account_id) WHERE status IN ('active', 'paused');
CREATE INDEX IF NOT EXISTS ix_merchant_rules_status ON merchant_rules(status, updated_at DESC);

-- What Clerk wants to know. Each row is one change Clerk would like to make
-- to its own knowledge, with the evidence for it, waiting for a person.
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    merchant_key TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'open',
    created_at REAL NOT NULL,
    resolved_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_proposal_open
ON proposals(kind, merchant_key) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS ix_proposals_status ON proposals(status, created_at DESC);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


_PLAID_ITEM_DEFAULTS: dict[str, Any] = {
    "environment": "sandbox",
    "institution_id": "",
    "institution_name": "",
    "cursor": "",
    "status": "ok",
    "last_error": "",
    "last_refresh_at": None,
    "last_sync_at": None,
    "last_successful_update": "",
}

_BANK_LINK_DEFAULTS: dict[str, Any] = {
    "item_id": "",
    "external_name": "",
    "mask": "",
    "account_type": "",
    "account_subtype": "",
    "institution": "",
    "enabled": 1,
    "cutover_date": "",
    "last_import_at": None,
    "last_error": "",
    "previous_provider": "",
    "previous_external_id": "",
}


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
            self._migrate_digests(connection)
            connection.executescript(SCHEMA)
            self._migrate_health_candidates(connection)
            self._migrate_bank_links(connection)
            self._migrate_anticipated(connection)
            self._migrate_decisions(connection)
            self._restore_digests(connection)
            now = time.time()
            # A job that was running when the process died has no worker to
            # finish it, so hand it back to the queue.
            connection.execute(
                "UPDATE jobs SET status='queued', phase='recovered', worker_id=NULL, "
                "lease_until=NULL, next_run_at=?, updated_at=? WHERE status='running'",
                (now, now),
            )

    @staticmethod
    def _migrate_digests(connection: sqlite3.Connection) -> None:
        """Step aside for the per-scheduled-time digest table.

        The original keyed one delivery to a whole date. Renaming the table
        before the schema runs lets the schema stay the single definition of
        the new shape, with the rows carried over afterwards.
        """

        columns = {row["name"] for row in connection.execute("PRAGMA table_info(digests)")}
        if columns and "scheduled_for" not in columns:
            connection.execute("ALTER TABLE digests RENAME TO digests_pre_slot")

    @staticmethod
    def _restore_digests(connection: sqlite3.Connection) -> None:
        """Carry the old delivery ledger into the new table, once."""
        present = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='digests_pre_slot'"
        ).fetchone()
        if not present:
            return
        # These rows keep an empty scheduled_for: the time they went out was
        # never recorded, so they are history rather than a live claim.
        connection.execute(
            "INSERT OR IGNORE INTO digests"
            "(local_date,scheduled_for,payload_json,delivered,error,created_at) "
            "SELECT local_date,'',payload_json,delivered,error,created_at FROM digests_pre_slot"
        )
        connection.execute("DROP TABLE digests_pre_slot")

    @staticmethod
    def _migrate_health_candidates(connection: sqlite3.Connection) -> None:
        """Add the upstream observation key to databases from v0.2.3."""

        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(health_candidates)")
        }
        if columns and "remote_balance_date" not in columns:
            connection.execute(
                "ALTER TABLE health_candidates ADD COLUMN "
                "remote_balance_date TEXT NOT NULL DEFAULT ''"
            )
            # Legacy counts represent repeated polls, not distinct upstream
            # observations. Candidates are deliberately non-public, so restart
            # their confirmation rather than carrying suspect evidence forward.
            connection.execute("DELETE FROM health_candidates")

    @staticmethod
    def _migrate_bank_links(connection: sqlite3.Connection) -> None:
        """Add the previous-provider columns to link tables from the first Plaid builds."""

        columns = {row["name"] for row in connection.execute("PRAGMA table_info(bank_links)")}
        for column in ("previous_provider", "previous_external_id"):
            if columns and column not in columns:
                connection.execute(
                    f"ALTER TABLE bank_links ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )

    @staticmethod
    def _migrate_anticipated(connection: sqlite3.Connection) -> None:
        """Add the provisional-category columns to ledgers from the first companion builds."""

        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(anticipated_charges)")
        }
        additions = {
            "category_id": "TEXT NOT NULL DEFAULT ''",
            "category_name": "TEXT NOT NULL DEFAULT ''",
            "category_source": "TEXT NOT NULL DEFAULT ''",
            "category_confidence": "REAL NOT NULL DEFAULT 0",
        }
        for column, definition in additions.items():
            if columns and column not in columns:
                connection.execute(
                    f"ALTER TABLE anticipated_charges ADD COLUMN {column} {definition}"
                )

    @staticmethod
    def _migrate_decisions(connection: sqlite3.Connection) -> None:
        """Add the observation columns to decision ledgers from before Stage 4."""

        columns = {row["name"] for row in connection.execute("PRAGMA table_info(decisions)")}
        additions = {
            "observed": "TEXT NOT NULL DEFAULT ''",
            "observed_at": "REAL",
            "observed_category_id": "TEXT NOT NULL DEFAULT ''",
            "observed_category_name": "TEXT NOT NULL DEFAULT ''",
        }
        for column, definition in additions.items():
            if columns and column not in columns:
                connection.execute(f"ALTER TABLE decisions ADD COLUMN {column} {definition}")

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

    def resolve_open_reviews(self, resolutions: dict[str, str]) -> list[dict[str, str]]:
        """Close reviews whose transactions were settled directly in Actual.

        The status and rationale preserve that Clerk did not apply or learn
        from the decision. The status predicate also makes reconciliation safe
        against a person resolving the same row in the UI at the same time.
        """
        if not resolutions:
            return []
        resolved: list[dict[str, str]] = []
        now = time.time()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT id,transaction_id,rationale_json FROM decisions "
                "WHERE status='needs_review'"
            ).fetchall()
            for row in rows:
                transaction_id = row["transaction_id"]
                reason = resolutions.get(transaction_id)
                if not reason:
                    continue
                rationale = json.loads(row["rationale_json"])
                rationale["external_resolution"] = reason
                cursor = connection.execute(
                    "UPDATE decisions SET status='resolved_external',resolved_at=?,rationale_json=? "
                    "WHERE id=? AND status='needs_review'",
                    (now, _json(rationale), row["id"]),
                )
                if cursor.rowcount:
                    resolved.append(
                        {
                            "id": row["id"],
                            "transaction_id": transaction_id,
                            "reason": reason,
                        }
                    )
            connection.commit()
        return resolved

    def decisions_to_observe(self, *, since_date: str, limit: int = 5000) -> list[dict[str, Any]]:
        """Applied decisions the snapshot can still vouch for, not yet settled by a correction."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM decisions WHERE status='applied' AND category_id IS NOT NULL "
                "AND category_id!='' AND transaction_date>=? AND observed IN ('','standing') "
                "ORDER BY created_at DESC LIMIT ?",
                (since_date, limit),
            ).fetchall()
        return [self._decision_row(row) for row in rows]

    def mark_observed(
        self,
        decision_id: str,
        status: str,
        *,
        category_id: str = "",
        category_name: str = "",
    ) -> bool:
        """Record what Actual showed; only a change of state counts as new."""
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE decisions SET observed=?, observed_at=?, observed_category_id=?, "
                "observed_category_name=? WHERE id=? AND observed!=?",
                (status, time.time(), category_id, category_name, decision_id, status),
            )
        return cursor.rowcount > 0

    def record_rule_dispute(self, rule_id: str) -> int:
        """Count a hand correction against a rule; returns the running total."""
        now = time.time()
        with self.connect() as connection:
            connection.execute(
                "UPDATE merchant_rules SET disputed_count=disputed_count+1, updated_at=? WHERE id=?",
                (now, rule_id),
            )
            row = connection.execute(
                "SELECT disputed_count FROM merchant_rules WHERE id=?", (rule_id,)
            ).fetchone()
        return int(row["disputed_count"]) if row else 0

    def reset_rule_disputes(self, rule_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE merchant_rules SET disputed_count=0, updated_at=? WHERE id=?",
                (time.time(), rule_id),
            )

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

    def list_memory(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """What Clerk has learned, one row per merchant, strongest category first."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT m.merchant_key, m.category_id, m.category_name, m.hits, m.corrections, "
                "m.last_seen, (SELECT payee_name FROM decisions d WHERE d.merchant_key=m.merchant_key "
                "AND d.payee_name!='' ORDER BY d.created_at DESC LIMIT 1) AS label "
                "FROM merchant_memory m ORDER BY m.merchant_key, m.hits+m.corrections*3 DESC"
            ).fetchall()
        merchants: dict[str, dict[str, Any]] = {}
        for row in rows:
            entry = merchants.setdefault(
                row["merchant_key"],
                {
                    "merchant_key": row["merchant_key"],
                    "label": row["label"] or "",
                    "categories": [],
                    "last_seen": 0.0,
                },
            )
            entry["categories"].append(
                {
                    "category_id": row["category_id"],
                    "category_name": row["category_name"],
                    "hits": row["hits"],
                    "corrections": row["corrections"],
                }
            )
            entry["last_seen"] = max(entry["last_seen"], float(row["last_seen"] or 0))
        ordered = sorted(merchants.values(), key=lambda item: -item["last_seen"])
        return ordered[:limit]

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


    # ---------------------------------------------------------- merchant rules

    def upsert_rule(
        self,
        *,
        merchant_key: str,
        category_id: str,
        category_name: str = "",
        account_id: str = "",
        match: str = "exact",
        merchant_label: str = "",
        source: str = "user",
        actual_rule_id: str = "",
        actual_rule_json: str = "",
        actual_status: str = "",
    ) -> dict[str, Any] | None:
        """Declare a rule, or change the category of the live one for this merchant.

        A merchant has one live rule per account scope. Declaring it again
        changes the category (and reactivates a paused rule) rather than
        creating a second, so the user's latest word is always the one that
        applies.
        """

        if not merchant_key or not category_id:
            return None
        now = time.time()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id FROM merchant_rules WHERE merchant_key=? AND account_id=? "
                "AND status IN ('active','paused')",
                (merchant_key, account_id),
            ).fetchone()
            if existing:
                connection.execute(
                    "UPDATE merchant_rules SET category_id=?, category_name=?, match=?, "
                    "merchant_label=CASE WHEN ?!='' THEN ? ELSE merchant_label END, "
                    "source=?, status='active', updated_at=? WHERE id=?",
                    (
                        category_id,
                        category_name,
                        match,
                        merchant_label,
                        merchant_label[:120],
                        source,
                        now,
                        existing["id"],
                    ),
                )
                rule_id = existing["id"]
            else:
                rule_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO merchant_rules(id,merchant_key,account_id,match,category_id,"
                    "category_name,merchant_label,source,status,actual_rule_id,actual_rule_json,"
                    "actual_status,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,'active',?,?,?,?,?)",
                    (
                        rule_id,
                        merchant_key,
                        account_id,
                        match,
                        category_id,
                        category_name,
                        merchant_label[:120],
                        source,
                        actual_rule_id,
                        actual_rule_json,
                        actual_status,
                        now,
                        now,
                    ),
                )
            row = connection.execute(
                "SELECT * FROM merchant_rules WHERE id=?", (rule_id,)
            ).fetchone()
            connection.commit()
        return dict(row)

    def get_rule(self, rule_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM merchant_rules WHERE id=?", (rule_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_rules(
        self, *, statuses: Sequence[str] = ("active", "paused"), limit: int = 2000
    ) -> list[dict[str, Any]]:
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM merchant_rules WHERE status IN ({placeholders}) "
                "ORDER BY merchant_key, account_id LIMIT ?",
                (*statuses, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def active_rules(self) -> list[dict[str, Any]]:
        return self.list_rules(statuses=("active",))

    def rules_for_merchant(self, merchant_key: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM merchant_rules WHERE merchant_key=? AND status IN ('active','paused') "
                "ORDER BY account_id",
                (merchant_key,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_rule(self, rule_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "category_id",
            "category_name",
            "match",
            "status",
            "account_id",
            "merchant_label",
            "actual_status",
            "actual_rule_id",
            "actual_rule_json",
        }
        changes = {key: value for key, value in fields.items() if key in allowed}
        if not changes:
            return self.get_rule(rule_id)
        changes["updated_at"] = time.time()
        assignments = ", ".join(f"{key}=?" for key in changes)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE merchant_rules SET {assignments} WHERE id=?",
                (*changes.values(), rule_id),
            )
        return self.get_rule(rule_id)

    def record_rules_applied(self, counts: Mapping[str, int]) -> None:
        """Count what each rule filed in a run, so the page can show what earns its keep."""
        if not counts:
            return
        now = time.time()
        with self.connect() as connection:
            for rule_id, count in counts.items():
                connection.execute(
                    "UPDATE merchant_rules SET applied_count=applied_count+?, last_applied_at=?, "
                    "updated_at=? WHERE id=?",
                    (int(count), now, now, rule_id),
                )

    def delete_rule(self, rule_id: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute("DELETE FROM merchant_rules WHERE id=?", (rule_id,))
        return cursor.rowcount > 0

    # --------------------------------------------------------------- proposals

    def add_proposal(
        self,
        *,
        kind: str,
        merchant_key: str,
        payload: dict[str, Any],
        evidence: dict[str, Any] | None = None,
    ) -> str | None:
        """Ask the user something once; an open or declined question is not repeated."""
        proposal_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id FROM proposals WHERE kind=? AND merchant_key=? "
                "AND status IN ('open','declined') LIMIT 1",
                (kind, merchant_key),
            ).fetchone()
            if existing:
                connection.commit()
                return None
            connection.execute(
                "INSERT INTO proposals(id,kind,merchant_key,payload_json,evidence_json,status,"
                "created_at) VALUES(?,?,?,?,?,'open',?)",
                (
                    proposal_id,
                    kind,
                    merchant_key,
                    _json(payload),
                    _json(evidence or {}),
                    time.time(),
                ),
            )
            connection.commit()
        return proposal_id

    def list_proposals(self, *, status: str = "open", limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM proposals WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        return [self._proposal_row(row) for row in rows]

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM proposals WHERE id=?", (proposal_id,)
            ).fetchone()
        return self._proposal_row(row) if row else None

    def claim_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        """Take an open proposal so a double click cannot act on it twice."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM proposals WHERE id=? AND status='open'", (proposal_id,)
            ).fetchone()
            if not row:
                connection.rollback()
                return None
            connection.execute(
                "UPDATE proposals SET status='claimed' WHERE id=?", (proposal_id,)
            )
            connection.commit()
        return self._proposal_row(row)

    def resolve_proposal(self, proposal_id: str, status: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE proposals SET status=?, resolved_at=? WHERE id=?",
                (status, None if status == "open" else time.time(), proposal_id),
            )

    def decline_open_proposals(self, *, kind: str | None = None) -> int:
        """Close every open question of a kind, so a bulk proposal can be waved away."""
        sql = "UPDATE proposals SET status='declined', resolved_at=? WHERE status='open'"
        values: list[Any] = [time.time()]
        if kind:
            sql += " AND kind=?"
            values.append(kind)
        with self.connect() as connection:
            cursor = connection.execute(sql, values)
        return cursor.rowcount

    def close_proposals_for(self, merchant_key: str, *, kind: str | None = None) -> int:
        """Withdraw open questions a newer fact has answered (a rule was made by hand)."""
        sql = "UPDATE proposals SET status='withdrawn', resolved_at=? WHERE merchant_key=? AND status='open'"
        values: list[Any] = [time.time(), merchant_key]
        if kind:
            sql += " AND kind=?"
            values.append(kind)
        with self.connect() as connection:
            cursor = connection.execute(sql, values)
        return cursor.rowcount

    @staticmethod
    def _proposal_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        item["evidence"] = json.loads(item.pop("evidence_json"))
        return item

    # ------------------------------------------------------------------ health

    def record_health(
        self,
        snapshots: list[dict[str, Any]],
        *,
        drift_confirmation_checks: int = 1,
    ) -> list[dict[str, Any]]:
        """Persist stable per-account health and return genuine transitions.

        SimpleFIN can expose a new balance just before the corresponding
        transaction set is coherent. When ``drift_confirmation_checks`` is
        greater than one, a mismatch stays a non-alerting candidate until it
        has appeared in that many successively newer bank balance snapshots. Re-reading
        one unchanged ``balance-date`` does not manufacture new evidence.
        ``snapshots`` is updated in place so every caller shows the same
        stabilized status that was saved.
        """
        now = time.time()
        required = max(1, int(drift_confirmation_checks))
        transitions: list[dict[str, Any]] = []
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for snapshot in snapshots:
                account_id = snapshot["account_id"]
                previous = connection.execute(
                    "SELECT status,since FROM account_health WHERE account_id=?", (account_id,)
                ).fetchone()
                effective = dict(snapshot)
                observed_status = str(snapshot.get("status") or "unknown")
                if (
                    observed_status == "drifted"
                    and required > 1
                    and (previous is None or previous["status"] != "drifted")
                ):
                    candidate = connection.execute(
                        "SELECT status,checks,remote_balance_date,first_seen "
                        "FROM health_candidates WHERE account_id=?",
                        (account_id,),
                    ).fetchone()
                    remote_balance_date = str(snapshot.get("remote_balance_date") or "")
                    candidate_balance_date = remote_balance_date
                    same_candidate = (
                        candidate is not None and candidate["status"] == observed_status
                    )
                    if same_candidate:
                        previous_balance_date = str(candidate["remote_balance_date"] or "")
                        # SimpleFIN requires balance-date. Preserve the old
                        # consecutive-check behavior only for an unusable or
                        # absent timestamp; otherwise count each upstream
                        # snapshot once, however often Clerk polls it.
                        if remote_balance_date and previous_balance_date:
                            newer_bank_snapshot = remote_balance_date > previous_balance_date
                            checks = int(candidate["checks"]) + int(newer_bank_snapshot)
                            if not newer_bank_snapshot:
                                candidate_balance_date = previous_balance_date
                        elif remote_balance_date:
                            # The prior observation had no usable timestamp. It
                            # cannot count as distinct evidence, but this one can
                            # become the baseline for the next comparison.
                            checks = int(candidate["checks"])
                        else:
                            checks = int(candidate["checks"]) + 1
                    else:
                        checks = 1
                    first_seen = (
                        float(candidate["first_seen"])
                        if same_candidate
                        else now
                    )
                    if checks < required:
                        connection.execute(
                            "INSERT INTO health_candidates(account_id,status,checks,remote_balance_date,"
                            "first_seen,checked_at) VALUES(?,?,?,?,?,?) "
                            "ON CONFLICT(account_id) DO UPDATE SET "
                            "status=excluded.status,checks=excluded.checks,"
                            "remote_balance_date=excluded.remote_balance_date,"
                            "first_seen=excluded.first_seen,checked_at=excluded.checked_at",
                            (
                                account_id,
                                observed_status,
                                checks,
                                candidate_balance_date,
                                first_seen,
                                now,
                            ),
                        )
                        fallback = str(snapshot.get("status_without_drift") or "ok")
                        effective.update(
                            {
                                "observed_status": observed_status,
                                "status": fallback,
                                "status_label": snapshot.get("status_without_drift_label")
                                or fallback.replace("_", " ").title(),
                                "alerting": bool(snapshot.get("alerting_without_drift", False)),
                                "detail": (
                                    snapshot.get("detail_without_drift")
                                    if fallback != "ok"
                                    else "Clerk saw a possible balance mismatch and is waiting "
                                    f"for {required} successively newer SimpleFIN balance snapshots "
                                    "before declaring it."
                                ),
                                "signals": list(snapshot.get("signals_without_drift") or [])
                                + [
                                    f"Possible balance mismatch seen in {checks} of {required} "
                                    "successively newer SimpleFIN balance snapshots; repeated checks of "
                                    "the same snapshot do not count."
                                ],
                                "balance_mismatch_pending": True,
                                "balance_mismatch_checks": checks,
                                "balance_mismatch_required": required,
                            }
                        )
                    else:
                        connection.execute(
                            "DELETE FROM health_candidates WHERE account_id=?", (account_id,)
                        )
                        effective.update(
                            {
                                "balance_mismatch_pending": False,
                                "balance_mismatch_checks": checks,
                                "balance_mismatch_required": required,
                            }
                        )
                else:
                    connection.execute(
                        "DELETE FROM health_candidates WHERE account_id=?", (account_id,)
                    )

                snapshot.clear()
                snapshot.update(effective)
                status = effective["status"]
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
                    cursor = connection.execute(
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
                            # Carried so a delivered alert can mark its own row,
                            # which is what makes `notified` mean anything.
                            "event_id": cursor.lastrowid,
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
                connection.execute("DELETE FROM health_candidates")
                return
            placeholders = ",".join("?" for _ in account_ids)
            connection.execute(
                f"DELETE FROM account_health WHERE account_id NOT IN ({placeholders})", account_ids
            )
            connection.execute(
                f"DELETE FROM health_candidates WHERE account_id NOT IN ({placeholders})", account_ids
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

    def claim_digest(self, local_date: str, scheduled_for: str = "") -> bool:
        """Reserve one delivery for a date and time, even across restarts."""
        with self.connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO digests(local_date,scheduled_for,created_at) VALUES(?,?,?)",
                    (local_date, scheduled_for, time.time()),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def digest_claimed(self, local_date: str, scheduled_for: str) -> bool:
        """Whether this date and time has already been spent.

        Rows carried over from before scheduled times were recorded have no
        time and so block nothing. They stay in the ledger as history: the
        worst they can cost is a single repeat on the day of the upgrade,
        against permanently forfeiting that day's ability to test at all.
        """

        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM digests WHERE local_date=? AND scheduled_for=? LIMIT 1",
                (local_date, scheduled_for),
            ).fetchone()
        return row is not None

    def complete_digest(
        self,
        local_date: str,
        scheduled_for: str = "",
        payload: dict[str, Any] | None = None,
        *,
        delivered: bool,
        error: str = "",
        receipt: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE digests SET payload_json=?, receipt_json=?, delivered=?, error=? "
                "WHERE local_date=? AND scheduled_for=?",
                (
                    _json(payload or {}),
                    _json(receipt or {}),
                    1 if delivered else 0,
                    error[:500],
                    local_date,
                    scheduled_for,
                ),
            )

    def release_digest(self, local_date: str, scheduled_for: str = "") -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM digests WHERE local_date=? AND scheduled_for=?",
                (local_date, scheduled_for),
            )

    def list_digests(self, limit: int = 10) -> list[dict[str, Any]]:
        """Recent digest claims, delivered or not, newest first.

        A claim exists for every delivery Clerk has reserved, which is what
        makes a digest that never arrived distinguishable from one that never
        ran -- and the receipt says where a delivered one actually went.
        """

        with self.connect() as connection:
            rows = connection.execute(
                "SELECT local_date, scheduled_for, delivered, error, receipt_json, created_at "
                "FROM digests ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{**dict(row), "receipt": json.loads(row["receipt_json"] or "{}")} for row in rows]

    def latest_digest(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM digests WHERE delivered=1 ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        return item

    # -------------------------------------------------------------- dashboard

    # --------------------------------------------------------- bank links

    PLAID_ITEM_FIELDS = (
        "environment",
        "institution_id",
        "institution_name",
        "access_token",
        "cursor",
        "status",
        "last_error",
        "last_refresh_at",
        "last_sync_at",
        "last_successful_update",
    )

    def upsert_plaid_item(self, item: dict[str, Any]) -> dict[str, Any]:
        item_id = str(item.get("item_id") or "")
        if not item_id:
            raise ValueError("A Plaid item id is required")
        now = time.time()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM plaid_items WHERE item_id=?", (item_id,)
            ).fetchone()
            merged = dict(existing) if existing else {"item_id": item_id, "created_at": now}
            for field in self.PLAID_ITEM_FIELDS:
                if field in item:
                    merged[field] = item[field]
            if not merged.get("access_token"):
                raise ValueError("A Plaid access token is required")
            merged["updated_at"] = now
            columns = ["item_id", "created_at", "updated_at", *self.PLAID_ITEM_FIELDS]
            assignments = ", ".join(f"{name}=excluded.{name}" for name in columns[2:])
            connection.execute(
                f"INSERT INTO plaid_items({','.join(columns)}) "
                f"VALUES({','.join('?' for _ in columns)}) "
                f"ON CONFLICT(item_id) DO UPDATE SET {assignments}",
                [merged.get(column) if column in merged else _PLAID_ITEM_DEFAULTS.get(column)
                 for column in columns],
            )
        return self.get_plaid_item(item_id) or merged

    def update_plaid_item(self, item_id: str, **fields: Any) -> dict[str, Any] | None:
        unknown = set(fields) - set(self.PLAID_ITEM_FIELDS)
        if unknown:
            raise ValueError(f"Unknown Plaid item fields: {', '.join(sorted(unknown))}")
        if not fields:
            return self.get_plaid_item(item_id)
        assignments = ", ".join(f"{name}=?" for name in fields)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE plaid_items SET {assignments}, updated_at=? WHERE item_id=?",
                [*fields.values(), time.time(), item_id],
            )
        return self.get_plaid_item(item_id)

    def get_plaid_item(self, item_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM plaid_items WHERE item_id=?", (item_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_plaid_items(self, *, include_removed: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM plaid_items"
        if not include_removed:
            query += " WHERE status != 'removed'"
        with self.connect() as connection:
            rows = connection.execute(query + " ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    def remove_plaid_item(self, item_id: str) -> int:
        """Mark an item removed and disable every link that depended on it."""
        now = time.time()
        with self.connect() as connection:
            connection.execute(
                "UPDATE plaid_items SET status='removed', updated_at=? WHERE item_id=?",
                (now, item_id),
            )
            cursor = connection.execute(
                "UPDATE bank_links SET enabled=0, updated_at=? WHERE item_id=? AND enabled=1",
                (now, item_id),
            )
        return cursor.rowcount

    BANK_LINK_FIELDS = (
        "provider",
        "item_id",
        "external_account_id",
        "external_name",
        "mask",
        "account_type",
        "account_subtype",
        "institution",
        "enabled",
        "cutover_date",
        "last_import_at",
        "last_error",
        "previous_provider",
        "previous_external_id",
    )

    def upsert_bank_link(self, link: dict[str, Any]) -> dict[str, Any]:
        account_id = str(link.get("actual_account_id") or "")
        if not account_id:
            raise ValueError("An Actual account id is required")
        now = time.time()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM bank_links WHERE actual_account_id=?", (account_id,)
            ).fetchone()
            merged = (
                dict(existing)
                if existing
                else {"actual_account_id": account_id, "created_at": now, "enabled": 1}
            )
            for field in self.BANK_LINK_FIELDS:
                if field in link:
                    merged[field] = link[field]
            if not merged.get("provider") or not merged.get("external_account_id"):
                raise ValueError("A bank link needs a provider and an external account id")
            merged["enabled"] = 1 if merged.get("enabled", 1) else 0
            merged["updated_at"] = now
            columns = ["actual_account_id", "created_at", "updated_at", *self.BANK_LINK_FIELDS]
            # An explicit upsert on the primary key: OR REPLACE would silently
            # delete another account's link on a (provider, external id) clash
            # instead of refusing it.
            assignments = ", ".join(f"{name}=excluded.{name}" for name in columns[2:])
            connection.execute(
                f"INSERT INTO bank_links({','.join(columns)}) "
                f"VALUES({','.join('?' for _ in columns)}) "
                f"ON CONFLICT(actual_account_id) DO UPDATE SET {assignments}",
                [merged.get(column) if column in merged else _BANK_LINK_DEFAULTS.get(column)
                 for column in columns],
            )
        return self.get_bank_link(account_id) or merged

    def update_bank_link(self, account_id: str, **fields: Any) -> dict[str, Any] | None:
        unknown = set(fields) - set(self.BANK_LINK_FIELDS)
        if unknown:
            raise ValueError(f"Unknown bank link fields: {', '.join(sorted(unknown))}")
        if not fields:
            return self.get_bank_link(account_id)
        if "enabled" in fields:
            fields["enabled"] = 1 if fields["enabled"] else 0
        assignments = ", ".join(f"{name}=?" for name in fields)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE bank_links SET {assignments}, updated_at=? WHERE actual_account_id=?",
                [*fields.values(), time.time(), account_id],
            )
        return self.get_bank_link(account_id)

    def get_bank_link(self, account_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM bank_links WHERE actual_account_id=?", (account_id,)
            ).fetchone()
        return self._bank_link_row(row) if row else None

    def list_bank_links(
        self, *, provider: str | None = None, enabled_only: bool = False
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if provider:
            clauses.append("provider=?")
            params.append(provider)
        if enabled_only:
            clauses.append("enabled=1")
        query = "SELECT * FROM bank_links"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with self.connect() as connection:
            rows = connection.execute(query + " ORDER BY created_at", params).fetchall()
        return [self._bank_link_row(row) for row in rows]

    def delete_bank_link(self, account_id: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM bank_links WHERE actual_account_id=?", (account_id,)
            )
        return cursor.rowcount > 0

    @staticmethod
    def _bank_link_row(row: sqlite3.Row) -> dict[str, Any]:
        link = dict(row)
        link["enabled"] = bool(link.get("enabled", 1))
        return link

    def record_adoption(
        self,
        *,
        account_id: str,
        transaction_id: str,
        previous_imported_id: str,
        imported_id: str,
        reason: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO import_adoptions(actual_account_id,transaction_id,"
                "previous_imported_id,imported_id,reason,created_at) VALUES(?,?,?,?,?,?)",
                (account_id, transaction_id, previous_imported_id, imported_id, reason, time.time()),
            )

    def list_adoptions(self, *, account_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        query = "SELECT * FROM import_adoptions"
        params: list[Any] = []
        if account_id:
            query += " WHERE actual_account_id=?"
            params.append(account_id)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]


    # ------------------------------------------------- anticipated charges

    SOURCE_FIELDS = (
        "device_id",
        "device_name",
        "package_name",
        "app_label",
        "actual_account_id",
        "account_name",
        "sample_title",
        "sample_text",
        "enabled",
        "last_seen_at",
    )

    def upsert_notification_source(self, source: dict[str, Any]) -> dict[str, Any]:
        """Register one app on one phone, or refresh it if it is already known."""
        device_id = str(source.get("device_id") or "")
        package_name = str(source.get("package_name") or "")
        if not device_id or not package_name:
            raise ValueError("A notification source needs a device id and a package name")
        now = time.time()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM notification_sources WHERE device_id=? AND package_name=?",
                (device_id, package_name),
            ).fetchone()
            merged = (
                dict(existing)
                if existing
                else {"id": str(uuid.uuid4()), "created_at": now, "enabled": 1}
            )
            for field in self.SOURCE_FIELDS:
                if field in source:
                    merged[field] = source[field]
            merged["enabled"] = 1 if merged.get("enabled", 1) else 0
            merged["updated_at"] = now
            columns = ["id", "created_at", "updated_at", *self.SOURCE_FIELDS]
            assignments = ", ".join(f"{name}=excluded.{name}" for name in columns[2:])
            connection.execute(
                f"INSERT INTO notification_sources({','.join(columns)}) "
                f"VALUES({','.join('?' for _ in columns)}) "
                f"ON CONFLICT(id) DO UPDATE SET {assignments}",
                [merged.get(column, "" if column not in ("last_seen_at",) else None) for column in columns],
            )
        return self.get_notification_source(merged["id"]) or merged

    def update_notification_source(self, source_id: str, **fields: Any) -> dict[str, Any] | None:
        unknown = set(fields) - set(self.SOURCE_FIELDS)
        if unknown:
            raise ValueError(f"Unknown source fields: {', '.join(sorted(unknown))}")
        if not fields:
            return self.get_notification_source(source_id)
        if "enabled" in fields:
            fields["enabled"] = 1 if fields["enabled"] else 0
        assignments = ", ".join(f"{name}=?" for name in fields)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE notification_sources SET {assignments}, updated_at=? WHERE id=?",
                [*fields.values(), time.time(), source_id],
            )
            if "actual_account_id" in fields:
                # An open anticipation follows its source to the new account;
                # settled history stays where it was settled.
                connection.execute(
                    "UPDATE anticipated_charges SET actual_account_id=?, updated_at=? "
                    "WHERE source_id=? AND status='open'",
                    (fields["actual_account_id"], time.time(), source_id),
                )
        return self.get_notification_source(source_id)

    def get_notification_source(self, source_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM notification_sources WHERE id=?", (source_id,)
            ).fetchone()
        return self._source_row(row) if row else None

    def find_notification_source(self, device_id: str, package_name: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM notification_sources WHERE device_id=? AND package_name=?",
                (device_id, package_name),
            ).fetchone()
        return self._source_row(row) if row else None

    def list_notification_sources(self, *, device_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM notification_sources"
        params: list[Any] = []
        if device_id is not None:
            query += " WHERE device_id=?"
            params.append(device_id)
        with self.connect() as connection:
            rows = connection.execute(query + " ORDER BY created_at", params).fetchall()
        return [self._source_row(row) for row in rows]

    def delete_notification_source(self, source_id: str) -> bool:
        """Forget a source and every anticipation it produced."""
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM notification_sources WHERE id=?", (source_id,)
            )
        return cursor.rowcount > 0

    @staticmethod
    def _source_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["enabled"] = bool(item.get("enabled", 1))
        return item

    def add_anticipated_charge(self, charge: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Record one notification; the same notification twice is one charge."""
        now = time.time()
        charge_id = str(uuid.uuid4())
        status = str(charge.get("status") or "open")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM anticipated_charges WHERE source_id=? AND notification_key=?",
                (charge["source_id"], charge["notification_key"]),
            ).fetchone()
            if existing:
                connection.commit()
                return dict(existing), False
            connection.execute(
                "INSERT INTO anticipated_charges(id,source_id,actual_account_id,notification_key,"
                "kind,amount_cents,merchant,merchant_key,title,text,noticed_at,noticed_date,status,"
                "resolved_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    charge_id,
                    charge["source_id"],
                    charge.get("actual_account_id", ""),
                    charge["notification_key"],
                    charge.get("kind", "charge"),
                    int(charge.get("amount_cents", 0)),
                    (charge.get("merchant") or "")[:200],
                    charge.get("merchant_key", ""),
                    (charge.get("title") or "")[:400],
                    (charge.get("text") or "")[:2000],
                    float(charge["noticed_at"]),
                    charge["noticed_date"],
                    status,
                    None if status == "open" else now,
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE notification_sources SET last_seen_at=?, updated_at=? WHERE id=?",
                (now, now, charge["source_id"]),
            )
            row = connection.execute(
                "SELECT * FROM anticipated_charges WHERE id=?", (charge_id,)
            ).fetchone()
            connection.commit()
        return dict(row), True

    def get_anticipated_charge(self, charge_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM anticipated_charges WHERE id=?", (charge_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_anticipated_charges(
        self,
        *,
        status: str | None = None,
        source_ids: Sequence[str] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if source_ids is not None:
            if not source_ids:
                return []
            clauses.append(f"source_id IN ({','.join('?' for _ in source_ids)})")
            params.extend(source_ids)
        query = "SELECT * FROM anticipated_charges"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY noticed_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def resolve_anticipated_charge(
        self,
        charge_id: str,
        status: str,
        *,
        matched_transaction_id: str = "",
        matched_payee: str = "",
        matched_date: str = "",
        match_reason: str = "",
    ) -> bool:
        """Close an open anticipation. Only an open one can be closed, once."""
        now = time.time()
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE anticipated_charges SET status=?, matched_transaction_id=?, matched_payee=?, "
                "matched_date=?, match_reason=?, resolved_at=?, updated_at=? "
                "WHERE id=? AND status='open'",
                (
                    status,
                    matched_transaction_id,
                    matched_payee[:200],
                    matched_date,
                    match_reason[:200],
                    now,
                    now,
                    charge_id,
                ),
            )
        return cursor.rowcount > 0

    def set_anticipated_category(
        self,
        charge_id: str,
        *,
        category_id: str,
        category_name: str,
        source: str,
        confidence: float = 0.0,
    ) -> bool:
        """Record the provisional category of one anticipation."""
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE anticipated_charges SET category_id=?, category_name=?, category_source=?, "
                "category_confidence=?, updated_at=? WHERE id=?",
                (category_id, category_name[:200], source, float(confidence), time.time(), charge_id),
            )
        return cursor.rowcount > 0

    def upsert_alias(
        self,
        alias_key: str,
        merchant_key: str,
        *,
        alias_label: str = "",
        merchant_label: str = "",
        source: str = "settled",
    ) -> dict[str, Any] | None:
        """Remember that a notification's merchant posts under another key.

        A taught alias is a statement of intent and is not overwritten by a
        later settlement; a settled one yields to whatever settles next.
        """
        if not alias_key or not merchant_key or alias_key == merchant_key:
            return None
        now = time.time()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO merchant_aliases(alias_key,merchant_key,alias_label,merchant_label,source,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(alias_key) DO UPDATE SET "
                "merchant_key=CASE WHEN merchant_aliases.source='taught' AND excluded.source!='taught' "
                "THEN merchant_aliases.merchant_key ELSE excluded.merchant_key END, "
                "alias_label=CASE WHEN excluded.alias_label!='' THEN excluded.alias_label ELSE merchant_aliases.alias_label END, "
                "merchant_label=CASE WHEN excluded.merchant_label!='' THEN excluded.merchant_label ELSE merchant_aliases.merchant_label END, "
                "source=CASE WHEN merchant_aliases.source='taught' AND excluded.source!='taught' "
                "THEN 'taught' ELSE excluded.source END, updated_at=excluded.updated_at",
                (alias_key, merchant_key, alias_label[:120], merchant_label[:120], source, now, now),
            )
            row = connection.execute(
                "SELECT * FROM merchant_aliases WHERE alias_key=?", (alias_key,)
            ).fetchone()
        return dict(row) if row else None

    def learn_aliases(self, pairs: Mapping[str, Mapping[str, str]], *, source: str) -> int:
        """Absorb aliases read from evidence, never over a taught one, never as a chain.

        Aliases of this source are derived, so ones the evidence no longer
        supports are dropped first: the table follows the budget rather than
        accumulating every pair it ever saw.
        """
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM merchant_aliases WHERE source=? AND alias_key NOT IN "
                f"({','.join('?' for _ in pairs) or "''"})",
                (source, *pairs.keys()),
            )
        if not pairs:
            return 0
        learned = 0
        with self.connect() as connection:
            existing_targets = {
                row["merchant_key"]
                for row in connection.execute("SELECT merchant_key FROM merchant_aliases")
            }
            alias_keys = {
                row["alias_key"] for row in connection.execute("SELECT alias_key FROM merchant_aliases")
            }
        for alias_key, entry in pairs.items():
            target = str(entry.get("merchant_key") or "")
            if not target or alias_key == target:
                continue
            # The new alias must not point at a key that is itself an alias,
            # nor turn an existing target into an alias.
            if target in alias_keys or alias_key in existing_targets:
                continue
            if self.upsert_alias(
                alias_key,
                target,
                alias_label=str(entry.get("alias_label") or ""),
                merchant_label=str(entry.get("merchant_label") or ""),
                source=source,
            ):
                learned += 1
        return learned

    def list_aliases(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM merchant_aliases ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def alias_map(self) -> dict[str, str]:
        return {row["alias_key"]: row["merchant_key"] for row in self.list_aliases()}

    def delete_alias(self, alias_key: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM merchant_aliases WHERE alias_key=?", (alias_key,)
            )
        return cursor.rowcount > 0

    def reopen_anticipated_charge(self, charge_id: str) -> bool:
        now = time.time()
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE anticipated_charges SET status='open', matched_transaction_id='', "
                "matched_payee='', matched_date='', match_reason='', resolved_at=NULL, updated_at=? "
                "WHERE id=? AND status IN ('matched','expired','dismissed')",
                (now, charge_id),
            )
        return cursor.rowcount > 0

    def matched_transaction_ids(self) -> set[str]:
        """Transactions already claimed by a settled anticipation."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT matched_transaction_id FROM anticipated_charges "
                "WHERE status='matched' AND matched_transaction_id != ''"
            ).fetchall()
        return {row["matched_transaction_id"] for row in rows}

    def delete_anticipated_charge(self, charge_id: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM anticipated_charges WHERE id=?", (charge_id,)
            )
        return cursor.rowcount > 0

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
            proposals = connection.execute(
                "SELECT COUNT(*) FROM proposals WHERE status='open'"
            ).fetchone()[0]
            rules = connection.execute(
                "SELECT COUNT(*) FROM merchant_rules WHERE status='active'"
            ).fetchone()[0]
            degraded = connection.execute(
                "SELECT COUNT(*) FROM account_health WHERE status NOT IN ('ok','not_linked','muted')"
            ).fetchone()[0]
            anticipated = connection.execute(
                "SELECT COUNT(*) FROM anticipated_charges WHERE status='open' AND kind='charge'"
            ).fetchone()[0]
        return {
            "active_jobs": sum(jobs.get(status, 0) for status in ACTIVE_STATUSES),
            "failed_jobs": jobs.get("failed", 0),
            "needs_review": needs_review,
            "applied_today": applied_today,
            "proposals": proposals,
            "rules": rules,
            "degraded_accounts": degraded,
            "anticipated_open": anticipated,
        }
