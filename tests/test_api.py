from __future__ import annotations

import datetime
from typing import Any

import httpx
import pytest

from actual_clerk.clients.actual import ActualGatewayError
from actual_clerk.config import SettingsManager
from actual_clerk.db import Database
from actual_clerk.main import app
from actual_clerk.processing import OVERVIEW_SNAPSHOT, JobManager


class FakeGateway:
    """Stands in for the budget thread, recording what the API asked it to do."""

    def __init__(self, overview: dict[str, Any] | None = None):
        self.updates: list[dict[str, Any]] = []
        self.overwrite_flags: list[bool] = []
        self.rules: list[dict[str, Any]] = []
        self.categories: list[dict[str, Any]] = []
        self.applied_ids: list[str] | None = None
        self.skipped: list[dict[str, str]] = []
        self.error: Exception | None = None
        self.overview = overview or {"budget": {}, "categories": [], "health": []}
        self.snapshot_payload: dict[str, Any] | None = None
        self.probe_payload: dict[str, Any] = {
            "budget_type_preference": "tracking",
            "reads_table": "reflect_budgets",
            "is_tracking": True,
            "tables": {
                "zero_budgets": {"rows": 0, "non_zero": 0, "total_cents": 0},
                "reflect_budgets": {"rows": 8, "non_zero": 3, "total_cents": 400000},
            },
            "budget_name": "Household",
            "budget_id": "budget-1",
        }

    async def snapshot(self, *, today=None):
        if self.snapshot_payload is None:
            raise ActualGatewayError("An Actual server password is not configured")
        return self.snapshot_payload

    async def run(self, fn, *, refresh: bool = True):
        return self.probe_payload

    def status(self):
        return {"connected": True, "last_error": "", "last_connected_at": None}

    def settings_changed(self):
        self.settings_changes = getattr(self, "settings_changes", 0) + 1

    async def apply_updates(self, updates, *, overwrite: bool = False):
        if self.error is not None:
            raise self.error
        self.updates.extend(updates)
        self.overwrite_flags.append(overwrite)
        applied = (
            self.applied_ids
            if self.applied_ids is not None
            else [item["transaction_id"] for item in updates]
        )
        return {"applied": applied, "skipped": self.skipped}

    async def create_category_rule(self, *, match_value, category_id, run_immediately=False):
        if self.error is not None:
            raise self.error
        rule = {"id": "rule-1", "match_value": match_value, "category_id": category_id}
        self.rules.append(rule)
        return rule

    async def create_category(self, name, group_name):
        created = {"id": "cat-new", "name": name, "group_name": group_name}
        self.categories.append(created)
        return created

    async def test_connection(self):
        if self.error is not None:
            raise self.error
        return {"ok": True, "message": "3 account(s) in My Budget"}


@pytest.fixture
def gateway():
    return FakeGateway()


@pytest.fixture
async def client(tmp_path, gateway, monkeypatch):
    database = Database(tmp_path / "clerk.db")
    database.initialize()
    manager = SettingsManager(database)
    jobs = JobManager(database, manager, gateway)

    async def refresh_now():
        return gateway.overview

    monkeypatch.setattr(jobs, "refresh_now", refresh_now)

    app.state.database = database
    app.state.settings_manager = manager
    app.state.gateway = gateway
    app.state.job_manager = jobs
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://clerk") as http:
        http.database = database
        http.settings_manager = manager
        yield http


def decision(**kwargs):
    base = {
        "transaction_id": "txn-1",
        "account_id": "acct-1",
        "account_name": "Checking",
        "payee_name": "Blue Bottle",
        "merchant_key": "blue bottle",
        "transaction_date": "2026-08-21",
        "amount_cents": -650,
        "source": "model",
        "status": "needs_review",
        "category_id": "cat-coffee",
        "category_name": "Coffee",
        "confidence": 0.55,
        "tags": ["subscription"],
        "rationale": {"reason": "looks like a cafe"},
    }
    base.update(kwargs)
    return base


# -------------------------------------------------------------------- basics


async def test_health_reports_what_is_configured(client):
    response = await client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["configured"]["actual"] is False
    assert set(body["configured"]) == {"actual", "simplefin", "model", "notifications"}


async def test_api_responses_are_never_cached(client):
    response = await client.get("/api/health")
    assert response.headers["cache-control"] == "no-store"


async def test_the_overview_serves_the_last_good_snapshot(client):
    client.database.set_snapshot(OVERVIEW_SNAPSHOT, {"budget": {"free_cents": 20800}})
    response = await client.get("/api/overview")
    body = response.json()
    assert body["overview"]["budget"]["free_cents"] == 20800
    assert body["counts"]["needs_review"] == 0
    assert body["gateway"]["connected"] is True
    assert body["stale"] is not None


async def test_the_overview_works_before_the_first_sync(client):
    body = (await client.get("/api/overview")).json()
    assert body["overview"] == {}
    assert body["last_sync"] is None


async def test_unknown_api_paths_are_not_swallowed_by_the_single_page_app(client):
    assert (await client.get("/api/nope")).status_code == 404
    # A UI route still serves the application shell.
    assert (await client.get("/accounts")).status_code == 200


# ---------------------------------------------------------------------- jobs


async def test_a_job_can_be_queued_and_deduplicated(client):
    first = await client.post("/api/jobs", json={"kind": "sync"})
    second = await client.post("/api/jobs", json={"kind": "sync"})
    assert first.status_code == 202
    assert first.json()["created"] is True
    assert second.json()["created"] is False


async def test_an_unknown_job_kind_is_refused(client):
    assert (await client.post("/api/jobs", json={"kind": "nonsense"})).status_code == 422


async def test_a_missing_job_is_a_404(client):
    assert (await client.get("/api/jobs/nope")).status_code == 404
    assert (await client.post("/api/jobs/nope/retry")).status_code == 409
    assert (await client.post("/api/jobs/nope/cancel")).status_code == 409


async def test_a_queued_job_can_be_cancelled(client):
    job = (await client.post("/api/jobs", json={"kind": "health"})).json()["job"]
    assert (await client.post(f"/api/jobs/{job['id']}/cancel")).json() == {"cancelled": True}
    listed = (await client.get("/api/jobs?status=cancelled")).json()
    assert [item["id"] for item in listed] == [job["id"]]


async def test_job_timestamps_are_serialized_as_iso_strings(client):
    job = (await client.post("/api/jobs", json={"kind": "sync"})).json()["job"]
    detail = (await client.get(f"/api/jobs/{job['id']}")).json()
    datetime.datetime.fromisoformat(detail["created_at"])
    assert detail["completed_at"] is None
    assert "worker_id" not in detail
    assert detail["events"][0]["event_type"] == "enqueued"


# ------------------------------------------------------------------- reviews


async def test_accepting_a_review_writes_the_proposal_and_teaches_clerk(client, gateway):
    client.database.add_decision(decision())
    [review] = (await client.get("/api/reviews")).json()

    response = await client.post(f"/api/reviews/{review['id']}/resolve", json={"action": "accept"})
    assert response.json()["status"] == "applied"
    assert gateway.updates == [
        {"transaction_id": "txn-1", "category_id": "cat-coffee", "add_tags": ["subscription", "clerk"]}
    ]
    assert gateway.overwrite_flags == [True]
    memory = client.database.memory_for("blue bottle")
    assert memory[0]["category_id"] == "cat-coffee"
    assert memory[0]["corrections"] == 0


async def test_choosing_a_different_category_is_recorded_as_a_correction(client, gateway):
    client.database.set_snapshot(
        OVERVIEW_SNAPSHOT,
        {"categories": [{"id": "cat-dining", "name": "Dining", "group_name": "Everyday"}]},
    )
    client.database.add_decision(decision())
    [review] = (await client.get("/api/reviews")).json()

    response = await client.post(
        f"/api/reviews/{review['id']}/resolve",
        json={"action": "recategorize", "category_id": "cat-dining"},
    )
    assert response.json()["category_name"] == "Dining"
    assert gateway.updates[0]["category_id"] == "cat-dining"
    memory = {row["category_id"]: row for row in client.database.memory_for("blue bottle")}
    assert memory["cat-dining"]["corrections"] == 1


async def test_recategorizing_without_a_category_is_refused(client):
    client.database.add_decision(decision())
    [review] = (await client.get("/api/reviews")).json()
    response = await client.post(
        f"/api/reviews/{review['id']}/resolve", json={"action": "recategorize"}
    )
    assert response.status_code == 422


async def test_dismissing_a_review_writes_nothing(client, gateway):
    client.database.add_decision(decision())
    [review] = (await client.get("/api/reviews")).json()
    response = await client.post(f"/api/reviews/{review['id']}/resolve", json={"action": "dismiss"})
    assert response.json() == {"status": "dismissed"}
    assert gateway.updates == []
    assert (await client.get("/api/reviews")).json() == []


async def test_a_review_resolves_only_once(client, gateway):
    client.database.add_decision(decision())
    [review] = (await client.get("/api/reviews")).json()
    first = await client.post(f"/api/reviews/{review['id']}/resolve", json={"action": "accept"})
    second = await client.post(f"/api/reviews/{review['id']}/resolve", json={"action": "accept"})
    assert first.status_code == 200
    assert second.status_code == 409
    assert len(gateway.updates) == 1


async def test_a_review_with_no_proposal_needs_a_category_chosen(client):
    client.database.add_decision(decision(category_id=None, proposed_category="Pet Care"))
    [review] = (await client.get("/api/reviews")).json()
    response = await client.post(f"/api/reviews/{review['id']}/resolve", json={"action": "accept"})
    assert response.status_code == 422
    # Still open, so the review can be answered properly.
    assert len((await client.get("/api/reviews")).json()) == 1


async def test_a_failed_write_reopens_the_review(client, gateway):
    client.database.add_decision(decision())
    [review] = (await client.get("/api/reviews")).json()
    gateway.error = ActualGatewayError("Actual is down", retryable=True)

    response = await client.post(f"/api/reviews/{review['id']}/resolve", json={"action": "accept"})
    assert response.status_code == 502
    assert len((await client.get("/api/reviews")).json()) == 1


async def test_a_transaction_deleted_in_actual_is_reported_not_learned(client, gateway):
    client.database.add_decision(decision())
    [review] = (await client.get("/api/reviews")).json()
    gateway.applied_ids = []
    gateway.skipped = [{"id": "txn-1", "reason": "deleted"}]

    response = await client.post(f"/api/reviews/{review['id']}/resolve", json={"action": "accept"})
    assert response.json() == {"status": "skipped", "reason": "deleted"}
    assert client.database.memory_for("blue bottle") == []


async def test_a_missing_review_is_a_404(client):
    assert (await client.post("/api/reviews/nope/resolve", json={"action": "dismiss"})).status_code == 404


# --------------------------------------------------------------------- rules


def rule_row(database):
    database.suggest_rule(
        merchant_key="blue bottle",
        merchant_label="Blue Bottle",
        category_id="cat-coffee",
        category_name="Coffee",
        match_value="BLUE BOTTLE",
        observations=3,
    )
    return database.list_rule_suggestions()[0]


async def test_creating_a_promoted_rule_hands_it_to_actual(client, gateway):
    suggestion = rule_row(client.database)
    response = await client.post(f"/api/rules/{suggestion['id']}/resolve", json={"action": "create"})
    assert response.json()["status"] == "created"
    assert gateway.rules == [
        {"id": "rule-1", "match_value": "BLUE BOTTLE", "category_id": "cat-coffee"}
    ]
    assert (await client.get("/api/rules")).json() == []


async def test_declining_a_rule_leaves_actual_untouched(client, gateway):
    suggestion = rule_row(client.database)
    response = await client.post(f"/api/rules/{suggestion['id']}/resolve", json={"action": "decline"})
    assert response.json() == {"status": "declined"}
    assert gateway.rules == []


async def test_a_rule_is_created_only_once(client, gateway):
    suggestion = rule_row(client.database)
    await client.post(f"/api/rules/{suggestion['id']}/resolve", json={"action": "create"})
    second = await client.post(f"/api/rules/{suggestion['id']}/resolve", json={"action": "create"})
    assert second.status_code == 409
    assert len(gateway.rules) == 1


async def test_a_failed_rule_creation_leaves_the_suggestion_open(client, gateway):
    suggestion = rule_row(client.database)
    gateway.error = ActualGatewayError("Actual is down")
    response = await client.post(f"/api/rules/{suggestion['id']}/resolve", json={"action": "create"})
    assert response.status_code == 502
    assert len((await client.get("/api/rules")).json()) == 1


# ------------------------------------------------------------------ settings


async def test_settings_never_return_a_secret(client):
    client.settings_manager.update({"actual_password": "hunter2"})
    body = (await client.get("/api/settings")).json()
    assert "hunter2" not in str(body)
    assert body["actual_password_configured"] is True


async def test_settings_can_be_changed_and_validated(client):
    response = await client.patch(
        "/api/settings", json={"values": {"sync_interval_minutes": 30}}
    )
    assert response.json()["settings"]["sync_interval_minutes"] == 30
    bad = await client.patch("/api/settings", json={"values": {"timezone": "Mars/Olympus"}})
    assert bad.status_code == 422


async def test_a_setting_that_needs_a_restart_says_so(client):
    response = await client.patch("/api/settings", json={"values": {"log_level": "DEBUG"}})
    assert response.json()["restart_required"] == ["log_level"]


async def test_a_secret_is_cleared_by_saving_an_empty_value(client):
    client.settings_manager.update({"ntfy_token": "tok"})
    await client.patch("/api/settings", json={"values": {"ntfy_token": ""}})
    assert (await client.get("/api/settings")).json()["ntfy_token_configured"] is False


async def test_the_actual_connection_test_goes_through_the_gateway(client, gateway):
    response = await client.post("/api/settings/test/actual")
    assert response.json()["ok"] is True
    gateway.error = ActualGatewayError("no route to host")
    assert (await client.post("/api/settings/test/actual")).status_code == 502


async def test_an_unknown_test_target_is_a_404(client):
    assert (await client.post("/api/settings/test/nonsense")).status_code == 404


# ---------------------------------------------------------------- categories


async def test_a_category_can_be_created_from_a_suggestion(client, gateway):
    response = await client.post(
        "/api/categories", json={"name": "Pet Care", "group_name": "Everyday"}
    )
    assert response.status_code == 201
    assert gateway.categories == [{"id": "cat-new", "name": "Pet Care", "group_name": "Everyday"}]


async def test_a_blank_category_name_is_refused(client):
    assert (await client.post("/api/categories", json={"name": "  ", "group_name": "x"})).status_code == 422


# ------------------------------------------------------------------- memory


async def test_a_merchant_can_be_forgotten(client):
    client.database.record_memory("blue bottle", "cat-coffee", "Coffee")
    response = await client.delete("/api/memory/blue%20bottle")
    assert response.json() == {"forgotten": True}
    assert client.database.memory_for("blue bottle") == []


# ------------------------------------------------------------------ accounts


async def test_connection_health_and_its_history_are_served(client):
    client.database.record_health(
        [{"account_id": "a1", "account_name": "Checking", "status": "error", "detail": "down"}]
    )
    body = (await client.get("/api/accounts")).json()
    assert body["health"][0]["status"] == "error"
    assert body["events"][0]["previous_status"] == ""
    datetime.datetime.fromisoformat(body["health"][0]["checked_at"])


# ---------------------------------------------------------------- monitoring


async def test_monitoring_can_be_turned_off_and_back_on(client):
    off = await client.post(
        "/api/accounts/acct-1/monitoring", json={"monitored": False, "account_name": "Old Savings"}
    )
    assert off.json() == {"account_id": "acct-1", "monitored": False}
    assert client.database.unmonitored_account_ids() == {"acct-1"}

    on = await client.post("/api/accounts/acct-1/monitoring", json={"monitored": True})
    assert on.json()["monitored"] is True
    assert client.database.unmonitored_account_ids() == set()


async def test_changing_monitoring_rechecks_the_connections(client):
    await client.post("/api/accounts/acct-1/monitoring", json={"monitored": False})
    assert [job["kind"] for job in client.database.list_jobs()] == ["health"]


async def test_monitoring_needs_an_explicit_choice(client):
    assert (await client.post("/api/accounts/acct-1/monitoring", json={})).status_code == 422


# ------------------------------------------------------------ history catch-up


async def test_a_catch_up_run_is_marked_as_covering_all_history(client):
    response = await client.post("/api/jobs", json={"kind": "categorize", "full": True})
    assert response.status_code == 202
    assert response.json()["job"]["params"] == {"full": True}


async def test_an_ordinary_filing_run_is_not_a_catch_up(client):
    response = await client.post("/api/jobs", json={"kind": "categorize"})
    assert response.json()["job"]["params"] == {}


async def test_the_full_flag_is_meaningless_for_other_job_kinds(client):
    response = await client.post("/api/jobs", json={"kind": "sync", "full": True})
    assert response.json()["job"]["params"] == {}


# --------------------------------------------------------------- diagnostics


async def test_diagnostics_still_reports_when_actual_cannot_be_read(client, gateway):
    """The report is most needed exactly when the budget will not open."""
    response = await client.get("/api/diagnostics")
    assert response.status_code == 200
    body = response.json()
    assert "ACTUAL CLERK DIAGNOSTIC" in body["report"]
    assert "password is not configured" in body["report"]
    assert body["generated_at"]


async def test_diagnostics_reports_on_a_live_snapshot(client, gateway):
    from tests.factories import snapshot as make_snapshot
    from tests.factories import transaction as make_transaction

    gateway.snapshot_payload = make_snapshot(
        transactions=[
            make_transaction(
                datetime.date.today(), 400000, payee="Work", category_id="cat-paycheck"
            )
        ],
        budgeted={"cat-paycheck": 400000, "cat-rent": 120000},
    )
    response = await client.get("/api/diagnostics")
    report = response.json()["report"]
    assert "TRACKING" in report
    assert "Actual's Projected Savings" in report
    assert "Checking" in report


async def test_diagnostics_redaction_is_opt_in(client, gateway):
    from tests.factories import account as make_account
    from tests.factories import snapshot as make_snapshot

    gateway.snapshot_payload = make_snapshot(
        accounts=[make_account("Ally Savings")], budgeted={"cat-rent": 120000}
    )
    plain = (await client.get("/api/diagnostics")).json()["report"]
    hidden = (await client.get("/api/diagnostics", params={"redact": "true"})).json()["report"]
    assert "Ally Savings" in plain
    assert "Ally Savings" not in hidden
    assert "Account 1" in hidden


# ------------------------------------------------------------------- assets


async def test_index_stamps_asset_urls_with_a_content_hash(client):
    """A wheel dates every file to 2020, which browsers read as licence to
    cache for months. The stamp changes with the bytes, so a rebuilt container
    is never served from a stale cache."""
    import hashlib

    from actual_clerk.main import STATIC_DIRECTORY

    html = (await client.get("/")).text
    for name in ("app.js", "styles.css"):
        digest = hashlib.sha256((STATIC_DIRECTORY / name).read_bytes()).hexdigest()[:12]
        assert f"/assets/{name}?v={digest}" in html
        assert f'"/assets/{name}"' not in html, "an unstamped URL would keep the old cache alive"


async def test_index_is_never_cached_without_revalidating(client):
    response = await client.get("/")
    assert response.headers["cache-control"] == "no-cache"


async def test_assets_are_revalidated_rather_than_heuristically_cached(client):
    response = await client.get("/assets/app.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers.get("etag")


async def test_revalidating_an_unchanged_asset_costs_no_body(client):
    first = await client.get("/assets/app.js")
    again = await client.get("/assets/app.js", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304
    assert not again.content


async def test_the_asset_stamp_follows_the_file_contents():
    """Rebuilding without changing an asset must not change its URL."""
    import hashlib

    from actual_clerk.main import STATIC_DIRECTORY, _asset_query

    stamps = _asset_query()
    for name, stamp in stamps.items():
        expected = hashlib.sha256((STATIC_DIRECTORY / name).read_bytes()).hexdigest()[:12]
        assert stamp == expected
