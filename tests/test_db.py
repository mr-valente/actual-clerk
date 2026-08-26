from __future__ import annotations

import sqlite3
import time

from actual_clerk.db import Database


def decision(**kwargs):
    base = {
        "transaction_id": "txn-1",
        "account_id": "acct-1",
        "account_name": "Checking",
        "payee_name": "Blue Bottle",
        "merchant_key": "blue bottle",
        "transaction_date": "2026-08-21",
        "amount_cents": -650,
        "source": "memory",
        "status": "applied",
        "category_id": "cat-coffee",
        "category_name": "Coffee",
        "confidence": 0.9,
        "tags": ["subscription"],
        "rationale": {"reason": "history"},
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------- jobs


def test_a_second_request_for_a_running_kind_is_a_duplicate(database):
    first, created_first = database.enqueue_job("sync", 3)
    second, created_second = database.enqueue_job("sync", 3)
    assert created_first is True
    assert created_second is False
    assert second["id"] == first["id"]
    # A different kind still queues.
    _, created_other = database.enqueue_job("health", 3)
    assert created_other is True


def test_claiming_moves_a_job_to_running_exactly_once(database):
    database.enqueue_job("sync", 3)
    claimed = database.claim_job("worker-a", 60)
    assert claimed["status"] == "running"
    assert claimed["attempt"] == 1
    assert database.claim_job("worker-b", 60) is None


def test_an_expired_lease_is_reclaimed(database):
    database.enqueue_job("sync", 3)
    database.claim_job("worker-a", lease_seconds=-1)
    reclaimed = database.claim_job("worker-b", 60)
    assert reclaimed is not None
    assert reclaimed["phase"] == "starting"
    assert reclaimed["attempt"] == 2


def test_a_crashed_run_is_requeued_on_startup(database, data_dir):
    database.enqueue_job("sync", 3)
    database.claim_job("worker-a", 600)
    reopened = Database(data_dir / "clerk.db")
    reopened.initialize()
    [job] = reopened.list_jobs(kind="sync")
    assert job["status"] == "queued"
    assert job["phase"] == "recovered"


def test_a_retryable_failure_waits_then_a_terminal_one_gives_up(database):
    database.enqueue_job("sync", 2)
    job = database.claim_job("worker", 60)
    assert database.fail_or_retry(job["id"], "boom", "temporary", True) == "retry_wait"
    stored = database.get_job(job["id"])
    assert stored["next_run_at"] > time.time()
    database.claim_job("worker", 60)  # not due yet, so nothing is claimed
    assert database.fail_or_retry(job["id"], "boom", "permanent", False) == "failed"


def test_retries_stop_at_the_attempt_limit(database):
    database.enqueue_job("sync", 1)
    job = database.claim_job("worker", 60)
    assert database.fail_or_retry(job["id"], "boom", "temporary", True) == "failed"


def test_a_finished_job_can_be_retried_but_not_duplicated(database):
    database.enqueue_job("sync", 3)
    job = database.claim_job("worker", 60)
    database.finish_job(job["id"], status="failed", phase="failed")
    assert database.retry_job(job["id"]) is not None
    # It is active again, so a second retry is refused.
    assert database.retry_job(job["id"]) is None


def test_only_queued_jobs_can_be_cancelled(database):
    job, _ = database.enqueue_job("sync", 3)
    assert database.cancel_job(job["id"]) is True
    assert database.cancel_job(job["id"]) is False


def test_job_results_and_events_round_trip(database):
    job, _ = database.enqueue_job("categorize", 3)
    database.add_event(job["id"], "info", "started", "working", {"n": 1})
    database.finish_job(job["id"], result={"applied": 4})
    stored = database.get_job(job["id"], include_events=True)
    assert stored["result"] == {"applied": 4}
    assert stored["events"][0]["data"] == {"n": 1}
    assert database.last_job("categorize", "completed")["id"] == job["id"]


def test_jobs_can_be_listed_by_state_and_kind(database):
    database.enqueue_job("sync", 3)
    database.enqueue_job("health", 3)
    assert len(database.list_jobs(status="active")) == 2
    assert len(database.list_jobs(kind="sync")) == 1
    assert database.list_jobs(status="completed") == []


# ----------------------------------------------------------------- decisions


def test_a_new_proposal_supersedes_an_open_review(database):
    database.add_decision(decision(status="needs_review"))
    database.add_decision(decision(status="needs_review", confidence=0.5))
    open_reviews = database.list_decisions(status="needs_review")
    assert len(open_reviews) == 1
    assert len(database.list_decisions(status="superseded")) == 1


def test_resolving_a_review_is_atomic(database):
    database.add_decision(decision(status="needs_review"))
    [review] = database.list_decisions(status="needs_review")
    assert database.resolve_decision(review["id"], "applied") is not None
    # A second click finds nothing left to resolve.
    assert database.resolve_decision(review["id"], "applied") is None


def test_open_reviews_are_reported_for_exclusion(database):
    database.add_decision(decision(status="needs_review"))
    database.add_decision(decision(transaction_id="txn-2", status="applied"))
    assert database.open_review_transaction_ids() == {"txn-1"}


def test_decisions_keep_their_tags_and_rationale(database):
    decision_id = database.add_decision(decision())
    stored = database.get_decision(decision_id)
    assert stored["tags"] == ["subscription"]
    assert stored["rationale"]["reason"] == "history"
    assert database.get_decision("missing") is None


# -------------------------------------------------------------------- memory


def test_memory_accumulates_and_corrections_are_counted(database):
    database.record_memory("blue bottle", "cat-coffee", "Coffee")
    database.record_memory("blue bottle", "cat-coffee", "Coffee")
    database.record_memory("blue bottle", "cat-dining", "Dining", correction=True)
    rows = {row["category_id"]: row for row in database.memory_for("blue bottle")}
    assert rows["cat-coffee"]["hits"] == 2
    assert rows["cat-dining"]["corrections"] == 1
    assert database.memory_size() == 1

    database.forget_merchant("blue bottle")
    assert database.memory_for("blue bottle") == []


def test_incomplete_memory_rows_are_ignored(database):
    database.record_memory("", "cat-coffee", "Coffee")
    database.record_memory("blue bottle", "", "Coffee")
    assert database.memory_size() == 0


# ------------------------------------------------------------ rule promotion


def rule(**kwargs):
    base = {
        "merchant_key": "blue bottle",
        "merchant_label": "Blue Bottle",
        "category_id": "cat-coffee",
        "category_name": "Coffee",
        "match_value": "BLUE BOTTLE",
        "observations": 3,
    }
    base.update(kwargs)
    return base


def test_a_merchant_is_only_suggested_once(database):
    assert database.suggest_rule(**rule()) is not None
    assert database.suggest_rule(**rule()) is None


def test_a_declined_merchant_is_not_offered_again(database):
    rule_id = database.suggest_rule(**rule())
    database.resolve_rule_suggestion(rule_id, "declined")
    assert database.suggest_rule(**rule()) is None


def test_creating_a_rule_claims_it_first(database):
    rule_id = database.suggest_rule(**rule())
    assert database.claim_rule_suggestion(rule_id) is not None
    assert database.claim_rule_suggestion(rule_id) is None
    database.resolve_rule_suggestion(rule_id, "created")
    assert database.list_rule_suggestions(status="created")[0]["id"] == rule_id


# -------------------------------------------------------------------- health


def snapshot(account_id="acct-1", status="ok", detail=""):
    return {"account_id": account_id, "account_name": "Checking", "status": status, "detail": detail}


def test_only_a_real_change_of_state_produces_a_transition(database):
    assert len(database.record_health([snapshot()])) == 1
    assert database.record_health([snapshot()]) == []
    [transition] = database.record_health([snapshot(status="error", detail="down")])
    assert transition["previous_status"] == "ok"
    assert transition["status"] == "error"


def test_health_snapshots_and_events_are_retained(database):
    database.record_health([snapshot()])
    database.record_health([snapshot(status="stale")])
    [stored] = database.health_snapshots()
    assert stored["status"] == "stale"
    assert len(database.list_health_events()) == 2


def test_a_balance_mismatch_must_survive_three_consecutive_checks(database):
    database.record_health([snapshot()])

    def mismatch():
        return {
            **snapshot(status="drifted", detail="balances differ"),
            "status_label": "Balance mismatch",
            "alerting": True,
            "status_without_drift": "ok",
            "status_without_drift_label": "Connected",
            "alerting_without_drift": False,
            "detail_without_drift": "Balances agree and data is current.",
            "signals": ["balances differ"],
            "signals_without_drift": [],
        }

    first = mismatch()
    assert database.record_health([first], drift_confirmation_checks=3) == []
    assert first["status"] == "ok"
    assert first["balance_mismatch_pending"] is True
    assert first["balance_mismatch_checks"] == 1

    second = mismatch()
    assert database.record_health([second], drift_confirmation_checks=3) == []
    assert second["status"] == "ok"
    assert second["balance_mismatch_checks"] == 2

    third = mismatch()
    [transition] = database.record_health([third], drift_confirmation_checks=3)
    assert transition["previous_status"] == "ok"
    assert transition["status"] == "drifted"
    assert database.health_snapshots()[0]["status"] == "drifted"


def test_repolling_one_simplefin_snapshot_does_not_confirm_a_mismatch(database):
    database.record_health([snapshot()])

    def mismatch(balance_date):
        return {
            **snapshot(status="drifted", detail="balances differ"),
            "status_label": "Balance mismatch",
            "alerting": True,
            "status_without_drift": "ok",
            "status_without_drift_label": "Connected",
            "alerting_without_drift": False,
            "detail_without_drift": "Balances agree and data is current.",
            "signals": ["balances differ"],
            "signals_without_drift": [],
            "remote_balance_date": balance_date,
        }

    first = mismatch("2026-08-25T17:13:50+00:00")
    database.record_health([first], drift_confirmation_checks=3)
    assert first["balance_mismatch_checks"] == 1

    for _ in range(4):
        repeated = mismatch("2026-08-25T17:13:50+00:00")
        assert database.record_health([repeated], drift_confirmation_checks=3) == []
        assert repeated["status"] == "ok"
        assert repeated["balance_mismatch_checks"] == 1

    regressed = mismatch("2026-08-24T17:13:50+00:00")
    database.record_health([regressed], drift_confirmation_checks=3)
    assert regressed["balance_mismatch_checks"] == 1

    second = mismatch("2026-08-26T09:00:00+00:00")
    database.record_health([second], drift_confirmation_checks=3)
    assert second["balance_mismatch_checks"] == 2

    third = mismatch("2026-08-27T09:00:00+00:00")
    [transition] = database.record_health([third], drift_confirmation_checks=3)
    assert transition["status"] == "drifted"


def test_a_transient_balance_mismatch_clears_without_any_transition(database):
    database.record_health([snapshot()])
    candidate = {
        **snapshot(status="drifted"),
        "status_without_drift": "ok",
        "status_without_drift_label": "Connected",
        "signals_without_drift": [],
    }
    database.record_health([candidate], drift_confirmation_checks=3)
    assert database.record_health([snapshot()], drift_confirmation_checks=3) == []

    # A later mismatch starts from one again; the cleared observation broke
    # the consecutive streak.
    later = dict(candidate, status="drifted")
    database.record_health([later], drift_confirmation_checks=3)
    assert later["balance_mismatch_checks"] == 1


def test_existing_candidate_tables_gain_the_balance_snapshot_column(data_dir):
    path = data_dir / "old-health.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE health_candidates ("
        "account_id TEXT PRIMARY KEY,status TEXT NOT NULL,checks INTEGER NOT NULL DEFAULT 1,"
        "first_seen REAL NOT NULL,checked_at REAL NOT NULL)"
    )
    connection.execute(
        "INSERT INTO health_candidates VALUES('acct-1','drifted',2,1.0,2.0)"
    )
    connection.commit()
    connection.close()

    Database(path).initialize()

    connection = sqlite3.connect(path)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(health_candidates)")}
    remaining = connection.execute("SELECT COUNT(*) FROM health_candidates").fetchone()[0]
    connection.close()
    assert "remote_balance_date" in columns
    assert remaining == 0


def test_accounts_removed_from_actual_stop_being_reported(database):
    database.record_health([snapshot("acct-1"), snapshot("acct-2")])
    database.prune_health(["acct-1"])
    assert [item["account_id"] for item in database.health_snapshots()] == ["acct-1"]
    database.prune_health([])
    assert database.health_snapshots() == []


# ------------------------------------------------------------------ digests


def test_a_digest_is_claimed_once_per_date_and_time(database):
    assert database.claim_digest("2026-08-21", "07:30") is True
    assert database.claim_digest("2026-08-21", "07:30") is False
    assert database.digest_claimed("2026-08-21", "07:30") is True
    # A different day, or a different time of day, is a different delivery.
    assert database.claim_digest("2026-08-22", "07:30") is True
    assert database.claim_digest("2026-08-21", "18:00") is True


def test_a_row_from_before_slots_existed_is_history_not_a_live_claim(database):
    """It records that something went out, not that a given time is spent.

    Those rows never recorded a time, so treating them as claiming the whole
    date would forfeit the upgrade day entirely -- no scheduled delivery, and
    no way to test one. The worst the other reading costs is a single repeat
    on that one day.
    """

    database.claim_digest("2026-08-21")
    assert database.digest_claimed("2026-08-21", "07:30") is False
    assert database.list_digests()[0]["scheduled_for"] == ""


def test_a_failed_digest_releases_its_claim(database):
    database.claim_digest("2026-08-21", "07:30")
    database.release_digest("2026-08-21", "07:30")
    assert database.claim_digest("2026-08-21", "07:30") is True


def test_only_delivered_digests_are_shown(database):
    database.claim_digest("2026-08-20", "07:30")
    database.complete_digest("2026-08-20", "07:30", {"title": "old"}, delivered=False, error="off")
    assert database.latest_digest() is None
    database.claim_digest("2026-08-21", "07:30")
    database.complete_digest("2026-08-21", "07:30", {"title": "today"}, delivered=True)
    assert database.latest_digest()["payload"]["title"] == "today"


def test_the_delivery_receipt_says_where_a_digest_actually_went(database):
    """"Delivered" alone cannot tell a watched topic from an unwatched one."""
    database.claim_digest("2026-08-21", "07:30")
    database.complete_digest(
        "2026-08-21",
        "07:30",
        {"title": "today"},
        delivered=True,
        receipt={"server": "https://ntfy.sh", "topic": "budget-abc", "id": "RxIFhE7"},
    )
    listed = database.list_digests()[0]
    assert listed["scheduled_for"] == "07:30"
    assert listed["receipt"]["topic"] == "budget-abc"
    assert listed["receipt"]["id"] == "RxIFhE7"


# ---------------------------------------------------------------- snapshots


def test_snapshots_round_trip_with_their_age(database):
    assert database.get_snapshot("overview") is None
    database.set_snapshot("overview", {"budget": {"free_cents": 100}})
    stored = database.get_snapshot("overview")
    assert stored["budget"]["free_cents"] == 100
    assert stored["snapshot_updated_at"] <= time.time()


def test_settings_round_trip(database):
    assert database.get_setting("runtime") is None
    database.set_setting("runtime", "{}")
    database.set_setting("runtime", '{"a":1}')
    assert database.get_setting("runtime") == '{"a":1}'


# ------------------------------------------------------------------- counts


def test_dashboard_counts_reflect_the_work_outstanding(database):
    database.enqueue_job("sync", 3)
    database.add_decision(decision(status="needs_review"))
    database.add_decision(decision(transaction_id="txn-2", status="applied"))
    database.suggest_rule(**rule())
    database.record_health([snapshot(status="error")])
    counts = database.counts()
    assert counts == {
        "active_jobs": 1,
        "failed_jobs": 0,
        "needs_review": 1,
        "applied_today": 1,
        "rule_suggestions": 1,
        "degraded_accounts": 1,
    }


# --------------------------------------------------------------- monitoring


def test_accounts_are_monitored_until_told_otherwise(database):
    assert database.unmonitored_account_ids() == set()
    database.set_monitoring("a1", False, "Old Savings")
    assert database.unmonitored_account_ids() == {"a1"}
    database.set_monitoring("a1", True)
    assert database.unmonitored_account_ids() == set()


def test_monitoring_survives_health_rows_being_pruned(database):
    """The preference is the user's, not a by-product of the last check."""
    database.set_monitoring("a1", False, "Old Savings")
    database.record_health([{"account_id": "a1", "account_name": "Old Savings", "status": "muted"}])
    database.prune_health([])
    assert database.health_snapshots() == []
    assert database.unmonitored_account_ids() == {"a1"}


def test_a_later_update_keeps_the_known_account_name(database):
    database.set_monitoring("a1", False, "Old Savings")
    database.set_monitoring("a1", True)
    with database.connect() as connection:
        row = connection.execute(
            "SELECT account_name FROM account_monitoring WHERE account_id='a1'"
        ).fetchone()
    assert row["account_name"] == "Old Savings"


def test_muted_accounts_are_not_counted_as_degraded(database):
    database.record_health(
        [
            {"account_id": "a1", "account_name": "Dormant", "status": "muted"},
            {"account_id": "a2", "account_name": "Checking", "status": "error"},
        ]
    )
    assert database.counts()["degraded_accounts"] == 1


def test_every_path_that_hands_out_a_job_parses_it_the_same_way(database):
    """`params` and `result` must never leak as raw JSON strings."""
    created, _ = database.enqueue_job("categorize", 3, params={"full": True})
    duplicate, was_new = database.enqueue_job("categorize", 3)
    claimed = database.claim_job("worker", 60)
    database.finish_job(claimed["id"], status="failed", phase="failed", result={"applied": 1})
    retried = database.retry_job(claimed["id"])

    assert was_new is False
    for job in (created, duplicate, claimed, retried):
        assert job["params"] == {"full": True}
        assert "params_json" not in job
        assert "result_json" not in job
    assert database.get_job(claimed["id"])["params"] == {"full": True}


def test_a_digest_ledger_written_before_slots_existed_is_carried_over(data_dir):
    """The ledger is the only record of what already went out; it must survive."""
    path = data_dir / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE digests (local_date TEXT PRIMARY KEY, "
        "payload_json TEXT NOT NULL DEFAULT '{}', delivered INTEGER NOT NULL DEFAULT 0, "
        "error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL)"
    )
    connection.execute(
        "INSERT INTO digests(local_date,payload_json,delivered,created_at) "
        "VALUES('2026-08-21','{\"title\":\"yesterday\"}',1,1000.0)"
    )
    connection.commit()
    connection.close()

    database = Database(path)
    database.initialize()
    carried = database.list_digests()
    assert [row["local_date"] for row in carried] == ["2026-08-21"]
    assert carried[0]["scheduled_for"] == ""
    assert database.latest_digest()["payload"]["title"] == "yesterday"
    # It is history, not a live claim, so the upgrade day can still deliver
    # and still be tested.
    assert database.digest_claimed("2026-08-21", "07:30") is False

    # Starting again must not duplicate the rows or lose them.
    database.initialize()
    assert len(database.list_digests()) == 1
