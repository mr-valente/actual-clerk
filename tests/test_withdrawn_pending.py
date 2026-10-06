"""Pending charges the bank withdraws and posts again later: held, then carried."""

from __future__ import annotations

import datetime
import sqlite3

import pytest

from actual_clerk import anticipated
from actual_clerk.db import Database, pending_key
from actual_clerk.domain.plaid_import import plan_account
from actual_clerk.reporting import budget_report

from .factories import account, snapshot, transaction
from .test_plaid_sync import FakePlaid, StubGateway, engine, plaid_txn

TODAY = datetime.date(2026, 10, 5)
RENT = -255000


@pytest.fixture
def linked(database):
    database.upsert_plaid_item({"item_id": "item-1", "access_token": "access-1", "institution_name": "FFFCU"})
    database.upsert_bank_link({"actual_account_id": "acct-chk", "provider": "plaid", "item_id": "item-1",
                               "external_account_id": "plaid-chk", "external_name": "Checking",
                               "cutover_date": "2026-09-01"})
    return database


def held(database, settings, *, category_id="cat-rent", amount=RENT, imported_id="t-pending",
         payee="Demattheisinv", description="PL*DeMattheisInv - WEB PMTS", **row):
    deletion = {
        "transaction_id": "row-pending", "imported_id": imported_id, "amount_cents": amount,
        "date": TODAY, "payee_name": payee, "imported_description": description,
        "category_id": category_id, "is_transfer": False, "is_parent": False, **row,
    }
    count = anticipated.hold_withdrawn(
        database, settings, actual_account_id="acct-chk", label="FFFCU CHECKING", deletions=[deletion]
    )
    return count, database.list_anticipated_charges(origin=anticipated.ORIGIN_PENDING)


def rent_snapshot(*rows):
    return snapshot(accounts=[account("FFFCU Checking", account_id="acct-chk")], transactions=list(rows),
                    budgeted={"cat-rent": 255000})


def posted_rent(**overrides):
    values = {"payee": "Pl*Demattheisinv",
              "description": "PL*DeMattheisInv TYPE: WEB PMTS CO: PL*DeMattheisInv NAME: Nicholas Valente",
              "account_id": "acct-chk", "transaction_id": "row-posted"}
    values.update(overrides)
    return transaction(TODAY, values.pop("amount", RENT), **values)


# ------------------------------------------------------------------ the plan


def test_a_deletion_remembers_what_the_row_was():
    plan = plan_account(
        actual_account_id="acct-chk", external_account_id="plaid-chk", cutover=datetime.date(2026, 9, 1),
        added=[], modified=[], removed=[{"transaction_id": "t-pending", "account_id": "plaid-chk"}],
        existing=[{"id": "row-pending", "date": TODAY, "amount_cents": RENT, "imported_id": "t-pending",
                   "payee_name": "Demattheisinv", "imported_description": "PL*DeMattheisInv",
                   "category_id": "cat-rent", "cleared": False, "reconciled": False}],
    )
    [deletion] = plan.deletions
    assert deletion["payee_name"] == "Demattheisinv"
    assert deletion["category_id"] == "cat-rent"
    assert deletion["is_transfer"] is False and deletion["is_parent"] is False


def test_a_posted_row_naming_a_pending_row_already_gone_is_noted():
    plan = plan_account(
        actual_account_id="acct-chk", external_account_id="plaid-chk", cutover=datetime.date(2026, 9, 1),
        added=[plaid_txn("t-posted", 2550, "2026-10-05", pending_transaction_id="t-pending")],
        modified=[], removed=[], existing=[],
    )
    assert plan.posted_for_pending == {"t-pending": "t-posted"}
    assert len(plan.imports) == 1


# ----------------------------------------------------------------- the engine


async def test_a_withdrawn_pending_charge_is_held_and_later_named_by_its_posted_row(linked):
    gateway = StubGateway({"acct-chk": [
        {"id": "row-pending", "date": datetime.date(2026, 9, 10), "amount_cents": -2500, "imported_id": "t-pending",
         "payee_name": "Shop", "imported_description": "SHOP 123", "category_id": "cat-dining",
         "cleared": False, "reconciled": False},
    ]})
    client = FakePlaid([
        {"cursor": "", "removed": [{"transaction_id": "t-pending", "account_id": "plaid-chk"}], "next_cursor": "c-1"},
        {"cursor": "c-1", "added": [plaid_txn("t-posted", 25, "2026-09-11", pending_transaction_id="t-pending")],
         "next_cursor": "c-2"},
    ])
    instance, events, _ = engine(linked, gateway, client, plaid_refresh_enabled=False)
    await instance.run()
    assert gateway.deletions == [["row-pending"]]
    [hold] = linked.list_anticipated_charges(origin=anticipated.ORIGIN_PENDING)
    assert hold["status"] == "open" and hold["source_id"] is None
    assert hold["category_id"] == "cat-dining" and hold["category_source"] == anticipated.SOURCE_PENDING
    assert hold["notification_key"] == pending_key("t-pending")
    assert hold["noticed_date"] == "2026-09-10" and hold["amount_cents"] == -2500
    assert any(kind == "plaid_pending_held" for _, kind, _ in events)

    gateway.rows["acct-chk"] = []
    await instance.run()
    assert linked.get_anticipated_charge(hold["id"])["posted_imported_id"] == "t-posted"


async def test_nothing_is_held_when_holding_is_switched_off(linked):
    gateway = StubGateway({"acct-chk": [
        {"id": "row-pending", "date": datetime.date(2026, 9, 10), "amount_cents": -2500, "imported_id": "t-pending",
         "cleared": False, "reconciled": False},
    ]})
    client = FakePlaid([{"cursor": "", "removed": [{"transaction_id": "t-pending", "account_id": "plaid-chk"}]}])
    instance, _, _ = engine(linked, gateway, client, plaid_refresh_enabled=False, plaid_hold_withdrawn_days=0)
    await instance.run()
    assert gateway.deletions == [["row-pending"]]
    assert linked.list_anticipated_charges(origin=anticipated.ORIGIN_PENDING) == []


# ------------------------------------------------------------------- holding


def test_only_plain_spending_is_held_and_only_once(database, settings):
    count, rows = held(database, settings)
    assert count == 1 and len(rows) == 1
    again, rows = held(database, settings)
    assert again == 0 and len(rows) == 1, "the same bank id is one hold"
    assert held(database, settings, amount=5000, imported_id="t-refund")[0] == 0
    assert held(database, settings, imported_id="t-transfer", is_transfer=True)[0] == 0
    assert held(database, settings, imported_id="t-split", is_parent=True)[0] == 0


def test_a_hold_counts_against_its_own_category_while_the_bank_has_nothing(database, settings):
    held(database, settings)
    snap = rent_snapshot()
    summary = anticipated.reconcile(database, snap, settings, today=TODAY)
    [open_hold] = summary["open"]
    assert open_hold["origin"] == anticipated.ORIGIN_PENDING
    assert open_hold["category_name"] == "Rent", "the hold learns its category's name from the budget"
    assert open_hold["account_name"] == "FFFCU Checking"
    report = budget_report(snap, settings, today=TODAY, anticipated=summary["open"])
    assert report["anticipated_cents"] == 255000
    assert report["anticipated_committed_cents"] == 255000, "rent draws on its own budget, not free money"
    assert report["uncategorized_cents"] == 0


def test_a_hold_counts_even_with_the_phone_switched_off(database, settings):
    settings.anticipated_enabled = False
    held(database, settings)
    summary = anticipated.reconcile(database, rent_snapshot(), settings, today=TODAY)
    assert len(summary["open"]) == 1


def test_an_uncategorized_hold_is_not_asked_about(database, settings):
    held(database, settings, category_id="")
    summary = anticipated.reconcile(database, rent_snapshot(), settings, today=TODAY)
    assert summary["asking"] == 0
    assert database.open_phone_decisions() == []


# ------------------------------------------------------------------ settling


def test_the_posted_row_inherits_the_category_under_its_new_name(database, settings):
    """2026-10-05: FFFCU withdrew the pending rent and posted it 14 hours later as PL*DeMattheisInv."""
    held(database, settings)
    snap = rent_snapshot(posted_rent())
    summary = anticipated.reconcile(database, snap, settings, today=TODAY)
    assert summary["matched"] == 1 and summary["carried"] == 1
    assert summary["open"] == []
    [hold] = database.list_anticipated_charges(origin=anticipated.ORIGIN_PENDING)
    assert hold["status"] == "matched" and hold["matched_transaction_id"] == "row-posted"
    [carried] = database.approved_decisions()
    assert carried["transaction_id"] == "row-posted"
    assert carried["source"] == anticipated.SOURCE_HELD
    assert carried["category_id"] == "cat-rent"
    assert carried["rationale"]["earlier_key"] == "demattheisinv"
    assert database.list_aliases() == [], "two bank names for one purchase are not an alias"


def test_a_posted_row_named_by_the_bank_settles_whatever_its_amount(database, settings):
    held(database, settings, amount=-4500, category_id="cat-groceries")
    database.note_posted_for_pending("acct-chk", {"t-pending": "t-posted"})
    row = transaction(TODAY, -3810, payee="Gas", account_id="acct-chk", transaction_id="row-gas")
    row["imported_id"] = "t-posted"
    summary = anticipated.reconcile(database, rent_snapshot(row), settings, today=TODAY)
    assert summary["matched"] == 1
    [hold] = database.list_anticipated_charges(origin=anticipated.ORIGIN_PENDING)
    assert hold["matched_transaction_id"] == "row-gas" and hold["match_reason"] == "posted by the bank"


def test_a_posted_row_already_categorized_or_decided_keeps_what_it_has(database, settings):
    held(database, settings)
    snap = rent_snapshot(posted_rent(category_id="cat-electric"))
    summary = anticipated.reconcile(database, snap, settings, today=TODAY)
    assert summary["matched"] == 1 and "carried" not in summary
    assert database.approved_decisions() == []


def test_a_posted_row_waiting_in_review_is_answered_by_the_hold(database, settings):
    held(database, settings)
    database.add_decision({"transaction_id": "row-posted", "status": "needs_review", "source": "unresolved",
                           "merchant_key": "pl demattheisinv"})
    anticipated.reconcile(database, rent_snapshot(posted_rent()), settings, today=TODAY)
    [carried] = database.approved_decisions()
    assert carried["transaction_id"] == "row-posted"
    assert database.open_review_transaction_ids() == set()


def test_an_uncategorized_hold_settles_without_filing_anything(database, settings):
    held(database, settings, category_id="")
    summary = anticipated.reconcile(database, rent_snapshot(posted_rent()), settings, today=TODAY)
    assert summary["matched"] == 1
    assert database.approved_decisions() == []


def test_a_hold_the_bank_never_posts_is_released_days_after_the_withdrawal(database, settings):
    settings.plaid_hold_withdrawn_days = 3
    held(database, settings)
    [hold] = database.list_anticipated_charges(origin=anticipated.ORIGIN_PENDING)
    withdrawn = datetime.datetime.fromtimestamp(hold["noticed_at"], datetime.UTC).date()
    snap = rent_snapshot()
    assert anticipated.reconcile(database, snap, settings, today=withdrawn + datetime.timedelta(days=3))["expired"] == 0
    summary = anticipated.reconcile(database, snap, settings, today=withdrawn + datetime.timedelta(days=4))
    assert summary["expired"] == 1 and summary["open"] == []


def test_a_settled_hold_never_moves_its_row_s_date_or_name(database, settings):
    held(database, settings)
    anticipated.reconcile(database, rent_snapshot(posted_rent()), settings, today=TODAY)
    assert database.matched_charge_dates() == []
    assert database.matched_charge_names() == []
    assert database.matched_transaction_ids(origin=anticipated.ORIGIN_PHONE) == set()
    assert database.matched_transaction_ids() == {"row-posted"}


# ----------------------------------------------------------------- migration


def test_a_phone_only_ledger_is_rebuilt_to_hold_bank_charges_too(tmp_path):
    path = tmp_path / "old.db"
    old = Database(path)
    old.initialize()
    source_row = old.upsert_notification_source(
        {"device_id": "phone-1", "package_name": "com.capitalone", "actual_account_id": "acct-card"}
    )
    old.add_anticipated_charge({"source_id": source_row["id"], "notification_key": "k", "amount_cents": -100,
                                "noticed_at": 1.0, "noticed_date": "2026-08-21"})
    # Put the table back the way the phone-only builds made it.
    raw = sqlite3.connect(path)
    raw.executescript(
        "ALTER TABLE anticipated_charges RENAME TO newer;"
        "DROP INDEX IF EXISTS uq_anticipated_notification; DROP INDEX IF EXISTS ix_anticipated_status;"
        "DROP INDEX IF EXISTS ix_anticipated_noticed; DROP INDEX IF EXISTS uq_anticipated_pending;"
        "CREATE TABLE anticipated_charges (id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES "
        "notification_sources(id) ON DELETE CASCADE, actual_account_id TEXT NOT NULL DEFAULT '', "
        "notification_key TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'charge', amount_cents INTEGER NOT NULL DEFAULT 0, "
        "merchant TEXT NOT NULL DEFAULT '', merchant_key TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', "
        "text TEXT NOT NULL DEFAULT '', noticed_at REAL NOT NULL, noticed_date TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'open', matched_transaction_id TEXT NOT NULL DEFAULT '', "
        "matched_payee TEXT NOT NULL DEFAULT '', matched_date TEXT NOT NULL DEFAULT '', "
        "match_reason TEXT NOT NULL DEFAULT '', resolved_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL);"
        "INSERT INTO anticipated_charges(id,source_id,actual_account_id,notification_key,kind,amount_cents,merchant,"
        "merchant_key,title,text,noticed_at,noticed_date,status,matched_transaction_id,matched_payee,matched_date,"
        "match_reason,resolved_at,created_at,updated_at) SELECT id,source_id,actual_account_id,notification_key,kind,"
        "amount_cents,merchant,merchant_key,title,text,noticed_at,noticed_date,status,matched_transaction_id,"
        "matched_payee,matched_date,match_reason,resolved_at,created_at,updated_at FROM newer;"
        "DROP TABLE newer;"
        "CREATE UNIQUE INDEX uq_anticipated_notification ON anticipated_charges(source_id, notification_key);"
    )
    raw.close()

    database = Database(path)
    database.initialize()
    [phone] = database.list_anticipated_charges()
    assert phone["origin"] == anticipated.ORIGIN_PHONE and phone["source_id"] == source_row["id"]
    assert phone["category_id"] == "" and phone["posted_imported_id"] == ""
    with database.connect() as connection:
        columns = {row["name"]: row for row in connection.execute("PRAGMA table_info(anticipated_charges)")}
        indexes = {row["name"] for row in connection.execute("PRAGMA index_list(anticipated_charges)")}
        tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert columns["source_id"]["notnull"] == 0
    assert {"uq_anticipated_notification", "uq_anticipated_pending", "ix_anticipated_status"} <= indexes
    assert "anticipated_charges_pre_origin" not in tables
    # The same notification is still one charge, and the source still owns it.
    _, created = database.add_anticipated_charge({"source_id": source_row["id"], "notification_key": "k",
                                                  "noticed_at": 2.0, "noticed_date": "2026-08-21"})
    assert created is False
    database.delete_notification_source(source_row["id"])
    assert database.list_anticipated_charges() == []
    # And a second start leaves it alone.
    Database(path).initialize()


# ------------------------------------------------------- review follow-ups


def test_a_hold_never_settles_against_its_own_pending_row(database, settings):
    """A refresh that read Actual just before the sync deleted the pending row."""
    held(database, settings)
    stale = transaction(TODAY, RENT, payee="Demattheisinv", account_id="acct-chk", transaction_id="row-pending")
    stale["imported_id"] = "t-pending"
    summary = anticipated.reconcile(database, rent_snapshot(stale), settings, today=TODAY)
    assert summary["matched"] == 0 and len(summary["open"]) == 1
    # The posted row still settles it once it arrives.
    summary = anticipated.reconcile(database, rent_snapshot(posted_rent()), settings, today=TODAY)
    assert summary["matched"] == 1


def test_reopening_a_hold_the_bank_settled_keeps_it_open(database, settings):
    held(database, settings, amount=-4500, category_id="cat-groceries")
    database.note_posted_for_pending("acct-chk", {"t-pending": "t-posted"})
    row = transaction(TODAY, -3810, payee="Gas", account_id="acct-chk", transaction_id="row-gas")
    row["imported_id"] = "t-posted"
    snap = rent_snapshot(row)
    anticipated.reconcile(database, snap, settings, today=TODAY)
    [hold] = database.list_anticipated_charges(origin=anticipated.ORIGIN_PENDING)
    assert database.reopen_anticipated_charge(hold["id"])
    summary = anticipated.reconcile(database, snap, settings, today=TODAY)
    assert summary["matched"] == 0 and len(summary["open"]) == 1


def test_a_reopened_hold_counts_its_days_from_the_reopen(database, settings, monkeypatch):
    settings.plaid_hold_withdrawn_days = 3
    held(database, settings)
    [hold] = database.list_anticipated_charges(origin=anticipated.ORIGIN_PENDING)
    withdrawn = datetime.datetime.fromtimestamp(hold["noticed_at"], datetime.UTC).date()
    later = withdrawn + datetime.timedelta(days=10)
    snap = rent_snapshot()
    assert anticipated.reconcile(database, snap, settings, today=later)["expired"] == 1
    reopened_at = datetime.datetime.combine(later, datetime.time(12), datetime.UTC).timestamp()
    monkeypatch.setattr("actual_clerk.db.time.time", lambda: reopened_at)
    assert database.reopen_anticipated_charge(hold["id"])
    summary = anticipated.reconcile(database, snap, settings, today=later)
    assert summary["expired"] == 0 and len(summary["open"]) == 1
