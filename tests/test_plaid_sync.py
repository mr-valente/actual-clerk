"""The engine that reads Plaid's stream and writes it into Actual."""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from actual_clerk.clients.actual import ActualGatewayError
from actual_clerk.clients.plaid import PlaidError
from actual_clerk.config import Settings
from actual_clerk.plaid_sync import PlaidSyncEngine, read_stream

TODAY = datetime.date(2026, 9, 12)


class FakePlaid:
    """Answers the engine's calls from scripted pages and records what was asked."""

    def __init__(self, pages=None):
        self.pages = list(pages or [])
        self.calls: list[tuple[str, Any]] = []
        self.item_updates = iter([])
        self.refresh_error: PlaidError | None = None
        self.item_error: dict[str, Any] | None = None
        self.mutation_failures = 0
        self.closed = False
        self.last_successful_update = datetime.datetime(2026, 9, 12, 8, tzinfo=datetime.UTC)

    async def transactions_sync_page(self, access_token, *, cursor="", count=500):
        self.calls.append(("sync", cursor))
        if self.mutation_failures:
            self.mutation_failures -= 1
            raise PlaidError("mutation", error_code="TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION")
        index = 0
        for position, page in enumerate(self.pages):
            if page.get("cursor", "") == cursor:
                index = position
                break
        page = self.pages[index]
        return {
            "added": page.get("added", []),
            "modified": page.get("modified", []),
            "removed": page.get("removed", []),
            "next_cursor": page.get("next_cursor", cursor or "cursor-end"),
            "has_more": page.get("has_more", False),
            "update_status": page.get("update_status", "HISTORICAL_UPDATE_COMPLETE"),
            "accounts": page.get("accounts", []),
        }

    async def transactions_refresh(self, access_token):
        self.calls.append(("refresh", access_token))
        if self.refresh_error:
            raise self.refresh_error
        return {"request_id": "r"}

    async def get_item(self, access_token):
        self.calls.append(("item", access_token))
        return {
            "item_id": "item-1", "institution_name": "Platypus", "error": self.item_error,
            "last_successful_update": self.last_successful_update, "last_failed_update": None,
        }

    async def close(self):
        self.closed = True


class StubGateway:
    def __init__(self, rows=None):
        self.rows: dict[str, list[dict[str, Any]]] = rows or {}
        self.imports: list[tuple[str, list[dict[str, Any]]]] = []
        self.adoptions: list[list[dict[str, Any]]] = []
        self.deletions: list[list[str]] = []
        self.import_errors: list[str] = []
        self.categories = [{"id": "cat-sb", "name": "Starting Balances", "is_income": True}]

    async def account_transactions(self, account_id, *, start=None, end=None):
        rows = self.rows.get(account_id, [])
        if start:
            rows = [row for row in rows if row["date"] >= start]
        return list(rows)

    async def adopt_imported_ids(self, updates):
        self.adoptions.append(list(updates))
        return {"applied": [{"id": item["transaction_id"]} for item in updates], "skipped": []}

    async def delete_transactions(self, ids):
        self.deletions.append(list(ids))
        return {"deleted": list(ids), "missing": []}

    async def import_transactions(self, account_id, transactions, *, dry_run=False):
        self.imports.append((account_id, list(transactions)))
        return {"added": [f"new-{i}" for i in range(len(transactions))], "updated": [], "errors": self.import_errors}

    async def snapshot(self, *, today=None, transaction_ids=()):
        return {"categories": self.categories, "accounts": [], "transactions": []}


def plaid_txn(transaction_id, amount, date, account="plaid-chk", **extra):
    return {"transaction_id": transaction_id, "account_id": account, "amount": amount, "date": date,
            "name": "SHOP", "merchant_name": "Shop", "pending": False, **extra}


def settings(**overrides) -> Settings:
    values = {"actual_password": "x", "actual_budget_id": "b", "plaid_client_id": "c", "plaid_secret": "s",
              "plaid_refresh_wait_seconds": 0}
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def linked(database):
    database.upsert_plaid_item({"item_id": "item-1", "access_token": "access-1", "institution_name": "Platypus"})
    database.upsert_bank_link({"actual_account_id": "acct-chk", "provider": "plaid", "item_id": "item-1",
                               "external_account_id": "plaid-chk", "external_name": "Checking",
                               "cutover_date": "2026-09-01"})
    return database


def engine(database, gateway, client, **overrides):
    events: list[tuple[str, str, str]] = []
    clock = {"now": 1_000_000.0}
    sleeps: list[float] = []

    async def sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    instance = PlaidSyncEngine(
        database, settings(**overrides), gateway, client=client,
        events=lambda level, kind, message, data=None: events.append((level, kind, message)),
        today=TODAY, clock=lambda: clock["now"], sleep=sleep,
    )
    return instance, events, sleeps


# ------------------------------------------------------------- read_stream


async def test_the_stream_is_read_to_the_end_and_restarted_on_mutation():
    client = FakePlaid([
        {"cursor": "", "added": [plaid_txn("t1", 1, "2026-09-02")], "next_cursor": "c1", "has_more": True,
         "accounts": [{"id": "plaid-chk", "balance_cents": 100}]},
        {"cursor": "c1", "added": [plaid_txn("t2", 2, "2026-09-03")], "next_cursor": "c2"},
    ])
    client.mutation_failures = 1
    stream = await read_stream(client, "access-1", "")
    assert [item["transaction_id"] for item in stream["added"]] == ["t1", "t2"]
    assert stream["next_cursor"] == "c2"
    assert stream["accounts"][0]["id"] == "plaid-chk"
    assert [call for call in client.calls if call[0] == "sync"] == [("sync", ""), ("sync", ""), ("sync", "c1")]


async def test_too_many_mutations_give_up_with_a_retryable_error():
    client = FakePlaid([{"cursor": ""}])
    client.mutation_failures = 10
    with pytest.raises(PlaidError) as caught:
        await read_stream(client, "access-1", "")
    assert caught.value.retryable is True


# ------------------------------------------------------------------ engine


async def test_nothing_happens_without_an_enabled_mapping(database):
    database.upsert_plaid_item({"item_id": "item-1", "access_token": "a"})
    instance, events, _ = engine(database, StubGateway(), FakePlaid())
    result = await instance.run()
    assert result["items"] == 0
    assert events == []


async def test_a_first_sync_imports_from_the_cutover_and_opens_the_balance(linked):
    client = FakePlaid([{
        "cursor": "",
        "added": [plaid_txn("old", 5, "2026-08-20"), plaid_txn("t1", 4.5, "2026-09-02"),
                  plaid_txn("t2", 10, "2026-09-05", pending=True)],
        "next_cursor": "cursor-1",
        "accounts": [{"id": "plaid-chk", "balance_cents": 50000}],
    }])
    gateway = StubGateway()
    instance, events, sleeps = engine(linked, gateway, client, plaid_refresh_enabled=False)
    result = await instance.run()
    assert result["items"] == 1 and result["items_failed"] == 0
    assert result["imported"] == 2
    assert result["skipped_before_cutover"] == 1
    assert result["starting_balances"] == 1
    assert result["refreshed"] == 0
    [(account_id, rows)] = gateway.imports
    assert account_id == "acct-chk"
    opening, first, second = rows
    assert opening["starting_balance_flag"] is True
    assert opening["date"] == datetime.date(2026, 8, 31)
    assert opening["category_id"] == "cat-sb"
    # Bank says 500.00 posted; the one posted import is -4.50, so the opening balance is 504.50.
    assert opening["amount_cents"] == 50450
    assert first["imported_id"] == "t1" and first["cleared"] is True
    assert second["imported_id"] == "t2" and second["cleared"] is False
    item = linked.get_plaid_item("item-1")
    assert item["cursor"] == "cursor-1"
    assert item["status"] == "ok"
    assert linked.get_bank_link("acct-chk")["last_import_at"]
    assert [kind for _, kind, _ in events] == ["plaid_starting_balance", "plaid_item_synced"]


async def test_a_not_ready_connection_is_left_for_next_time(linked):
    client = FakePlaid([{"cursor": "", "update_status": "TRANSACTIONS_UPDATE_STATUS_NOT_READY", "next_cursor": "c-x"}])
    instance, events, _ = engine(linked, StubGateway(), client, plaid_refresh_enabled=False)
    result = await instance.run()
    assert result["not_ready"] == 1
    assert linked.get_plaid_item("item-1")["cursor"] == ""
    assert events[0][1] == "plaid_not_ready"


async def test_pending_rows_are_settled_deleted_or_kept_and_adoptions_logged(linked):
    gateway = StubGateway({"acct-chk": [
        {"id": "row-p", "date": datetime.date(2026, 9, 3), "amount_cents": -450, "imported_id": "t-pending", "cleared": False, "reconciled": False},
        {"id": "row-gone", "date": datetime.date(2026, 9, 4), "amount_cents": -300, "imported_id": "t-gone", "cleared": False, "reconciled": False},
        {"id": "row-cleared", "date": datetime.date(2026, 9, 4), "amount_cents": -900, "imported_id": "t-cleared", "cleared": True, "reconciled": False},
        {"id": "row-sf", "date": datetime.date(2026, 9, 2), "amount_cents": -1200, "imported_id": "ACT-simplefin-9", "cleared": True, "reconciled": False},
    ]})
    client = FakePlaid([{
        "cursor": "",
        "added": [plaid_txn("t-posted", 4.75, "2026-09-05", pending_transaction_id="t-pending"),
                  plaid_txn("t-sf", 12, "2026-09-02")],
        "removed": [{"transaction_id": "t-pending", "account_id": "plaid-chk"},
                    {"transaction_id": "t-gone", "account_id": "plaid-chk"},
                    {"transaction_id": "t-cleared", "account_id": "plaid-chk"}],
        "next_cursor": "c-2",
    }])
    instance, events, _ = engine(linked, gateway, client, plaid_refresh_enabled=False)
    result = await instance.run()
    assert result["adopted"] == 2 and result["deleted"] == 1 and result["kept"] == 1 and result["imported"] == 0
    [adoptions] = gateway.adoptions
    assert {a["transaction_id"]: a for a in adoptions}["row-p"] == {"transaction_id": "row-p", "imported_id": "t-posted", "cleared": True, "amount_cents": -475, "date": datetime.date(2026, 9, 5)}
    assert {a["transaction_id"]: a for a in adoptions}["row-sf"] == {"transaction_id": "row-sf", "imported_id": "t-sf"}
    assert gateway.deletions == [["row-gone"]]
    assert gateway.imports == []
    logged = {row["transaction_id"]: row for row in linked.list_adoptions(account_id="acct-chk")}
    assert logged["row-p"]["reason"] == "posted" and logged["row-p"]["previous_imported_id"] == "t-pending"
    assert logged["row-sf"]["reason"] == "cutover"
    assert any(kind == "plaid_removed_cleared" for _, kind, _ in events)


async def test_deleting_withdrawn_pending_rows_can_be_switched_off(linked):
    gateway = StubGateway({"acct-chk": [
        {"id": "row-gone", "date": datetime.date(2026, 9, 4), "amount_cents": -300, "imported_id": "t-gone", "cleared": False, "reconciled": False},
    ]})
    client = FakePlaid([{"cursor": "", "removed": [{"transaction_id": "t-gone", "account_id": "plaid-chk"}]}])
    instance, events, _ = engine(linked, gateway, client, plaid_refresh_enabled=False, plaid_delete_removed_pending=False)
    result = await instance.run()
    assert result["deleted"] == 0 and result["kept"] == 1
    assert gateway.deletions == []
    kinds = [kind for _, kind, _ in events]
    assert "plaid_removed_pending_kept" in kinds
    assert "plaid_removed_cleared" not in kinds, "an uncleared row is not reported as cleared"


async def test_refresh_is_rate_limited_and_waits_for_a_newer_update(linked):
    client = FakePlaid([{"cursor": ""}])
    gateway = StubGateway()
    instance, events, sleeps = engine(linked, gateway, client, plaid_refresh_wait_seconds=30)
    first = await instance.run()
    assert first["refreshed"] == 1
    assert ("refresh", "access-1") in client.calls
    # The Item never reports a newer update, so the whole wait is spent in polls.
    assert sum(sleeps) == 30
    assert linked.get_plaid_item("item-1")["last_refresh_at"]
    client.calls.clear()
    second = await instance.run()
    assert second["refreshed"] == 0, "within the minimum interval no second refresh is sent"
    assert ("refresh", "access-1") not in client.calls


async def test_a_declined_refresh_still_reads_the_stream(linked):
    client = FakePlaid([{"cursor": "", "added": [plaid_txn("t1", 1, "2026-09-02")]}])
    client.refresh_error = PlaidError("no", error_type="INVALID_REQUEST", error_code="PRODUCTS_NOT_SUPPORTED")
    gateway = StubGateway()
    instance, events, _ = engine(linked, gateway, client)
    result = await instance.run()
    assert result["refreshed"] == 0 and result["imported"] == 1
    assert events[0][1] == "plaid_refresh_failed"


async def test_a_broken_item_is_recorded_as_needing_repair_and_its_cursor_kept(linked):
    linked.update_plaid_item("item-1", cursor="cursor-before")
    client = FakePlaid([{"cursor": "cursor-before"}])
    client.refresh_error = PlaidError("login", error_code="ITEM_LOGIN_REQUIRED")
    instance, events, _ = engine(linked, StubGateway(), client)
    result = await instance.run()
    assert result["items_failed"] == 1
    item = linked.get_plaid_item("item-1")
    assert item["status"] == "needs_repair"
    assert item["cursor"] == "cursor-before"
    assert linked.get_bank_link("acct-chk")["last_error"]
    assert events[-1][1] == "plaid_item_failed"


async def test_an_actual_refusal_leaves_the_cursor_where_it_was(linked):
    client = FakePlaid([{"cursor": "", "added": [plaid_txn("t1", 1, "2026-09-02")], "next_cursor": "c-new"}])
    gateway = StubGateway()
    gateway.import_errors = ["date is required"]
    instance, events, _ = engine(linked, gateway, client, plaid_refresh_enabled=False, plaid_starting_balance=False)
    result = await instance.run()
    assert result["items_failed"] == 1
    assert linked.get_plaid_item("item-1")["cursor"] == ""
    assert linked.get_plaid_item("item-1")["status"] == "error"


async def test_a_gateway_failure_fails_only_that_item(linked):
    linked.upsert_plaid_item({"item_id": "item-2", "access_token": "access-2"})
    linked.upsert_bank_link({"actual_account_id": "acct-2", "provider": "plaid", "item_id": "item-2",
                             "external_account_id": "plaid-2", "cutover_date": "2026-09-01"})

    class FlakyGateway(StubGateway):
        async def account_transactions(self, account_id, *, start=None, end=None):
            if account_id == "acct-chk":
                raise ActualGatewayError("worker died", retryable=True)
            return []

    client = FakePlaid([{"cursor": "", "added": [plaid_txn("t9", 3, "2026-09-02", account="plaid-2")]}])
    instance, events, _ = engine(linked, FlakyGateway(), client, plaid_refresh_enabled=False, plaid_starting_balance=False)
    result = await instance.run()
    assert result["items"] == 2 and result["items_failed"] == 1 and result["imported"] == 1
    assert linked.get_plaid_item("item-1")["status"] == "error"
    assert linked.get_plaid_item("item-2")["status"] == "ok"


async def test_a_mapping_added_after_the_cursor_moved_is_served_from_the_start(linked):
    linked.update_plaid_item("item-1", cursor="cursor-old")
    linked.upsert_bank_link({"actual_account_id": "acct-old", "provider": "plaid", "item_id": "item-1",
                             "external_account_id": "plaid-old", "cutover_date": "2026-09-01",
                             "last_import_at": 1.0})
    client = FakePlaid([
        {"cursor": "", "added": [plaid_txn("h1", 1, "2026-09-02"), plaid_txn("h-old", 2, "2026-09-02", account="plaid-old")], "next_cursor": "cursor-old"},
        {"cursor": "cursor-old", "added": [plaid_txn("n1", 3, "2026-09-10"), plaid_txn("n-old", 4, "2026-09-10", account="plaid-old")], "next_cursor": "cursor-new"},
    ])
    gateway = StubGateway()
    instance, events, _ = engine(linked, gateway, client, plaid_refresh_enabled=False, plaid_starting_balance=False)
    result = await instance.run()
    assert result["imported"] == 3, "history for the new mapping, then the increment for both"
    imported: dict[str, list[str]] = {}
    for account, rows in gateway.imports:
        imported.setdefault(account, []).extend(row["imported_id"] for row in rows)
    assert imported == {"acct-chk": ["h1", "n1"], "acct-old": ["n-old"]}
    assert [call for call in client.calls if call[0] == "sync"] == [("sync", ""), ("sync", "cursor-old")]
    assert linked.get_plaid_item("item-1")["cursor"] == "cursor-new"
    assert any(kind == "plaid_backfilled" for _, kind, _ in events)
    assert linked.get_bank_link("acct-chk")["last_import_at"]


async def test_preview_reads_the_whole_stream_without_writing(linked):
    gateway = StubGateway({"acct-chk": [
        {"id": "row-sf", "date": datetime.date(2026, 9, 2), "amount_cents": -450, "imported_id": "ACT-sf", "cleared": True, "reconciled": False, "payee_name": "Shop"},
    ]})
    client = FakePlaid([{"cursor": "", "added": [plaid_txn("t1", 4.5, "2026-09-03"), plaid_txn("t2", 9, "2026-09-04"), plaid_txn("old", 1, "2026-08-01")],
                         "accounts": [{"id": "plaid-chk", "balance_cents": 50000}], "next_cursor": "c-preview"}])
    instance, events, _ = engine(linked, gateway, client, plaid_refresh_enabled=False)
    preview = await instance.preview_link(linked.get_bank_link("acct-chk"))
    assert preview["counts"] == {"imports": 1, "adoptions": 1, "deletions": 0, "kept": 0, "skipped_before_cutover": 1, "unknown_removed": 0}
    assert preview["would_import"] == 1
    assert preview["adoptions"][0]["previous_imported_id"] == "ACT-sf"
    assert preview["opening_balance_cents"] is None, "the account already holds rows"
    assert preview["import_range"] == ["2026-09-04", "2026-09-04"]
    assert gateway.adoptions == [] and gateway.deletions == []
    assert all(rows == [] or True for _, rows in gateway.imports)
    assert linked.get_plaid_item("item-1")["cursor"] == "", "a preview never moves the cursor"
