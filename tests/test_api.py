from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from actual_clerk.clients.actual import ActualGatewayError
from actual_clerk.config import SettingsManager
from actual_clerk.db import Database
from actual_clerk.main import (
    STATIC_ASSET_VERSION,
    _static_asset_version,
    app,
)
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

    async def snapshot(self, *, today=None, transaction_ids=()):
        if self.snapshot_payload is None:
            raise ActualGatewayError("An Actual server password is not configured")
        requested = set(transaction_ids)
        return {
            **self.snapshot_payload,
            "review_transactions": [
                item
                for item in self.snapshot_payload.get("transactions", [])
                if item["id"] in requested
            ],
        }

    async def diagnostics(self):
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

    async def create_account(self, name, *, off_budget=False, initial_balance_cents=None):
        self.created_accounts = getattr(self, "created_accounts", [])
        self.created_accounts.append((name, off_budget))
        return {"id": "acct-new", "name": name, "off_budget": off_budget}

    # ---- bank links (Stage 1 primitives), scripted per test
    detailed_accounts: list[dict[str, Any]] = []
    server_configured = True
    server_accounts: list[dict[str, Any]] = []

    async def list_accounts_detailed(self):
        return [dict(account) for account in self.detailed_accounts]

    async def simplefin_server_status(self):
        return {"configured": self.server_configured, "error": ""}

    async def simplefin_server_accounts(self):
        return {"accounts": list(self.server_accounts), "error": "", "reason": ""}

    async def unlink_account(self, account_id):
        self.unlinked = getattr(self, "unlinked", [])
        self.unlinked.append(account_id)
        for account in self.detailed_accounts:
            if account["id"] == account_id:
                account["sync_source"] = ""
                account["external_id"] = ""
        return {"id": account_id, "previous_sync_source": "simpleFin"}

    async def link_simplefin_account(self, external_account, *, account_id="", off_budget=False, starting_date=None, starting_balance_cents=None):
        self.relinked = getattr(self, "relinked", [])
        self.relinked.append((account_id, external_account["account_id"], starting_date))
        return {"id": account_id, "sync_source": "simpleFin", "external_id": external_account["account_id"]}

    async def set_server_secret(self, name, value):
        self.secrets = getattr(self, "secrets", [])
        self.secrets.append((name, value))
        if value is None:
            self.server_configured = False
        else:
            self.server_configured = True
        return {"name": name, "cleared": value is None}

    async def account_transactions(self, account_id, *, start=None, end=None):
        return []

    # ---- rules in Actual, scripted per test
    actual_rules: list[dict[str, Any]] = []
    payees: list[dict[str, Any]] = []

    async def list_rules(self):
        return [dict(rule) for rule in self.actual_rules]

    async def list_payees(self):
        return [dict(payee) for payee in self.payees]

    async def delete_rule(self, rule_id):
        if self.error is not None:
            raise self.error
        self.actual_rules = [rule for rule in self.actual_rules if rule["id"] != rule_id]
        self.deleted_rules = getattr(self, "deleted_rules", [])
        self.deleted_rules.append(rule_id)
        return {"id": rule_id, "deleted": True}

    async def restore_rule(self, rule):
        if self.error is not None:
            raise self.error
        self.restored_rules = getattr(self, "restored_rules", [])
        self.restored_rules.append(rule)
        created = {**rule, "id": f"restored-{len(self.restored_rules)}"}
        self.actual_rules.append(created)
        return {"id": created["id"], "previous_id": rule.get("id", "")}

    async def import_transactions(self, account_id, transactions, *, dry_run=False):
        return {"added": [], "updated": [], "errors": [], "preview": [], "dry_run": dry_run}


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
    assert set(body["configured"]) == {"actual", "simplefin", "plaid", "model", "notifications"}


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


# -------------------------------------------------------------- intelligence


def snapshot_with_categories(client):
    client.database.set_snapshot(
        OVERVIEW_SNAPSHOT,
        {
            "budget": {},
            "categories": [
                {"id": "cat-coffee", "name": "Coffee", "group_name": "Everyday", "is_income": False},
                {"id": "cat-dining", "name": "Dining", "group_name": "Everyday", "is_income": False},
            ],
            "accounts": [{"id": "acct-1", "name": "Checking", "closed": False}],
            "health": [],
        },
    )


async def test_a_rule_is_declared_on_the_key_a_name_becomes(client):
    snapshot_with_categories(client)
    response = await client.post(
        "/api/intelligence/rules",
        json={"merchant": "SQ *BLUE BOTTLE 4471", "category_id": "cat-coffee"},
    )
    assert response.status_code == 201
    rule = response.json()["rule"]
    assert rule["merchant_key"] == "blue bottle"
    assert rule["merchant_label"] == "SQ *BLUE BOTTLE 4471"
    assert rule["category_name"] == "Coffee"
    assert rule["status"] == "active"
    assert rule["source"] == "user"
    page = (await client.get("/api/intelligence")).json()
    assert [item["id"] for item in page["rules"]] == [rule["id"]]
    assert page["counts"]["rules"] == 1
    assert [category["id"] for category in page["categories"]] == ["cat-coffee", "cat-dining"]


async def test_a_rule_needs_a_category_actual_has_and_a_name_with_something_in_it(client):
    snapshot_with_categories(client)
    missing = await client.post(
        "/api/intelligence/rules", json={"merchant": "Blue Bottle", "category_id": "cat-nope"}
    )
    assert missing.status_code == 404
    empty = await client.post(
        "/api/intelligence/rules", json={"merchant": "1234", "category_id": "cat-coffee"}
    )
    assert empty.status_code == 422
    blank = await client.post("/api/intelligence/rules", json={"category_id": "cat-coffee"})
    assert blank.status_code == 422


async def test_the_key_preview_shows_what_a_name_becomes(client):
    body = (await client.get("/api/intelligence/merchant", params={"text": "TST* Blue Bottle"})).json()
    assert body == {"merchant_key": "blue bottle", "label": "TST* Blue Bottle"}


async def test_a_rule_can_be_paused_recategorized_and_retired(client):
    snapshot_with_categories(client)
    rule = (await client.post(
        "/api/intelligence/rules", json={"merchant": "Blue Bottle", "category_id": "cat-coffee"}
    )).json()["rule"]
    paused = await client.patch(f"/api/intelligence/rules/{rule['id']}", json={"status": "paused"})
    assert paused.json()["rule"]["status"] == "paused"
    changed = await client.patch(
        f"/api/intelligence/rules/{rule['id']}", json={"category_id": "cat-dining", "match": "family"}
    )
    assert changed.json()["rule"]["category_name"] == "Dining"
    assert changed.json()["rule"]["match"] == "family"
    assert (await client.patch(f"/api/intelligence/rules/{rule['id']}", json={"category_id": "nope"})).status_code == 404
    retired = await client.delete(f"/api/intelligence/rules/{rule['id']}")
    assert retired.json()["rule"]["status"] == "retired"
    assert (await client.get("/api/intelligence")).json()["rules"] == []
    listed = (await client.get("/api/intelligence", params={"include_retired": "true"})).json()
    assert listed["rules"][0]["status"] == "retired"
    assert (await client.delete("/api/intelligence/rules/nope")).status_code == 404


async def test_accepting_a_rule_proposal_declares_the_rule(client):
    snapshot_with_categories(client)
    proposal_id = client.database.add_proposal(
        kind="rule",
        merchant_key="blue bottle",
        payload={"merchant_key": "blue bottle", "merchant_label": "Blue Bottle", "category_id": "cat-coffee", "category_name": "Coffee"},
        evidence={"observations": 3},
    )
    page = (await client.get("/api/intelligence")).json()
    assert page["proposals"][0]["evidence"] == {"observations": 3}
    response = await client.post(f"/api/intelligence/proposals/{proposal_id}/resolve", json={"action": "accept"})
    assert response.json()["status"] == "accepted"
    assert response.json()["rule"]["source"] == "proposal"
    assert client.database.active_rules()[0]["merchant_key"] == "blue bottle"
    assert (await client.get("/api/intelligence")).json()["proposals"] == []
    again = await client.post(f"/api/intelligence/proposals/{proposal_id}/resolve", json={"action": "accept"})
    assert again.status_code == 409


async def test_a_rule_change_proposal_updates_the_rule_and_clears_its_disputes(client):
    snapshot_with_categories(client)
    rule = client.database.upsert_rule(merchant_key="blue bottle", category_id="cat-coffee", category_name="Coffee")
    client.database.record_rule_dispute(rule["id"])
    proposal_id = client.database.add_proposal(kind="rule_change", merchant_key="blue bottle", payload={"rule_id": rule["id"], "category_id": "cat-dining"})
    response = await client.post(f"/api/intelligence/proposals/{proposal_id}/resolve", json={"action": "accept"})
    assert response.json()["rule"]["category_name"] == "Dining"
    assert client.database.get_rule(rule["id"])["disputed_count"] == 0


async def test_a_repair_needs_a_category_and_a_retire_retires(client):
    snapshot_with_categories(client)
    rule = client.database.upsert_rule(merchant_key="blue bottle", category_id="cat-gone", category_name="Old")
    repair = client.database.add_proposal(kind="repair", merchant_key="blue bottle", payload={"rule_id": rule["id"]})
    missing = await client.post(f"/api/intelligence/proposals/{repair}/resolve", json={"action": "accept"})
    assert missing.status_code == 422
    # Handed back open, so it can be answered properly.
    fixed = await client.post(f"/api/intelligence/proposals/{repair}/resolve", json={"action": "accept", "category_id": "cat-coffee"})
    assert fixed.json()["rule"]["category_id"] == "cat-coffee"
    retire = client.database.add_proposal(kind="rule_retire", merchant_key="blue bottle", payload={"rule_id": rule["id"]})
    response = await client.post(f"/api/intelligence/proposals/{retire}/resolve", json={"action": "accept"})
    assert response.json()["rule"]["status"] == "retired"
    orphan = client.database.add_proposal(kind="rule_change", merchant_key="nobody", payload={"rule_id": "nope"})
    assert (await client.post(f"/api/intelligence/proposals/{orphan}/resolve", json={"action": "accept"})).status_code == 409


async def test_accepting_an_alias_proposal_declares_the_alias(client):
    proposal_id = client.database.add_proposal(kind="alias", merchant_key="valve", payload={"alias_key": "valve", "alias_label": "Valve", "merchant_key": "steam"})
    response = await client.post(f"/api/intelligence/proposals/{proposal_id}/resolve", json={"action": "accept"})
    assert response.json()["alias"]["merchant_key"] == "steam"
    assert client.database.alias_map() == {"valve": "steam"}
    chained = client.database.add_proposal(kind="alias", merchant_key="steam", payload={"alias_key": "steam", "merchant_key": "valve corp"})
    assert (await client.post(f"/api/intelligence/proposals/{chained}/resolve", json={"action": "accept"})).status_code == 422
    assert client.database.list_proposals()[0]["id"] == chained, "handed back open"


async def test_open_proposals_can_be_declined_in_bulk_by_kind(client):
    client.database.add_proposal(kind="rule", merchant_key="a", payload={})
    client.database.add_proposal(kind="rule", merchant_key="b", payload={})
    client.database.add_proposal(kind="alias", merchant_key="c", payload={})
    assert (await client.post("/api/intelligence/proposals/decline", json={"kind": "rule"})).json() == {"declined": 2}
    assert [p["kind"] for p in client.database.list_proposals()] == ["alias"]
    assert (await client.post("/api/intelligence/proposals/decline", json={})).json() == {"declined": 1}


async def test_a_history_proposal_run_is_a_categorize_job_with_a_flag(client):
    response = await client.post("/api/jobs", json={"kind": "categorize", "propose_rules": True})
    assert response.status_code == 202
    assert response.json()["job"]["params"] == {"propose_rules": True}


async def test_declining_a_proposal_declares_nothing(client):
    proposal_id = client.database.add_proposal(kind="rule", merchant_key="blue bottle", payload={"category_id": "cat-coffee"})
    response = await client.post(f"/api/intelligence/proposals/{proposal_id}/resolve", json={"action": "decline"})
    assert response.json() == {"status": "declined"}
    assert client.database.active_rules() == []
    assert client.database.list_proposals(status="declined")[0]["id"] == proposal_id


async def test_applying_a_review_with_always_declares_a_rule_and_withdraws_the_question(client, gateway):
    snapshot_with_categories(client)
    client.database.add_proposal(kind="rule", merchant_key="blue bottle", payload={})
    decision_id = client.database.add_decision(decision())
    response = await client.post(
        f"/api/reviews/{decision_id}/resolve",
        json={"action": "recategorize", "category_id": "cat-dining", "always": True},
    )
    assert response.status_code == 200
    rule = response.json()["rule"]
    assert rule["merchant_key"] == "blue bottle"
    assert rule["category_id"] == "cat-dining"
    assert gateway.updates[0]["category_id"] == "cat-dining"
    assert client.database.list_proposals() == []


async def test_applying_a_merchant_with_always_declares_one_rule_for_the_group(client, gateway):
    snapshot_with_categories(client)
    ids = [client.database.add_decision(decision(transaction_id=f"txn-{n}")) for n in range(3)]
    response = await client.post(
        "/api/reviews/resolve", json={"ids": ids, "action": "accept", "always": True}
    )
    assert response.json()["rules"] == 1
    assert len(client.database.active_rules()) == 1
    assert client.database.active_rules()[0]["category_id"] == "cat-coffee"


async def test_an_alias_can_be_taught_listed_and_forgotten(client):
    response = await client.post("/api/intelligence/aliases", json={"alias": "VALVE", "merchant": "Steam Games"})
    assert response.status_code == 201
    assert response.json()["alias"]["alias_key"] == "valve"
    assert response.json()["alias"]["merchant_key"] == "steam games"
    assert response.json()["alias"]["source"] == "taught"
    page = (await client.get("/api/intelligence")).json()
    assert [a["alias_key"] for a in page["aliases"]] == ["valve"]
    assert page["counts"]["aliases"] == 1
    same = await client.post("/api/intelligence/aliases", json={"alias": "Steam", "merchant": "STEAM #1"})
    assert same.status_code == 422
    chain = await client.post("/api/intelligence/aliases", json={"alias": "Steam Games", "merchant": "Valve Corp"})
    assert chain.status_code == 422
    assert (await client.delete("/api/intelligence/aliases/valve")).json() == {"deleted": True}
    assert (await client.delete("/api/intelligence/aliases/valve")).status_code == 404


async def test_the_page_lists_what_clerk_has_learned(client):
    client.database.record_memory("blue bottle", "cat-coffee", "Coffee")
    client.database.add_decision(decision(status="applied"))
    page = (await client.get("/api/intelligence")).json()
    [merchant] = page["merchants"]
    assert merchant["merchant_key"] == "blue bottle"
    assert merchant["label"] == "Blue Bottle"
    assert merchant["categories"][0]["category_name"] == "Coffee"
    assert merchant["last_seen"] is not None


async def test_the_old_actual_rule_endpoints_are_gone(client):
    assert (await client.get("/api/rules")).status_code == 404


# ------------------------------------------------------- takeover from Actual


def actual_with_rules(client, gateway):
    """A budget whose rule table holds one simple rule, one scoped rule, and one transfer rule."""
    snapshot_with_categories(client)
    gateway.snapshot_payload = {
        "accounts": [{"id": "acct-1", "name": "Checking", "closed": False, "off_budget": False, "sync_source": "", "external_id": "", "balance_cents": 0, "cleared_balance_cents": 0, "unconfirmed_transfers": [], "last_sync": None, "type": "checking", "last_transaction_date": None}],
        "categories": [
            {"id": "cat-coffee", "name": "Coffee", "group_name": "Everyday", "is_income": False, "hidden": False},
            {"id": "cat-dining", "name": "Dining", "group_name": "Everyday", "is_income": False, "hidden": False},
        ],
        "transactions": [
            {"id": f"t{n}", "date": "2026-08-01", "amount_cents": -500, "category_id": "cat-coffee", "category_name": "Coffee", "payee_name": "Blue Bottle", "imported_description": "", "merchant_key": "blue bottle", "account_id": "acct-1", "account_name": "Checking", "is_transfer": False, "is_starting_balance": False, "off_budget": False, "closed_account": False, "is_child": False}
            for n in range(3)
        ],
        "budgeted": {},
        "budgeted_history": {},
        "income_history": [],
        "tags": [],
        "history_start": "2026-01-01",
        "collected_at": "2026-08-21T00:00:00Z",
    }
    gateway.payees = [
        {"id": "p-bb", "name": "Blue Bottle", "transfer_account_id": ""},
        {"id": "p-sb", "name": "Starbucks", "transfer_account_id": ""},
        {"id": "p-xfer", "name": "", "transfer_account_id": "acct-2"},
    ]
    gateway.actual_rules = [
        {"id": "r-simple", "stage": "default", "conditions_op": "and", "conditions": [{"op": "is", "field": "payee", "value": "p-bb", "type": "id"}], "actions": [{"op": "set", "field": "category", "value": "cat-coffee", "type": "id"}]},
        {"id": "r-scoped", "stage": "default", "conditions_op": "and", "conditions": [{"op": "is", "field": "account", "value": "acct-1", "type": "id"}, {"op": "is", "field": "payee", "value": "p-sb", "type": "id"}], "actions": [{"op": "set", "field": "category", "value": "cat-dining", "type": "id"}]},
        {"id": "r-transfer", "stage": "default", "conditions_op": "and", "conditions": [{"op": "is", "field": "account", "value": "acct-1", "type": "id"}, {"op": "is", "field": "payee", "value": "p-bb", "type": "id"}], "actions": [{"op": "set", "field": "payee", "value": "p-xfer", "type": "id"}]},
    ]


async def test_reading_actual_classifies_and_replays_every_rule(client, gateway):
    actual_with_rules(client, gateway)
    body = (await client.get("/api/intelligence/actual")).json()
    assert body["counts"] == {"in_actual": 3, "movable": 2, "kept": 1, "imported_present": 0, "retired": 0}
    by_id = {rule["id"]: rule for rule in body["rules"]}
    simple = by_id["r-simple"]
    assert simple["disposition"] == "move"
    assert simple["translations"][0]["merchant_key"] == "blue bottle"
    assert simple["translations"][0]["replay"] == {"matched": 3, "agree": 3, "disagree": 0, "disagreeing": {}}
    assert simple["translations"][0]["existing"] is None
    scoped = by_id["r-scoped"]
    assert scoped["translations"][0]["account_name"] == "Checking"
    kept = by_id["r-transfer"]
    assert kept["disposition"] == "kept"
    assert "set the payee" in kept["reason"]
    assert kept["summary"] == "If account is Checking and payee is Blue Bottle, set the payee"


async def test_a_dry_run_import_changes_nothing(client, gateway):
    actual_with_rules(client, gateway)
    body = (await client.post("/api/intelligence/actual/import", json={"dry_run": True})).json()
    assert body["dry_run"] is True
    assert [item["merchant_key"] for item in body["imported"]] == ["blue bottle", "starbucks"]
    assert client.database.active_rules() == []


async def test_importing_copies_simple_rules_and_leaves_actual_alone(client, gateway):
    actual_with_rules(client, gateway)
    client.database.add_proposal(kind="rule", merchant_key="blue bottle", payload={})
    body = (await client.post("/api/intelligence/actual/import", json={})).json()
    assert len(body["imported"]) == 2 and body["skipped"] == []
    rules = {rule["merchant_key"]: rule for rule in client.database.active_rules()}
    assert rules["blue bottle"]["source"] == "imported"
    assert rules["blue bottle"]["actual_rule_id"] == "r-simple"
    assert rules["blue bottle"]["actual_status"] == "present"
    assert "r-simple" in rules["blue bottle"]["actual_rule_json"]
    assert rules["starbucks"]["account_id"] == "acct-1"
    assert len(gateway.actual_rules) == 3, "import never touches Actual"
    # The open question about that merchant is answered by the import.
    assert client.database.list_proposals() == []
    # Reading again shows them as managed, and nothing left to move.
    reading = (await client.get("/api/intelligence/actual")).json()
    assert reading["counts"]["movable"] == 0
    assert reading["counts"]["imported_present"] == 2
    again = (await client.post("/api/intelligence/actual/import", json={})).json()
    assert again["imported"] == []


async def test_importing_can_be_limited_to_named_rules_and_skips_problems(client, gateway):
    actual_with_rules(client, gateway)
    gateway.actual_rules.append({"id": "r-gone", "stage": "default", "conditions_op": "and", "conditions": [{"op": "is", "field": "payee", "value": "p-missing", "type": "id"}], "actions": [{"op": "set", "field": "category", "value": "cat-coffee", "type": "id"}]})
    body = (await client.post("/api/intelligence/actual/import", json={"rule_ids": ["r-simple", "r-gone", "r-transfer"]})).json()
    assert [item["merchant_key"] for item in body["imported"]] == ["blue bottle"]
    assert "no longer exists" in body["skipped"][0]["reason"]
    assert [rule["merchant_key"] for rule in client.database.active_rules()] == ["blue bottle"]


async def test_a_clerk_rule_that_disagrees_blocks_the_import_of_that_merchant(client, gateway):
    actual_with_rules(client, gateway)
    client.database.upsert_rule(merchant_key="blue bottle", category_id="cat-dining", category_name="Dining")
    reading = (await client.get("/api/intelligence/actual")).json()
    translation = next(rule for rule in reading["rules"] if rule["id"] == "r-simple")["translations"][0]
    assert translation["existing"]["agrees"] is False
    assert "already files this merchant as Dining" in translation["problem"]
    body = (await client.post("/api/intelligence/actual/import", json={"rule_ids": ["r-simple"]})).json()
    assert body["imported"] == [] and len(body["skipped"]) == 1


async def test_a_clerk_rule_that_agrees_is_adopted_as_the_imported_one(client, gateway):
    actual_with_rules(client, gateway)
    mine = client.database.upsert_rule(merchant_key="blue bottle", category_id="cat-coffee", category_name="Coffee")
    body = (await client.post("/api/intelligence/actual/import", json={"rule_ids": ["r-simple"]})).json()
    assert body["imported"][0]["already_in_clerk"] is True
    stored = client.database.get_rule(mine["id"])
    assert stored["source"] == "user" and stored["actual_rule_id"] == "r-simple"


async def test_retiring_deletes_imported_rules_from_actual_and_restore_brings_them_back(client, gateway):
    actual_with_rules(client, gateway)
    await client.post("/api/intelligence/actual/import", json={})
    preview = (await client.post("/api/intelligence/actual/retire", json={"dry_run": True})).json()
    assert sorted(item["actual_rule_id"] for item in preview["retired"]) == ["r-scoped", "r-simple"]
    assert len(gateway.actual_rules) == 3

    body = (await client.post("/api/intelligence/actual/retire", json={})).json()
    assert sorted(item["actual_rule_id"] for item in body["retired"]) == ["r-scoped", "r-simple"]
    assert sorted(gateway.deleted_rules) == ["r-scoped", "r-simple"]
    assert [rule["id"] for rule in gateway.actual_rules] == ["r-transfer"], "the transfer rule stays"
    assert {rule["actual_status"] for rule in client.database.active_rules()} == {"retired"}
    reading = (await client.get("/api/intelligence/actual")).json()
    assert reading["counts"] == {"in_actual": 1, "movable": 0, "kept": 1, "imported_present": 0, "retired": 2}
    # Retiring again finds nothing to do.
    assert (await client.post("/api/intelligence/actual/retire", json={})).json()["retired"] == []

    restored = (await client.post("/api/intelligence/actual/restore", json={"rule_ids": ["r-simple"]})).json()
    assert restored["restored"][0]["new_actual_rule_id"] == "restored-1"
    assert gateway.restored_rules[0]["id"] == "r-simple"
    assert gateway.restored_rules[0]["conditions"][0]["value"] == "p-bb"
    blue = next(rule for rule in client.database.active_rules() if rule["merchant_key"] == "blue bottle")
    assert blue["actual_status"] == "restored" and blue["actual_rule_id"] == "restored-1"
    # A restored rule is present in Actual again and can be retired again.
    again = (await client.post("/api/intelligence/actual/retire", json={"dry_run": True})).json()
    assert [item["actual_rule_id"] for item in again["retired"]] == ["restored-1"]


async def test_a_paused_clerk_rule_keeps_its_actual_rule_in_place(client, gateway):
    actual_with_rules(client, gateway)
    await client.post("/api/intelligence/actual/import", json={})
    blue = next(rule for rule in client.database.active_rules() if rule["merchant_key"] == "blue bottle")
    client.database.update_rule(blue["id"], status="paused")
    body = (await client.post("/api/intelligence/actual/retire", json={})).json()
    assert [item["actual_rule_id"] for item in body["retired"]] == ["r-scoped"]
    assert "r-simple" in [rule["id"] for rule in gateway.actual_rules]


async def test_a_failed_delete_is_reported_and_leaves_the_rule_present(client, gateway):
    actual_with_rules(client, gateway)
    await client.post("/api/intelligence/actual/import", json={})
    gateway.error = ActualGatewayError("Actual is down")
    body = (await client.post("/api/intelligence/actual/retire", json={"rule_ids": ["r-simple"]})).json()
    assert body["retired"] == []
    assert body["failed"][0]["actual_rule_id"] == "r-simple"
    blue = next(rule for rule in client.database.active_rules() if rule["merchant_key"] == "blue bottle")
    assert blue["actual_status"] == "present"


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


async def test_a_rejected_field_is_named_so_the_form_can_be_fixed(client):
    """One bad box rejects the whole form, so the message must say which box.

    Every value in the settings page is saved in one request. A raw pydantic
    dump in a toast leaves the reader with a wall of text and no idea that,
    say, the time zone they typed is why their new digest time did not save.
    """

    bad = await client.patch(
        "/api/settings",
        json={"values": {"timezone": "Mars/Olympus", "digest_time": "07:30"}},
    )
    assert bad.status_code == 422
    detail = bad.json()["detail"]
    assert "timezone" in detail
    assert "unknown time zone: Mars/Olympus" in detail
    assert "\n" not in detail

    # And nothing was saved, including the value that was perfectly valid.
    assert (await client.get("/api/settings")).json()["timezone"] == "UTC"


async def test_the_report_options_round_trip_through_the_api(client):
    """Unchecking a box has to reach the server as false, not go missing."""
    before = (await client.get("/api/settings")).json()
    assert before["digest_title"] == "The Morning Report"
    assert before["digest_show_pace"] is True
    assert before["digest_show_balances"] is False

    response = await client.patch(
        "/api/settings",
        json={
            "values": {
                "digest_title": "Budget o'clock",
                "digest_show_pace": False,
                "digest_show_projection": True,
                "digest_show_balances": True,
            }
        },
    )
    saved = response.json()["settings"]
    assert saved["digest_title"] == "Budget o'clock"
    assert saved["digest_show_pace"] is False
    assert saved["digest_show_projection"] is True
    assert saved["digest_show_balances"] is True


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


async def test_a_review_retry_is_marked_as_targeting_the_queue(client):
    response = await client.post("/api/jobs", json={"kind": "categorize", "reviews": True})
    assert response.status_code == 202
    assert response.json()["job"]["params"] == {"reviews": True}


async def test_a_categorize_job_cannot_mix_history_and_review_scopes(client):
    response = await client.post(
        "/api/jobs", json={"kind": "categorize", "full": True, "reviews": True}
    )
    assert response.status_code == 422


async def test_the_full_flag_is_meaningless_for_other_job_kinds(client):
    response = await client.post("/api/jobs", json={"kind": "sync", "full": True})
    assert response.json()["job"]["params"] == {}


async def test_the_reviews_flag_is_meaningless_for_other_job_kinds(client):
    response = await client.post("/api/jobs", json={"kind": "sync", "reviews": True})
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
    html = (await client.get("/")).text
    for name in ("app.js", "styles.css", "favicon.svg"):
        assert f"/assets/{name}?v={STATIC_ASSET_VERSION}" in html
        assert f'"/assets/{name}"' not in html, "an unstamped URL would keep the old cache alive"


async def test_index_is_never_cached_without_revalidating(client):
    response = await client.get("/")
    assert response.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize("name", ("app.js", "styles.css", "favicon.svg"))
async def test_current_fingerprinted_assets_are_cached_immutably(client, name):
    response = await client.get(f"/assets/{name}?v={STATIC_ASSET_VERSION}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


@pytest.mark.parametrize("query", ("", "?v=incorrect"))
async def test_unversioned_or_incorrectly_versioned_assets_are_revalidated(client, query):
    response = await client.get(f"/assets/app.js{query}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"


async def test_revalidating_an_unchanged_asset_costs_no_body(client):
    first = await client.get("/assets/app.js")
    again = await client.get("/assets/app.js", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304
    assert not again.content


async def test_bank_recheck_syncs_while_global_connection_checks_remain_read_only(client):
    source = (await client.get("/assets/app.js")).text
    assert 'action === "sync-and-recheck"' in source
    assert 'enqueue("sync", "Bank sync and connection check")' in source
    assert 'action === "run-health" || action === "check-connections"' in source
    assert 'enqueue("health", "Connection check")' in source


async def test_activity_defaults_to_one_live_combined_timeline(client):
    source = (await client.get("/assets/app.js")).text
    assert 'activityView: "all"' in source
    assert 'data-view="all">All (' in source
    assert 'data-view="decisions">Filing decisions (' in source
    assert 'data-view="tasks">Tasks (' in source
    assert 'type: "job"' in source
    assert 'type: "decision"' in source
    assert 'renderActivity({ poll: true, refreshDrawer: Boolean(state.openJobId) })' in source
    assert 'state.route === "activity" ? 3000 : 8000' in source
    assert "Runs (" not in source


async def test_review_category_picker_is_grouped_and_filters_as_you_type(client):
    source = (await client.get("/assets/app.js")).text
    styles = (await client.get("/assets/styles.css")).text
    assert 'placeholder="Search categories"' in source
    assert 'class="category-picker-group"' in source
    assert 'data-action="category-picker-select"' in source
    assert "function filterCategoryPicker(picker)" in source
    assert 'option.hidden = !option.dataset.search.includes(query)' in source
    assert ".category-picker-options" in styles
    assert ".category-picker-option.selected" in styles


async def test_settings_offer_one_checkbox_for_all_monitored_account_balances(client):
    source = (await client.get("/assets/app.js")).text
    assert 'settingCheck("digest_show_balances", "Account balances"' in source
    assert "One switch controls the whole list." in source


@pytest.mark.parametrize("changed_name", ("app.js", "styles.css", "favicon.svg"))
def test_asset_version_changes_when_any_asset_changes(tmp_path: Path, changed_name: str):
    for name in ("app.js", "styles.css", "favicon.svg"):
        (tmp_path / name).write_text(f"initial {name}")
    initial = _static_asset_version(tmp_path)
    assert _static_asset_version(tmp_path) == initial

    (tmp_path / changed_name).write_text(f"changed {changed_name}")

    assert len(initial) == 16
    assert _static_asset_version(tmp_path) != initial


def test_asset_version_includes_asset_names(tmp_path: Path):
    original = tmp_path / "app.js"
    original.write_text("same content")
    initial = _static_asset_version(tmp_path)

    original.rename(tmp_path / "renamed.js")

    assert _static_asset_version(tmp_path) != initial


# ------------------------------------------------------- bulk review resolve


async def test_a_whole_merchant_is_applied_in_one_write(client, gateway):
    """A backlog is cleared a merchant at a time, in a single batch."""
    ids = [client.database.add_decision(decision(transaction_id=f"txn-{n}")) for n in range(3)]
    response = await client.post(
        "/api/reviews/resolve", json={"ids": ids, "action": "accept"}
    )
    assert response.status_code == 200
    assert response.json() == {"status": "applied", "resolved": 3, "skipped": 0, "rules": 0}
    assert len(gateway.updates) == 3, "one batch, not one call per transaction"
    assert {item["transaction_id"] for item in gateway.updates} == {"txn-0", "txn-1", "txn-2"}
    for decision_id in ids:
        assert client.database.get_decision(decision_id)["status"] == "applied"


async def test_bulk_dismiss_resolves_without_touching_actual(client, gateway):
    ids = [client.database.add_decision(decision(transaction_id=f"txn-{n}")) for n in range(4)]
    response = await client.post(
        "/api/reviews/resolve", json={"ids": ids, "action": "dismiss"}
    )
    assert response.json() == {"status": "dismissed", "resolved": 4}
    assert gateway.updates == []
    assert client.database.get_decision(ids[0])["status"] == "dismissed"


async def test_bulk_recategorize_applies_one_category_to_all(client, gateway):
    ids = [client.database.add_decision(decision(transaction_id=f"txn-{n}")) for n in range(2)]
    response = await client.post(
        "/api/reviews/resolve",
        json={"ids": ids, "action": "recategorize", "category_id": "cat-groceries"},
    )
    assert response.status_code == 200
    assert {item["category_id"] for item in gateway.updates} == {"cat-groceries"}


async def test_bulk_recategorize_needs_a_category(client):
    ids = [client.database.add_decision(decision())]
    response = await client.post(
        "/api/reviews/resolve", json={"ids": ids, "action": "recategorize"}
    )
    assert response.status_code == 422


async def test_an_already_resolved_review_is_skipped_not_rewritten(client, gateway):
    first = client.database.add_decision(decision(transaction_id="txn-1"))
    second = client.database.add_decision(decision(transaction_id="txn-2"))
    client.database.resolve_decision(first, "dismissed")
    response = await client.post(
        "/api/reviews/resolve", json={"ids": [first, second], "action": "accept"}
    )
    assert response.json()["resolved"] == 1
    assert [item["transaction_id"] for item in gateway.updates] == ["txn-2"]


async def test_a_failed_batch_hands_every_claim_back(client, gateway):
    """Nothing may be left recorded as applied that Actual never received."""
    ids = [client.database.add_decision(decision(transaction_id=f"txn-{n}")) for n in range(3)]
    gateway.error = ActualGatewayError("Actual is unreachable")
    response = await client.post(
        "/api/reviews/resolve", json={"ids": ids, "action": "accept"}
    )
    assert response.status_code == 502
    for decision_id in ids:
        assert client.database.get_decision(decision_id)["status"] == "needs_review"


async def test_bulk_resolve_rejects_an_empty_list(client):
    response = await client.post("/api/reviews/resolve", json={"ids": [], "action": "dismiss"})
    assert response.status_code == 422


# --------------------------------------------------------------------- plaid


@pytest.fixture
def plaid(client, monkeypatch):
    """Plaid configured, every network call answered locally, calls recorded."""
    client.settings_manager.update({"plaid_client_id": "client-1", "plaid_secret": "secret-1"})
    calls: list[tuple[str, Any]] = []
    state = {
        "item_error": None,
        "accounts": [
            {"id": "plaid-chk", "item_id": "item-1", "name": "Checking", "official_name": "Plaid checking",
             "mask": "8193", "type": "depository", "subtype": "checking", "currency": "USD",
             "balance_cents": 50000, "available_cents": 44200, "limit_cents": None, "balance_updated": None},
        ],
    }

    async def create_link_token(self, *, access_token=None, account_selection=False):
        calls.append(("link_token", access_token, account_selection))
        return {"link_token": "link-1", "expiration": "soon", "update_mode": bool(access_token), "environment": "sandbox"}

    async def exchange_public_token(self, public_token):
        calls.append(("exchange", public_token))
        return {"access_token": "access-1", "item_id": "item-1"}

    async def sandbox_create_item(self, *, institution_id="ins_109508", username="user_transactions_dynamic", password="pass_good"):
        calls.append(("sandbox_item", institution_id, username))
        return {"access_token": "access-sb", "item_id": "item-sb", "institution_id": institution_id}

    async def get_item(self, access_token):
        calls.append(("item", access_token))
        return {"item_id": "item-1", "institution_id": "ins_1", "institution_name": "Platypus", "error": state["item_error"],
                "consent_expiration_time": None, "products": [], "billed_products": [], "last_successful_update": None, "last_failed_update": None}

    async def get_accounts(self, access_token):
        calls.append(("accounts", access_token))
        return {"item": {"item_id": "item-1"}, "accounts": state["accounts"]}

    async def remove_item(self, access_token):
        calls.append(("remove", access_token))
        return True

    async def sandbox_reset_login(self, access_token):
        calls.append(("reset", access_token))
        return True

    for name, function in {
        "create_link_token": create_link_token, "exchange_public_token": exchange_public_token,
        "sandbox_create_item": sandbox_create_item, "get_item": get_item, "get_accounts": get_accounts,
        "remove_item": remove_item, "sandbox_reset_login": sandbox_reset_login,
    }.items():
        monkeypatch.setattr(f"actual_clerk.clients.plaid.PlaidClient.{name}", function)
    return {"calls": calls, "state": state}


async def test_plaid_endpoints_refuse_until_configured(client):
    response = await client.post("/api/plaid/link-token", json={})
    assert response.status_code == 409
    listing = await client.get("/api/plaid/items")
    assert listing.status_code == 200
    assert listing.json()["configured"] is False
    assert listing.json()["items"] == []


async def test_a_link_token_is_minted_for_a_new_connection_or_a_repair(client, plaid):
    fresh = await client.post("/api/plaid/link-token", json={})
    assert fresh.status_code == 200
    assert fresh.json()["update_mode"] is False
    assert (await client.post("/api/plaid/link-token", json={"item_id": "nope"})).status_code == 404
    client.database.upsert_plaid_item({"item_id": "item-1", "access_token": "access-1"})
    repair = await client.post("/api/plaid/link-token", json={"item_id": "item-1", "account_selection": True})
    assert repair.json()["update_mode"] is True
    assert ("link_token", "access-1", True) in plaid["calls"]


async def test_exchanging_a_public_token_stores_the_item_without_its_token(client, plaid):
    response = await client.post(
        "/api/plaid/exchange",
        json={"public_token": "public-sandbox-1", "institution_name": "Given", "accounts": [{"id": "plaid-chk", "name": "Checking"}]},
    )
    assert response.status_code == 201, response.text
    item = response.json()["item"]
    assert item["item_id"] == "item-1"
    assert item["institution_name"] == "Platypus", "Plaid's own description wins over Link metadata"
    assert "access_token" not in item
    stored = client.database.get_plaid_item("item-1")
    assert stored["access_token"] == "access-1"
    assert stored["environment"] == "sandbox"
    assert client.database.last_job("health")["status"] == "queued"


async def test_a_sandbox_item_can_be_created_without_link(client, plaid):
    response = await client.post("/api/plaid/sandbox/items", json={"username": "user_good"})
    assert response.status_code == 201, response.text
    assert response.json()["item"]["item_id"] == "item-sb"
    assert ("sandbox_item", "ins_109508", "user_good") in plaid["calls"]


async def test_items_are_listed_with_accounts_mappings_and_slot_usage(client, plaid):
    client.database.upsert_plaid_item({"item_id": "item-1", "access_token": "access-1", "institution_name": "Platypus"})
    client.database.upsert_bank_link({"actual_account_id": "acct-1", "provider": "plaid", "item_id": "item-1", "external_account_id": "plaid-chk", "cutover_date": "2026-09-01"})
    client.database.set_snapshot(OVERVIEW_SNAPSHOT, {"accounts": [
        {"id": "acct-1", "name": "Checking", "off_budget": False, "closed": False, "sync_source": "plaid", "actual_sync_source": "simpleFin"},
        {"id": "acct-2", "name": "Closed", "off_budget": False, "closed": True},
    ]})
    response = await client.get("/api/plaid/items")
    body = response.json()
    assert body["configured"] is True
    assert body["slots"] == {"used": 1, "limit": None}
    [item] = body["items"]
    assert item["status"] == "ok"
    assert "access_token" not in item
    [account] = item["accounts"]
    assert account["link"]["actual_account_id"] == "acct-1"
    assert account["link"]["cutover_date"] == "2026-09-01"
    [actual] = body["actual_accounts"]
    assert actual["actual_sync_source"] == "simpleFin"
    assert actual["linked_to"]["external_account_id"] == "plaid-chk"


async def test_a_broken_item_is_recorded_as_needing_repair_and_then_cleared(client, plaid):
    client.database.upsert_plaid_item({"item_id": "item-1", "access_token": "access-1"})
    plaid["state"]["item_error"] = {"error_type": "ITEM_ERROR", "error_code": "ITEM_LOGIN_REQUIRED", "error_message": "log in", "display_message": "", "needs_repair": True}
    listing = await client.get("/api/plaid/items")
    assert listing.json()["items"][0]["needs_repair"] is True
    assert client.database.get_plaid_item("item-1")["status"] == "needs_repair"
    still_broken = await client.post("/api/plaid/items/item-1/repaired")
    assert still_broken.json()["repaired"] is False
    plaid["state"]["item_error"] = None
    repaired = await client.post("/api/plaid/items/item-1/repaired")
    assert repaired.json()["repaired"] is True
    assert client.database.get_plaid_item("item-1")["status"] == "ok"


async def test_mapping_a_plaid_account_onto_an_existing_actual_account(client, plaid):
    client.database.upsert_plaid_item({"item_id": "item-1", "access_token": "access-1", "institution_name": "Platypus"})
    response = await client.post(
        "/api/plaid/links",
        json={"item_id": "item-1", "external_account_id": "plaid-chk", "actual_account_id": "acct-1", "cutover_date": "2026-09-01"},
    )
    assert response.status_code == 201, response.text
    link = response.json()["link"]
    assert link["external_name"] == "Checking"
    assert link["mask"] == "8193"
    assert link["institution"] == "Platypus"
    assert link["cutover_date"] == "2026-09-01"
    assert link["enabled"] is True
    unknown = await client.post("/api/plaid/links", json={"item_id": "item-1", "external_account_id": "plaid-nope", "actual_account_id": "acct-1"})
    assert unknown.status_code == 404
    taken = await client.post("/api/plaid/links", json={"item_id": "item-1", "external_account_id": "plaid-chk", "actual_account_id": "acct-2"})
    assert taken.status_code == 409
    both = await client.post("/api/plaid/links", json={"item_id": "item-1", "external_account_id": "plaid-chk", "actual_account_id": "acct-1", "new_account": {"name": "New"}})
    assert both.status_code == 422
    bad_date = await client.post("/api/plaid/links", json={"item_id": "item-1", "external_account_id": "plaid-chk", "actual_account_id": "acct-1", "cutover_date": "yesterday"})
    assert bad_date.status_code == 422


async def test_mapping_onto_a_new_actual_account_creates_it_first(client, plaid, gateway):
    client.database.upsert_plaid_item({"item_id": "item-1", "access_token": "access-1"})
    response = await client.post(
        "/api/plaid/links",
        json={"item_id": "item-1", "external_account_id": "plaid-chk", "new_account": {"name": "Plaid Checking", "off_budget": True}},
    )
    assert response.status_code == 201, response.text
    assert gateway.created_accounts == [("Plaid Checking", True)]
    assert response.json()["created_account"]["id"] == "acct-new"
    assert response.json()["link"]["actual_account_id"] == "acct-new"
    assert response.json()["link"]["cutover_date"]


async def test_a_mapping_can_be_paused_moved_and_forgotten(client, plaid):
    client.database.upsert_plaid_item({"item_id": "item-1", "access_token": "access-1"})
    await client.post("/api/plaid/links", json={"item_id": "item-1", "external_account_id": "plaid-chk", "actual_account_id": "acct-1"})
    updated = await client.patch("/api/plaid/links/acct-1", json={"enabled": False, "cutover_date": "2026-08-15"})
    assert updated.json()["link"]["enabled"] is False
    assert updated.json()["link"]["cutover_date"] == "2026-08-15"
    assert (await client.patch("/api/plaid/links/ghost", json={"enabled": True})).status_code == 404
    assert (await client.delete("/api/plaid/links/acct-1")).json()["deleted"] is True
    assert (await client.delete("/api/plaid/links/acct-1")).status_code == 404


async def test_removing_an_item_disconnects_at_plaid_and_disables_its_mappings(client, plaid):
    client.database.upsert_plaid_item({"item_id": "item-1", "access_token": "access-1"})
    client.database.upsert_bank_link({"actual_account_id": "acct-1", "provider": "plaid", "item_id": "item-1", "external_account_id": "plaid-chk"})
    response = await client.post("/api/plaid/items/item-1/remove")
    assert response.json() == {"removed": True, "links_disabled": 1, "plaid_error": ""}
    assert ("remove", "access-1") in plaid["calls"]
    assert client.database.get_bank_link("acct-1")["enabled"] is False
    assert (await client.post("/api/plaid/items/item-1/remove")).status_code == 404


async def test_a_sandbox_login_can_be_broken_on_purpose(client, plaid):
    client.database.upsert_plaid_item({"item_id": "item-1", "access_token": "access-1"})
    assert (await client.post("/api/plaid/sandbox/items/item-1/reset-login")).json() == {"reset": True}
    assert ("reset", "access-1") in plaid["calls"]


async def test_the_plaid_connection_test_reports_the_environment(client, plaid):
    response = await client.post("/api/settings/test/plaid")
    assert response.status_code == 200
    assert response.json()["environment"] == "sandbox"


async def test_plaid_failures_carry_their_error_code(client, plaid, monkeypatch):
    from actual_clerk.clients.plaid import PlaidError

    async def broken(self, *args, **kwargs):
        raise PlaidError("Plaid API_ERROR/INTERNAL: down", error_type="API_ERROR", error_code="INTERNAL", retryable=True)

    monkeypatch.setattr("actual_clerk.clients.plaid.PlaidClient.create_link_token", broken)
    response = await client.post("/api/plaid/link-token", json={})
    assert response.status_code == 502
    assert response.json()["error_code"] == "INTERNAL"


async def test_a_paused_mapping_is_replaced_by_mapping_the_account_again(client, plaid):
    """Remove a connection, reconnect the bank, map the same Actual account onto the new Item."""
    client.database.upsert_plaid_item({"item_id": "item-old", "access_token": "access-old"})
    client.database.upsert_bank_link({"actual_account_id": "acct-1", "provider": "plaid", "item_id": "item-old", "external_account_id": "plaid-old"})
    client.database.remove_plaid_item("item-old")
    client.database.upsert_plaid_item({"item_id": "item-1", "access_token": "access-1"})
    client.database.set_snapshot(OVERVIEW_SNAPSHOT, {"accounts": [{"id": "acct-1", "name": "Checking", "off_budget": False, "closed": False}]})
    listing = await client.get("/api/plaid/items")
    assert listing.json()["actual_accounts"][0]["linked_to"] is None
    response = await client.post("/api/plaid/links", json={"item_id": "item-1", "external_account_id": "plaid-chk", "actual_account_id": "acct-1"})
    assert response.status_code == 201, response.text
    link = client.database.get_bank_link("acct-1")
    assert link["item_id"] == "item-1"
    assert link["external_account_id"] == "plaid-chk"
    assert link["enabled"] is True


# ----------------------------------------------------------------- migration


@pytest.fixture
def feeds(client, gateway, plaid, monkeypatch):
    """One SimpleFIN-linked account, one manual account, one Plaid Item, a server token."""
    gateway.detailed_accounts = [
        {"id": "acct-sf", "name": "Checking", "closed": False, "off_budget": False, "sync_source": "simpleFin", "external_id": "sf-1", "bank_name": "Bank"},
        {"id": "acct-manual", "name": "Cash", "closed": False, "off_budget": False, "sync_source": "", "external_id": "", "bank_name": ""},
        {"id": "acct-closed", "name": "Old", "closed": True, "off_budget": False, "sync_source": "", "external_id": "", "bank_name": ""},
    ]
    gateway.server_accounts = [{"account_id": "sf-1", "name": "CHECKING (0010)", "institution": "Bank", "org_domain": "bank.example", "org_id": "", "balance": "12.00", "currency": "USD", "balance_date": 1}]
    client.database.upsert_plaid_item({"item_id": "item-1", "access_token": "access-1", "institution_name": "Platypus"})

    async def sync_page(self, access_token, *, cursor="", count=500):
        return {"added": [{"transaction_id": "p1", "account_id": "plaid-chk", "amount": 4.5, "date": "2026-09-02", "name": "SHOP", "pending": False}],
                "modified": [], "removed": [], "next_cursor": "c1", "has_more": False, "update_status": "HISTORICAL_UPDATE_COMPLETE",
                "accounts": [{"id": "plaid-chk", "balance_cents": 50000}]}

    monkeypatch.setattr("actual_clerk.clients.plaid.PlaidClient.transactions_sync_page", sync_page)
    return gateway


async def test_the_migration_overview_derives_each_accounts_feed_state(client, feeds):
    client.database.upsert_bank_link({"actual_account_id": "acct-manual", "provider": "plaid", "item_id": "item-1", "external_account_id": "plaid-chk", "enabled": False, "previous_external_id": "sf-old"})
    body = (await client.get("/api/migration")).json()
    by_id = {row["id"]: row for row in body["accounts"]}
    assert set(by_id) == {"acct-sf", "acct-manual"}, "closed accounts are left out"
    assert by_id["acct-sf"]["state"] == "simplefin"
    assert by_id["acct-sf"]["simplefin_account"]["name"] == "CHECKING (0010)"
    assert by_id["acct-manual"]["state"] == "plaid_paused"
    assert body["simplefin_server"]["configured"] is True
    assert body["plaid"]["items"][0]["item_id"] == "item-1"
    assert body["clerk_simplefin_configured"] is False


async def test_moving_to_plaid_previews_then_unlinks_maps_and_syncs(client, feeds):
    preview = await client.post("/api/migration/to-plaid", json={"actual_account_id": "acct-sf", "item_id": "item-1", "external_account_id": "plaid-chk", "cutover_date": "2026-09-01", "dry_run": True})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["dry_run"] is True
    assert body["will_unlink_actual"] is True
    assert body["preview"]["would_import"] == 1
    assert body["preview"]["opening_balance_cents"] == 50000 + 450
    assert feeds.unlinked if hasattr(feeds, "unlinked") else True
    assert client.database.get_bank_link("acct-sf") is None, "a dry run writes nothing"
    assert getattr(feeds, "unlinked", []) == []

    applied = await client.post("/api/migration/to-plaid", json={"actual_account_id": "acct-sf", "item_id": "item-1", "external_account_id": "plaid-chk", "cutover_date": "2026-09-01"})
    assert applied.status_code == 200, applied.text
    assert applied.json()["unlinked_actual"] is True
    assert feeds.unlinked == ["acct-sf"]
    link = client.database.get_bank_link("acct-sf")
    assert link["enabled"] is True
    assert link["previous_provider"] == "simpleFin"
    assert link["previous_external_id"] == "sf-1"
    assert link["cutover_date"] == "2026-09-01"
    assert client.database.last_job("sync")["status"] == "queued"


async def test_moving_to_plaid_can_keep_actuals_link_and_refuses_a_taken_account(client, feeds):
    kept = await client.post("/api/migration/to-plaid", json={"actual_account_id": "acct-sf", "item_id": "item-1", "external_account_id": "plaid-chk", "unlink_actual": False, "dry_run": True})
    assert kept.json()["keeps_actual_link"] is True
    client.database.upsert_bank_link({"actual_account_id": "acct-manual", "provider": "plaid", "item_id": "item-1", "external_account_id": "plaid-chk"})
    taken = await client.post("/api/migration/to-plaid", json={"actual_account_id": "acct-sf", "item_id": "item-1", "external_account_id": "plaid-chk"})
    assert taken.status_code == 409
    missing = await client.post("/api/migration/to-plaid", json={"actual_account_id": "acct-ghost", "item_id": "item-1", "external_account_id": "plaid-chk"})
    assert missing.status_code == 404


async def test_moving_back_to_simplefin_pauses_the_mapping_and_relinks_from_the_date(client, feeds):
    client.database.upsert_bank_link({"actual_account_id": "acct-manual", "provider": "plaid", "item_id": "item-1", "external_account_id": "plaid-chk", "cutover_date": "2026-09-01", "previous_provider": "simpleFin", "previous_external_id": "sf-1"})
    preview = await client.post("/api/migration/to-simplefin", json={"actual_account_id": "acct-manual", "dry_run": True})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["simplefin_account"]["account_id"] == "sf-1", "the remembered account is preselected"
    assert body["starting_date"] == "2026-09-01"
    assert body["will_pause_plaid_link"] is True
    assert body["warnings"] == []
    assert not hasattr(feeds, "relinked")

    applied = await client.post("/api/migration/to-simplefin", json={"actual_account_id": "acct-manual", "starting_date": "2026-09-05"})
    assert applied.status_code == 200, applied.text
    assert feeds.relinked == [("acct-manual", "sf-1", datetime.date(2026, 9, 5))]
    assert client.database.get_bank_link("acct-manual")["enabled"] is False
    assert client.database.last_job("sync")["status"] == "queued"


async def test_moving_back_to_simplefin_needs_a_server_token_and_a_known_account(client, feeds):
    feeds.server_configured = False
    blocked = await client.post("/api/migration/to-simplefin", json={"actual_account_id": "acct-manual", "simplefin_account_id": "sf-1"})
    assert blocked.status_code == 409
    assert "no SimpleFIN token" in blocked.json()["detail"]
    feeds.server_configured = True
    unknown = await client.post("/api/migration/to-simplefin", json={"actual_account_id": "acct-manual", "simplefin_account_id": "sf-nope", "dry_run": True})
    assert any("did not return" in warning for warning in unknown.json()["warnings"])
    already = await client.post("/api/migration/to-simplefin", json={"actual_account_id": "acct-sf", "simplefin_account_id": "sf-1", "dry_run": True})
    assert any("already links" in warning for warning in already.json()["warnings"])


async def test_the_server_simplefin_token_can_be_stored_and_removed(client, feeds):
    status = (await client.get("/api/simplefin/server?accounts=true")).json()
    assert status["configured"] is True
    assert status["accounts"][0]["account_id"] == "sf-1"
    stored = await client.post("/api/simplefin/server-token", json={"setup_token": "aHR0cHM6Ly9icmlkZ2Uu c2ltcGxlZmluLm9yZy8"})
    assert stored.status_code == 200
    assert feeds.secrets[-1] == ("simplefin_token", "aHR0cHM6Ly9icmlkZ2Uuc2ltcGxlZmluLm9yZy8")
    removed = await client.delete("/api/simplefin/server-token")
    assert removed.json()["configured"] is False
    assert feeds.secrets[-2:] == [("simplefin_token", None), ("simplefin_accessKey", None)]
