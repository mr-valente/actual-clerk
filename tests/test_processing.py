from __future__ import annotations

import asyncio
import datetime
from typing import Any

import pytest

from actual_clerk import processing
from actual_clerk.clients.actual import ActualGatewayError
from actual_clerk.clients.simplefin import SimpleFinError
from actual_clerk.processing import OVERVIEW_SNAPSHOT, JobManager, digest_is_due

from .factories import account, snapshot, transaction

TODAY = datetime.date(2026, 8, 21)


class StubGateway:
    def __init__(self, snap: dict[str, Any]):
        self.snap = snap
        self.updates: list[dict[str, Any]] = []
        self.tags: list[list[dict[str, Any]]] = []
        self.bank_sync_calls = 0
        self.pull_calls = 0
        self.bank_sync_error: Exception | None = None
        self.apply_error: Exception | None = None
        self.skipped: list[dict[str, str]] = []
        self.applied_ids: list[str] | None = None
        self.snapshot_transaction_ids: list[set[str]] = []

    def settings_changed(self):
        pass

    def status(self):
        return {"connected": True, "last_error": "", "last_connected_at": None}

    async def snapshot(self, *, today=None, transaction_ids=()):
        requested = set(transaction_ids)
        self.snapshot_transaction_ids.append(requested)
        return {
            **self.snap,
            "review_transactions": [
                item for item in self.snap["transactions"] if item["id"] in requested
            ],
        }

    async def bank_sync(self, *, run_rules=True):
        self.bank_sync_calls += 1
        if self.bank_sync_error is not None:
            raise self.bank_sync_error
        return {"imported": 2, "accounts": ["Checking"]}

    async def pull(self):
        self.pull_calls += 1
        return {"changes": 0}

    async def apply_updates(self, updates, *, overwrite: bool = False):
        if self.apply_error is not None:
            raise self.apply_error
        self.updates.extend(updates)
        applied = (
            self.applied_ids
            if self.applied_ids is not None
            else [item["transaction_id"] for item in updates]
        )
        return {"applied": applied, "skipped": self.skipped}

    async def ensure_tags(self, catalog):
        self.tags.append(list(catalog))
        return [entry["tag"] for entry in catalog]


def budget_snapshot(extra_transactions=()):
    # Deliberately irregular visits: this fixture is about filing, and a
    # perfectly monthly series would also pick up a #subscription tag.
    history = [
        transaction(
            TODAY - datetime.timedelta(days=days),
            -650,
            payee="Blue Bottle Coffee",
            category_id="cat-coffee",
            category_name="Coffee",
        )
        for days in (3, 11, 26)
    ]
    pending = [transaction(TODAY, -700, payee="Blue Bottle Coffee", transaction_id="txn-new")]
    return snapshot(
        accounts=[account("Checking", balance_cents=250000)],
        transactions=history + pending + list(extra_transactions),
        budgeted={"cat-rent": 180000},
    )


@pytest.fixture
def manager(database, settings_manager):
    settings_manager.update({"actual_password": "secret", "actual_budget_id": "budget-1"})
    gateway = StubGateway(budget_snapshot())
    manager = JobManager(database, settings_manager, gateway)
    manager.gateway = gateway
    return manager


@pytest.fixture
def delivers(monkeypatch):
    """Accept every notification without leaving the machine."""
    sent: list[dict[str, Any]] = []

    async def publish(self, **kwargs):
        sent.append(kwargs)
        return {"id": f"msg-{len(sent)}", "time": 1755940211}

    monkeypatch.setattr("actual_clerk.clients.ntfy.NtfyClient.publish", publish)
    return sent


async def run_job(manager, kind, params=None):
    job, _ = await manager.enqueue(kind, params=params)
    claimed = manager.database.claim_job("test-worker", 600)
    await manager._run_job(claimed)
    return manager.database.get_job(job["id"], include_events=True)


# ------------------------------------------------------------------ sync job


async def test_a_sync_imports_reads_and_then_queues_filing(manager, settings_manager):
    job = await run_job(manager, "sync")
    assert job["status"] == "completed"
    assert manager.gateway.bank_sync_calls == 1
    assert job["result"]["imported"] == 2
    assert job["result"]["accounts"] == 1
    # The overview is rebuilt so the dashboard is current straight away.
    current_month = datetime.datetime.now(datetime.UTC).strftime("%Y-%m")
    assert manager.database.get_snapshot(OVERVIEW_SNAPSHOT)["budget"]["month"] == current_month
    # Filing follows automatically.
    assert manager.database.list_jobs(kind="categorize")
    # Health is scored only after the native bank sync and fresh snapshot.
    assert manager.database.list_jobs(kind="health")


async def test_bank_sync_can_be_left_to_something_else(manager, settings_manager):
    settings_manager.update({"bank_sync_enabled": False})
    await run_job(manager, "sync")
    assert manager.gateway.bank_sync_calls == 0
    assert manager.gateway.pull_calls == 1


async def test_a_failed_bank_sync_does_not_lose_the_rest_of_the_run(manager):
    manager.gateway.bank_sync_error = ActualGatewayError("bank sync timed out", retryable=True)
    job = await run_job(manager, "sync")
    assert job["status"] == "completed"
    assert "timed out" in job["result"]["bank_sync_error"]
    assert manager.database.get_snapshot(OVERVIEW_SNAPSHOT) is not None
    assert any(event["event_type"] == "bank_sync_failed" for event in job["events"])


async def test_filing_is_not_queued_when_it_is_switched_off(manager, settings_manager):
    settings_manager.update({"categorization_enabled": False})
    await run_job(manager, "sync")
    assert manager.database.list_jobs(kind="categorize") == []


async def test_sync_retires_reviews_deleted_converted_or_categorized_in_actual(manager):
    categorized = transaction(
        TODAY,
        -1200,
        payee="Settled Shop",
        transaction_id="txn-categorized",
        category_id="cat-groceries",
        category_name="Groceries",
    )
    transfer = transaction(
        TODAY,
        -50000,
        payee="Transfer",
        transaction_id="txn-transfer",
        is_transfer=True,
    )
    manager.gateway.snap = budget_snapshot([categorized, transfer])
    for transaction_id in ("txn-categorized", "txn-transfer", "txn-deleted", "txn-new"):
        manager.database.add_decision(
            {
                "transaction_id": transaction_id,
                "source": "model",
                "status": "needs_review",
                "rationale": {"reason": "awaiting approval"},
            }
        )

    job = await run_job(manager, "sync")

    assert job["result"]["reviews_resolved"] == 3
    assert job["result"]["review_resolutions"] == {
        "categorized": 1,
        "transfer": 1,
        "deleted": 1,
    }
    assert manager.database.open_review_transaction_ids() == {"txn-new"}
    decisions = {
        item["transaction_id"]: item for item in manager.database.list_decisions(limit=20)
    }
    assert decisions["txn-categorized"]["rationale"]["external_resolution"] == "categorized"
    assert decisions["txn-transfer"]["rationale"]["external_resolution"] == "transfer"
    assert decisions["txn-deleted"]["rationale"]["external_resolution"] == "deleted"
    assert decisions["txn-new"]["status"] == "needs_review"
    assert manager.gateway.snapshot_transaction_ids[0] == {
        "txn-categorized",
        "txn-transfer",
        "txn-deleted",
        "txn-new",
    }
    [event] = [item for item in job["events"] if item["event_type"] == "reviews_reconciled"]
    assert event["data"]["resolved"] == 3


async def test_review_retry_reconciles_a_transfer_without_reclassifying_it(
    manager, settings_manager
):
    manager.gateway.snap = budget_snapshot(
        [
            transaction(
                TODAY,
                -50000,
                payee="Transfer",
                transaction_id="txn-transfer",
                is_transfer=True,
            )
        ]
    )
    manager.database.add_decision(
        {
            "transaction_id": "txn-transfer",
            "source": "model",
            "status": "needs_review",
        }
    )

    job = await run_job(manager, "categorize", params={"reviews": True})

    assert job["result"]["review_retry"] is True
    assert job["result"]["considered"] == 0
    assert job["result"]["reviews_resolved"] == 1
    assert manager.database.open_review_transaction_ids() == set()
    assert manager.gateway.updates == []


# ------------------------------------------------------------ categorize job


async def test_filing_writes_to_actual_and_records_what_it_did(manager, settings_manager):
    settings_manager.update({"ai_enabled": False})
    job = await run_job(manager, "categorize")

    assert job["status"] == "completed"
    assert job["result"]["applied"] == 1
    assert manager.gateway.updates == [
        {"transaction_id": "txn-new", "category_id": "cat-coffee", "add_tags": ["clerk"]}
    ]
    [recorded] = manager.database.list_decisions()
    assert recorded["status"] == "applied"
    assert recorded["source"] == "memory"
    # The applied decision is remembered for next time.
    assert manager.database.memory_for("blue bottle coffee")[0]["hits"] == 1


async def test_only_the_tags_actually_written_are_registered_in_actual(manager, settings_manager):
    settings_manager.update({"ai_enabled": False})
    await run_job(manager, "categorize")
    [catalog] = manager.gateway.tags
    assert [entry["tag"] for entry in catalog] == ["clerk"]
    assert all(entry["color"].startswith("#") for entry in catalog)


async def test_a_transaction_categorized_in_actual_first_is_not_overwritten(
    manager, settings_manager
):
    settings_manager.update({"ai_enabled": False})
    manager.gateway.applied_ids = []
    manager.gateway.skipped = [{"id": "txn-new", "reason": "already_categorized"}]

    job = await run_job(manager, "categorize")
    [recorded] = manager.database.list_decisions()
    assert recorded["status"] == "skipped"
    assert recorded["rationale"]["skipped_reason"] == "already_categorized"
    assert job["result"]["written"] == 0
    # Nothing was written, so nothing was learned.
    assert manager.database.memory_for("blue bottle coffee") == []


async def test_a_write_failure_fails_the_run_for_retry(manager, settings_manager):
    settings_manager.update({"ai_enabled": False})
    manager.gateway.apply_error = ActualGatewayError("Actual is down", retryable=True)
    job = await run_job(manager, "categorize")
    assert job["status"] == "retry_wait"
    assert manager.database.list_decisions() == []


async def test_filing_that_is_switched_off_does_nothing(manager, settings_manager):
    settings_manager.update({"categorization_enabled": False})
    job = await run_job(manager, "categorize")
    assert job["result"] == {"skipped": "categorization is disabled"}
    assert manager.gateway.updates == []


async def test_a_settled_merchant_is_promoted_to_a_rule(manager, settings_manager):
    settings_manager.update({"ai_enabled": False, "rule_promote_after": 2})
    manager.database.record_memory("blue bottle coffee", "cat-coffee", "Coffee")
    job = await run_job(manager, "categorize")
    assert job["result"]["rule_suggestions"] == 1
    [suggestion] = manager.database.list_rule_suggestions()
    assert suggestion["category_id"] == "cat-coffee"
    assert suggestion["match_value"] == "Blue Bottle Coffee"


async def test_rule_promotion_can_be_switched_off(manager, settings_manager):
    settings_manager.update({"ai_enabled": False, "rule_promotion_enabled": False})
    job = await run_job(manager, "categorize")
    assert job["result"]["rule_suggestions"] == 0
    assert manager.database.list_rule_suggestions() == []


# ---------------------------------------------------------------- health job


async def test_a_health_check_without_simplefin_still_scores_the_accounts(manager):
    job = await run_job(manager, "health")
    assert job["status"] == "completed"
    assert job["result"]["linked"] == 1
    [health] = manager.database.health_snapshots()
    assert health["account_name"] == "Checking"


async def test_a_simplefin_outage_is_recorded_but_does_not_fail_the_check(
    manager, settings_manager, monkeypatch
):
    settings_manager.update(
        {
            "simplefin_access_url": "https://user:pass@bridge/simplefin",
            "transaction_stale_days": 365,
        }
    )

    async def broken_fetch(self, **kwargs):
        raise SimpleFinError("bridge unreachable", retryable=True)

    monkeypatch.setattr("actual_clerk.clients.simplefin.SimpleFinClient.fetch", broken_fetch)
    job = await run_job(manager, "health")
    assert job["status"] == "completed"
    assert job["result"]["simplefin_error"] == "bridge unreachable"
    assert any(event["event_type"] == "simplefin_failed" for event in job["events"])


async def test_a_status_change_is_recorded_once(manager):
    await run_job(manager, "health")
    first = manager.database.list_health_events()
    second_job = await run_job(manager, "health")
    assert second_job["result"]["transitions"] == 0
    assert len(manager.database.list_health_events()) == len(first)


async def test_the_health_job_confirms_a_balance_mismatch_over_three_runs(
    manager, settings_manager, monkeypatch
):
    settings_manager.update(
        {
            "simplefin_access_url": "https://user:pass@bridge/simplefin",
            "transaction_stale_days": 365,
        }
    )
    remote_balance = {"cents": 250000}

    async def fetch(self, **kwargs):
        return {
            "accounts": [
                {
                    "id": "sf-acct-checking",
                    "name": "Checking",
                    "balance_cents": remote_balance["cents"],
                    "balance_date": datetime.datetime.now(datetime.UTC),
                }
            ],
            "errors": [],
        }

    monkeypatch.setattr("actual_clerk.clients.simplefin.SimpleFinClient.fetch", fetch)
    await run_job(manager, "health")
    assert manager.database.health_snapshots()[0]["status"] == "ok"

    remote_balance["cents"] = 240000
    first = await run_job(manager, "health")
    second = await run_job(manager, "health")
    assert first["result"]["degraded"] == 0
    assert second["result"]["degraded"] == 0
    assert manager.database.health_snapshots()[0]["balance_mismatch_checks"] == 2

    third = await run_job(manager, "health")
    assert third["result"]["degraded"] == 1
    assert third["result"]["transitions"] == 1
    assert manager.database.health_snapshots()[0]["status"] == "drifted"


# ---------------------------------------------------------------- digest job


async def test_a_digest_is_built_even_when_notifications_are_off(manager):
    job = await run_job(manager, "digest")
    assert job["result"] == {
        "delivered": False,
        "forced": False,
        "reason": "notifications are disabled",
    }
    # The claim is kept so the same day is not attempted again and again.
    assert manager.database.latest_digest() is None


async def test_a_digest_is_sent_at_most_once_a_day(manager, settings_manager, monkeypatch):
    settings_manager.update(
        {"notifications_enabled": True, "ntfy_topic": "clerk-test", "ntfy_url": "https://ntfy.sh"}
    )
    sent = []

    async def publish(self, **kwargs):
        sent.append(kwargs)
        return {"id": "1"}

    monkeypatch.setattr("actual_clerk.clients.ntfy.NtfyClient.publish", publish)
    first = await run_job(manager, "digest")
    second = await run_job(manager, "digest")

    assert first["result"]["delivered"] is True
    assert second["result"]["skipped"].startswith("already sent for")
    assert len(sent) == 1
    assert manager.database.latest_digest()["payload"]["title"] == sent[0]["title"]


async def test_a_delivered_digest_records_where_it_actually_went(
    manager, settings_manager, monkeypatch
):
    """"Delivered" cannot distinguish a watched topic from an unwatched one."""
    settings_manager.update(
        {
            "notifications_enabled": True,
            "ntfy_topic": "clerk-test",
            "ntfy_url": "https://ntfy.sh",
        }
    )

    async def publish(self, **kwargs):
        return {"id": "RxIFhE7bqQ0k", "time": 1755940211}

    monkeypatch.setattr("actual_clerk.clients.ntfy.NtfyClient.publish", publish)
    job = await run_job(manager, "digest")

    assert job["result"]["topic"] == "clerk-test"
    assert job["result"]["message_id"] == "RxIFhE7bqQ0k"
    assert manager.database.list_digests()[0]["receipt"] == {
        "server": "https://ntfy.sh",
        "topic": "clerk-test",
        "id": "RxIFhE7bqQ0k",
        "at": 1755940211,
    }


async def test_a_digest_that_could_not_be_delivered_may_be_retried(
    manager, settings_manager, monkeypatch
):
    settings_manager.update({"notifications_enabled": True, "ntfy_topic": "clerk-test"})

    async def broken(self, **kwargs):
        raise RuntimeError("ntfy refused the message")

    monkeypatch.setattr("actual_clerk.clients.ntfy.NtfyClient.publish", broken)
    job = await run_job(manager, "digest")
    assert job["status"] == "retry_wait"
    # The claim was released, so a retry at the same time is not blocked either.
    slot = settings_manager.get().digest_time
    assert manager.database.claim_digest(datetime.date.today().isoformat(), slot) is True


# ----------------------------------------------------------------- scheduler


async def test_work_is_scheduled_once_per_interval(manager, settings_manager):
    settings_manager.update({"sync_interval_minutes": 60, "health_interval_minutes": 60})
    manager._schedule_due_work()
    assert len(manager.database.list_jobs(kind="sync")) == 1
    assert len(manager.database.list_jobs(kind="health")) == 1
    # A queued job is not queued again.
    manager._schedule_due_work()
    assert len(manager.database.list_jobs(kind="sync")) == 1


async def test_a_recent_completion_defers_the_next_run(manager, settings_manager):
    settings_manager.update({"sync_interval_minutes": 60})
    job, _ = await manager.enqueue("sync")
    manager.database.finish_job(job["id"])
    manager._schedule_due_work()
    assert len(manager.database.list_jobs(kind="sync")) == 1


async def test_scheduled_syncing_can_be_switched_off(manager, settings_manager):
    settings_manager.update({"sync_enabled": False})
    manager._schedule_due_work()
    assert manager.database.list_jobs(kind="sync") == []
    # Connection checks keep running: a dead link is worth knowing about.
    assert len(manager.database.list_jobs(kind="health")) == 1


def _local_offset(settings_manager, minutes: int) -> str:
    settings = settings_manager.get()
    moment = datetime.datetime.now(settings.zone) + datetime.timedelta(minutes=minutes)
    return moment.strftime("%H:%M")


async def test_a_digest_is_only_scheduled_after_its_time_of_day(manager, settings_manager):
    settings_manager.update(
        {"notifications_enabled": True, "ntfy_topic": "clerk-test", "digest_enabled": True}
    )
    # Still an hour away.
    settings_manager.update({"digest_time": _local_offset(settings_manager, 60)})
    manager._schedule_due_work()
    assert manager.database.list_jobs(kind="digest") == []

    # Due five minutes ago.
    settings_manager.update({"digest_time": _local_offset(settings_manager, -5)})
    manager._schedule_due_work()
    assert len(manager.database.list_jobs(kind="digest")) == 1


async def test_moving_the_digest_time_asks_for_a_delivery_at_the_new_time(
    manager, settings_manager
):
    """One per date and time, so the scheduled path is testable today.

    A date that allowed exactly one delivery could only be tried again
    tomorrow, which made a digest that never arrived impossible to chase.
    """

    settings_manager.update(
        {"notifications_enabled": True, "ntfy_topic": "clerk-test", "digest_enabled": True}
    )
    first = _local_offset(settings_manager, -5)
    settings_manager.update({"digest_time": first})
    today = datetime.datetime.now(settings_manager.get().zone).date().isoformat()
    manager.database.claim_digest(today, first)

    # That delivery is spent, and no amount of ticking produces another.
    manager._schedule_due_work()
    manager._schedule_due_work()
    assert manager.database.list_jobs(kind="digest") == []

    # Moving the time asks for a fresh one, on the same day.
    settings_manager.update({"digest_time": _local_offset(settings_manager, -2)})
    manager._schedule_due_work()
    assert len(manager.database.list_jobs(kind="digest")) == 1


@pytest.mark.parametrize(
    ("hour", "minute", "due"),
    [
        (7, 29, False),  # a minute early
        (7, 30, True),   # exactly on time
        (10, 0, True),   # a late start, still this morning
        (13, 29, True),  # the last minute of the window
        (13, 30, False), # the window has closed
        (23, 0, False),  # no late-night 'good morning'
        (2, 0, False),   # before the digest time, not after yesterday's
    ],
)
def test_the_digest_window_opens_and_closes(hour, minute, due):
    now = datetime.datetime(2026, 8, 21, hour, minute, tzinfo=datetime.UTC)
    assert digest_is_due(now, datetime.time(7, 30), window_hours=6) is due


async def test_the_scheduler_leaves_nothing_behind_on_each_tick(manager, monkeypatch):
    """A container runs this loop three times a minute for months.

    Waiting on the stop flag under a shield left one task parked on it per
    tick, none of which could ever be collected, so the leak grew for as long
    as the container stayed up. Cancelling an Event wait is safe: it discards
    the waiter and never the flag.
    """

    monkeypatch.setattr(processing, "SCHEDULER_TICK_SECONDS", 0.01)
    monkeypatch.setattr(manager, "_schedule_due_work", lambda: None)
    settled = len(asyncio.all_tasks())

    loop = asyncio.create_task(manager._scheduler_loop())
    await asyncio.sleep(0.3)  # roughly thirty ticks
    stranded = len(asyncio.all_tasks()) - settled - 1  # less the loop itself

    manager._stopping.set()
    await asyncio.wait_for(loop, timeout=1)
    assert stranded == 0


async def test_an_unknown_job_kind_fails_without_stopping_the_worker(manager):
    job, _ = manager.database.enqueue_job("nonsense", 3)
    claimed = manager.database.claim_job("test-worker", 600)
    await manager._run_job(claimed)
    stored = manager.database.get_job(job["id"])
    assert stored["status"] == "failed"
    assert stored["error_code"] == "unknown_kind"


async def test_nothing_is_scheduled_before_actual_is_connected(database, settings_manager):
    manager = JobManager(database, settings_manager, StubGateway(budget_snapshot()))
    manager._schedule_due_work()
    assert database.list_jobs() == []
    # A manual run is still allowed, so the real error is one click away.
    await manager.enqueue("sync")
    assert len(database.list_jobs(kind="sync")) == 1


# ------------------------------------------------------------ history catch-up


async def old_and_new_snapshot():
    """One recent uncategorized charge and one from well outside the window."""
    return snapshot(
        accounts=[account("Checking")],
        transactions=[
            transaction(TODAY - datetime.timedelta(days=days), -650, payee="Blue Bottle Coffee",
                        category_id="cat-coffee", category_name="Coffee")
            for days in (3, 11, 26)
        ]
        + [
            transaction(TODAY, -700, payee="Blue Bottle Coffee", transaction_id="txn-recent"),
            transaction(TODAY - datetime.timedelta(days=400), -700,
                        payee="Blue Bottle Coffee", transaction_id="txn-ancient"),
        ],
    )


async def test_an_ordinary_run_only_reaches_back_over_the_recent_window(
    database, settings_manager
):
    settings_manager.update(
        {"actual_password": "x", "actual_budget_id": "b", "ai_enabled": False}
    )
    gateway = StubGateway(await old_and_new_snapshot())
    manager = JobManager(database, settings_manager, gateway)
    job = await run_job(manager, "categorize")

    assert job["result"]["lookback_days"] == 45
    assert job["result"]["full_history"] is False
    assert [update["transaction_id"] for update in gateway.updates] == ["txn-recent"]


async def test_a_catch_up_run_reaches_the_whole_retained_history(database, settings_manager):
    settings_manager.update(
        {"actual_password": "x", "actual_budget_id": "b", "ai_enabled": False}
    )
    gateway = StubGateway(await old_and_new_snapshot())
    manager = JobManager(database, settings_manager, gateway)

    await manager.enqueue("categorize", params={"full": True})
    claimed = database.claim_job("test-worker", 600)
    await manager._run_job(claimed)
    job = database.get_job(claimed["id"])

    assert job["result"]["lookback_days"] == 730
    assert job["result"]["full_history"] is True
    assert sorted(update["transaction_id"] for update in gateway.updates) == [
        "txn-ancient",
        "txn-recent",
    ]


async def test_a_review_retry_revisits_only_open_reviews_even_when_old(
    database, settings_manager
):
    settings_manager.update(
        {"actual_password": "x", "actual_budget_id": "b", "ai_enabled": False}
    )
    gateway = StubGateway(await old_and_new_snapshot())
    manager = JobManager(database, settings_manager, gateway)
    database.add_decision(
        {
            "transaction_id": "txn-ancient",
            "merchant_key": "blue bottle coffee",
            "status": "needs_review",
            "source": "unresolved",
        }
    )

    job = await run_job(manager, "categorize", params={"reviews": True})

    assert job["result"]["review_retry"] is True
    assert job["result"]["full_history"] is False
    assert [update["transaction_id"] for update in gateway.updates] == ["txn-ancient"]
    assert database.get_decision(database.list_decisions()[-1]["id"])["status"] == "superseded"


# ---------------------------------------------------------------- monitoring


async def test_an_unmonitored_account_is_muted_by_the_health_check(manager):
    manager.database.set_monitoring("acct-checking", False, "Checking")
    job = await run_job(manager, "health")

    [health] = manager.database.health_snapshots()
    assert health["status"] == "muted"
    assert health["monitored"] is False
    assert job["result"]["degraded"] == 0
    assert job["result"]["muted"] == 1


async def test_an_unmonitored_account_is_never_called_stale(manager):
    manager.database.set_monitoring("acct-checking", False, "Checking")
    await run_job(manager, "sync")

    overview = manager.database.get_snapshot(OVERVIEW_SNAPSHOT)
    assert overview["freshness"]["stale_accounts"] == 0
    assert overview["freshness"]["up_to_date"] is True
    assert overview["accounts"][0]["monitored"] is False


async def test_a_forced_digest_does_not_spend_todays_delivery(manager, settings_manager, delivers):
    """The rehearsal must leave the real morning report still to come."""
    settings_manager.update(
        {"notifications_enabled": True, "ntfy_topic": "clerk-test", "ntfy_url": "https://ntfy.sh"}
    )
    rehearsal = await run_job(manager, "digest", {"force": True})
    assert rehearsal["result"]["forced"] is True

    morning = await run_job(manager, "digest")
    assert "skipped" not in morning["result"], "the morning report must still go out"
    assert morning["result"]["forced"] is False


async def test_a_forced_digest_can_be_repeated(manager, settings_manager, delivers):
    settings_manager.update(
        {"notifications_enabled": True, "ntfy_topic": "clerk-test", "ntfy_url": "https://ntfy.sh"}
    )
    for _ in range(3):
        job = await run_job(manager, "digest", {"force": True})
        assert "skipped" not in job["result"]


async def test_an_unforced_digest_is_still_once_a_day(manager, settings_manager, delivers):
    settings_manager.update(
        {"notifications_enabled": True, "ntfy_topic": "clerk-test", "ntfy_url": "https://ntfy.sh"}
    )
    await run_job(manager, "digest")
    again = await run_job(manager, "digest")
    assert again["result"]["skipped"].startswith("already sent for")
