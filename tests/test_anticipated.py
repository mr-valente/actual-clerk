"""Anticipated charges: reading card notifications and settling them against Actual."""

from __future__ import annotations

import datetime

import pytest

from actual_clerk import anticipated
from actual_clerk.domain.anticipated import (
    KIND_CHARGE,
    KIND_CREDIT,
    KIND_DECLINED,
    KIND_UNKNOWN,
    AnticipatedCharge,
    CandidateTransaction,
    expired_charges,
    match_charges,
    parse_notification,
)
from actual_clerk.domain.budget import (
    AnticipatedInfo,
    CategoryInfo,
    TransactionInfo,
    build_budget_report,
)
from actual_clerk.reporting import budget_report

from .factories import account, snapshot, transaction

TODAY = datetime.date(2026, 8, 21)


# -------------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    ("title", "text", "kind", "cents", "merchant"),
    [
        (
            "Capital One",
            "A charge of $12.34 at STARBUCKS STORE 08871 was approved on your Venture card ending in 1234.",
            KIND_CHARGE, -1234, "STARBUCKS STORE 08871",
        ),
        ("Venture", "You made a $45.67 purchase at AMAZON.COM with your card ending in 1234.", KIND_CHARGE, -4567, "AMAZON.COM"),
        ("Capital One", "$1,234.56 was charged at BEST BUY #123 on your card.", KIND_CHARGE, -123456, "BEST BUY #123"),
        ("Capital One", "New transaction: $8 at SQ *BLUE BOTTLE on 8/21 at 3:45 PM", KIND_CHARGE, -800, "SQ *BLUE BOTTLE"),
        ("Capital One", "A $20.00 credit from AMAZON.COM was posted to your account.", KIND_CREDIT, 2000, "AMAZON.COM"),
        ("Capital One", "Your payment of $500.00 was received. Thank you!", KIND_CREDIT, 50000, ""),
        ("Capital One", "A $99.99 charge at ACME was declined.", KIND_DECLINED, -9999, "ACME"),
        ("Capital One", "Your statement is ready to view.", KIND_UNKNOWN, 0, ""),
        # The real thing, as seen on a Pixel on 2026-09-11.
        ("Venture Credit Card…4273", "Your purchase for $3.19 at Valve was approved.", KIND_CHARGE, -319, "Valve"),
    ],
)
def test_notifications_are_read_for_amount_direction_and_merchant(title, text, kind, cents, merchant):
    parsed = parse_notification(title, text)
    assert parsed.kind == kind
    assert parsed.amount_cents == cents
    assert parsed.merchant == merchant


def test_only_a_recognised_charge_counts():
    assert parse_notification("", "A charge of $5.00 at SHOP was approved.").counts is True
    assert parse_notification("", "A $5.00 charge at SHOP was declined.").counts is False
    assert parse_notification("", "A $5.00 credit from SHOP posted.").counts is False
    assert parse_notification("", "Hello there").counts is False


def test_the_merchant_key_follows_clerk_s_own_normalization():
    parsed = parse_notification("", "A charge of $4.50 at SQ *BLUE BOTTLE 4471 was approved.")
    assert parsed.merchant_key == "blue bottle"


def test_a_time_after_at_is_not_mistaken_for_a_merchant():
    parsed = parse_notification("", "$8.00 was spent at 3:45 PM at BLUE BOTTLE COFFEE.")
    assert parsed.merchant == "BLUE BOTTLE COFFEE"


# ------------------------------------------------------------------- matching


def charge(charge_id="c1", *, cents=-1234, noticed=TODAY, key="", account_id="acct-card"):
    return AnticipatedCharge(charge_id, account_id, cents, noticed, key)


def candidate(txn_id, *, cents=-1234, date=TODAY, key="", account_id="acct-card", imported=True, payee=""):
    return CandidateTransaction(txn_id, account_id, cents, date, key, payee or txn_id, imported)


def test_a_charge_settles_against_the_same_amount_on_the_same_account():
    matches = match_charges([charge()], [candidate("t1", date=TODAY + datetime.timedelta(days=2))], window_days=10)
    assert [(m.charge_id, m.transaction_id, m.reason) for m in matches] == [("c1", "t1", "amount and date")]


def test_a_different_account_or_amount_never_matches():
    assert not match_charges([charge()], [candidate("t1", account_id="acct-other")], window_days=10)
    assert not match_charges([charge()], [candidate("t1", cents=-1233)], window_days=10)


def test_the_window_bounds_both_directions():
    early = candidate("early", date=TODAY - datetime.timedelta(days=2))
    late = candidate("late", date=TODAY + datetime.timedelta(days=11))
    assert not match_charges([charge()], [early, late], window_days=10)
    just = candidate("just", date=TODAY - datetime.timedelta(days=1))
    assert match_charges([charge()], [just], window_days=10)[0].transaction_id == "just"


def test_the_same_merchant_wins_over_a_nearer_date():
    near = candidate("near", date=TODAY, key="walmart")
    right = candidate("right", date=TODAY + datetime.timedelta(days=3), key="blue bottle")
    matches = match_charges([charge(key="blue bottle")], [near, right], window_days=10)
    assert matches[0].transaction_id == "right"
    assert matches[0].reason == "amount and merchant"


def test_two_identical_coffees_settle_one_each():
    charges = [charge("c1", cents=-450), charge("c2", cents=-450, noticed=TODAY + datetime.timedelta(days=1))]
    rows = [candidate("t1", cents=-450), candidate("t2", cents=-450, date=TODAY + datetime.timedelta(days=1))]
    matches = match_charges(charges, rows, window_days=10)
    assert {(m.charge_id, m.transaction_id) for m in matches} == {("c1", "t1"), ("c2", "t2")}


def test_a_transaction_already_claimed_is_not_used_twice():
    matches = match_charges([charge()], [candidate("t1")], window_days=10, used_transaction_ids={"t1"})
    assert matches == []


def test_old_unposted_charges_expire():
    old = charge("old", noticed=TODAY - datetime.timedelta(days=15))
    fresh = charge("fresh", noticed=TODAY - datetime.timedelta(days=14))
    assert expired_charges([old, fresh], today=TODAY, expire_days=14) == ["old"]


# --------------------------------------------------------------------- budget

CATEGORIES = [
    CategoryInfo("inc", "Paycheck", "Income", is_income=True),
    CategoryInfo("rent", "Rent", "Bills"),
    CategoryInfo("food", "Groceries", "Everyday"),
]


def test_anticipated_charges_count_as_spent_without_touching_free_money():
    result = build_budget_report(
        today=TODAY,
        categories=CATEGORIES,
        budgeted={"rent": 180000},
        transactions=[
            TransactionInfo("1", datetime.date(2026, 8, 1), 400000, "inc"),
            TransactionInfo("2", datetime.date(2026, 8, 7), -9000, "food"),
        ],
        anticipated=[
            AnticipatedInfo("a", -2500),
            AnticipatedInfo("b", -1000, off_budget=True),  # never competes
            AnticipatedInfo("c", 2000),  # a credit anticipates nothing
        ],
    )
    assert result.free_cents == 220000
    assert result.anticipated_cents == 2500
    assert result.anticipated_count == 1
    assert result.anticipated_committed_cents == 0
    # Without a category the charge is discretionary, in the uncategorized lump,
    # but it is not an uncategorized *transaction*: there is nothing to file.
    assert result.discretionary_spent_cents == 11500
    assert result.uncategorized_cents == 2500
    assert result.uncategorized_count == 0
    assert result.spent_cents == 11500
    assert result.remaining_cents == 220000 - 11500


def test_a_categorized_anticipation_is_charged_to_its_own_category():
    result = build_budget_report(
        today=TODAY,
        categories=CATEGORIES,
        budgeted={"rent": 180000},
        transactions=[TransactionInfo("1", datetime.date(2026, 8, 1), 400000, "inc")],
        anticipated=[
            AnticipatedInfo("bill", -50000, category_id="rent"),   # committed: draws on its budget
            AnticipatedInfo("food", -2500, category_id="food"),    # discretionary, named
            AnticipatedInfo("gone", -100, category_id="deleted"),  # unknown category: uncategorized
            AnticipatedInfo("pay", -100, category_id="inc"),       # income category: uncategorized
        ],
    )
    assert result.anticipated_cents == 52700
    assert result.anticipated_committed_cents == 50000
    assert result.committed_spent_cents == 50000
    assert result.committed_overspend_cents == 0
    assert result.discretionary_spent_cents == 2700
    assert result.uncategorized_cents == 200
    assert result.spent_cents == 2700
    assert [line["category_name"] for line in result.top_categories] == ["Groceries"]
    assert result.committed_lines[0]["spent_cents"] == 50000


def test_an_anticipated_bill_beyond_its_budget_overspends_like_a_posted_one():
    result = build_budget_report(
        today=TODAY,
        categories=CATEGORIES,
        budgeted={"rent": 180000},
        transactions=[TransactionInfo("1", datetime.date(2026, 8, 1), 400000, "inc")],
        anticipated=[AnticipatedInfo("bill", -190000, category_id="rent")],
    )
    assert result.committed_overspend_cents == 10000
    assert result.spent_cents == 10000


def test_the_reporting_layer_reads_account_standing_off_the_snapshot(settings):
    snap = snapshot(
        accounts=[account("Card", account_id="acct-card"), account("Savings", account_id="acct-off", off_budget=True)],
        transactions=[transaction(datetime.date(2026, 8, 1), 400000, payee="Employer", category_id="cat-paycheck")],
        budgeted={"cat-rent": 180000},
    )
    report = budget_report(
        snap,
        settings,
        today=TODAY,
        anticipated=[
            {"id": "a", "amount_cents": -2500, "actual_account_id": "acct-card", "status": "open"},
            {"id": "b", "amount_cents": -2500, "actual_account_id": "acct-off", "status": "open"},
            {"id": "c", "amount_cents": -2500, "actual_account_id": "acct-gone", "status": "open"},
            {"id": "d", "amount_cents": -2500, "actual_account_id": "acct-card", "status": "matched"},
        ],
    )
    assert report["anticipated_cents"] == 2500
    assert report["anticipated_count"] == 1


# ------------------------------------------------------------------ database


def source(database, **overrides):
    payload = {
        "device_id": "phone-1",
        "device_name": "Pixel",
        "package_name": "com.konylabs.capitalone",
        "app_label": "Capital One",
        "actual_account_id": "acct-card",
        "account_name": "Venture",
    }
    payload.update(overrides)
    return database.upsert_notification_source(payload)


def test_a_source_is_one_app_on_one_phone(database):
    first = source(database)
    again = source(database, device_name="Pixel 9")
    assert again["id"] == first["id"]
    assert again["device_name"] == "Pixel 9"
    other = source(database, device_id="phone-2")
    assert other["id"] != first["id"]
    assert [s["id"] for s in database.list_notification_sources(device_id="phone-1")] == [first["id"]]


def test_the_same_notification_twice_is_one_charge(database, settings):
    src = source(database)
    first, created = anticipated.record_notification(
        database, settings, source=src, posted_at_ms=1_700_000_000_000,
        title="Capital One", text="A charge of $12.34 at STARBUCKS was approved.",
    )
    second, created_again = anticipated.record_notification(
        database, settings, source=src, posted_at_ms=1_700_000_000_000,
        title="Capital One", text="A charge of $12.34 at STARBUCKS was approved.",
    )
    assert created is True and created_again is False
    assert second["id"] == first["id"]
    assert first["status"] == "open"
    assert first["amount_cents"] == -1234
    assert first["merchant"] == "STARBUCKS"
    assert first["noticed_date"] == "2023-11-14"
    assert database.counts()["anticipated_open"] == 1


def test_unreadable_and_declined_notifications_are_kept_but_never_open(database, settings):
    src = source(database)
    declined, _ = anticipated.record_notification(
        database, settings, source=src, posted_at_ms=1, title="", text="A $9.99 charge at ACME was declined.",
    )
    unknown, _ = anticipated.record_notification(
        database, settings, source=src, posted_at_ms=2, title="Capital One", text="Your statement is ready.",
    )
    assert declined["status"] == "ignored" and declined["kind"] == "declined"
    assert unknown["status"] == "ignored" and unknown["kind"] == "unknown"
    assert database.list_anticipated_charges(status="open") == []


def test_moving_a_source_moves_its_open_charges_only(database, settings):
    src = source(database)
    open_row, _ = anticipated.record_notification(
        database, settings, source=src, posted_at_ms=1, title="", text="A charge of $1.00 at A was approved.",
    )
    settled, _ = anticipated.record_notification(
        database, settings, source=src, posted_at_ms=2, title="", text="A charge of $2.00 at B was approved.",
    )
    database.resolve_anticipated_charge(settled["id"], "matched", matched_transaction_id="t")
    database.update_notification_source(src["id"], actual_account_id="acct-new", account_name="New")
    assert database.get_anticipated_charge(open_row["id"])["actual_account_id"] == "acct-new"
    assert database.get_anticipated_charge(settled["id"])["actual_account_id"] == "acct-card"


def test_deleting_a_source_takes_its_charges_with_it(database, settings):
    src = source(database)
    anticipated.record_notification(
        database, settings, source=src, posted_at_ms=1, title="", text="A charge of $1.00 at A was approved.",
    )
    assert database.delete_notification_source(src["id"]) is True
    assert database.list_anticipated_charges() == []


def test_resolving_is_once_and_reopening_restores(database, settings):
    src = source(database)
    row, _ = anticipated.record_notification(
        database, settings, source=src, posted_at_ms=1, title="", text="A charge of $1.00 at A was approved.",
    )
    assert database.resolve_anticipated_charge(row["id"], "matched", matched_transaction_id="t1") is True
    assert database.resolve_anticipated_charge(row["id"], "dismissed") is False
    assert database.matched_transaction_ids() == {"t1"}
    assert database.reopen_anticipated_charge(row["id"]) is True
    assert database.get_anticipated_charge(row["id"])["status"] == "open"
    assert database.matched_transaction_ids() == set()


# ----------------------------------------------------------------- reconcile


def test_reconcile_settles_open_charges_against_the_snapshot(database, settings):
    src = source(database)
    row, _ = anticipated.record_notification(
        database, settings, source=src, posted_at_ms=int(datetime.datetime(2026, 8, 20, 15, tzinfo=datetime.UTC).timestamp() * 1000),
        title="Capital One", text="A charge of $12.34 at SQ *BLUE BOTTLE was approved.",
    )
    stale, _ = anticipated.record_notification(
        database, settings, source=src, posted_at_ms=int(datetime.datetime(2026, 7, 1, tzinfo=datetime.UTC).timestamp() * 1000),
        title="Capital One", text="A charge of $99.00 at NOWHERE was approved.",
    )
    snap = snapshot(
        accounts=[account("Card", account_id="acct-card")],
        transactions=[
            transaction(datetime.date(2026, 8, 21), -1234, payee="Blue Bottle Coffee", account_id="acct-card", transaction_id="t-bb"),
            transaction(datetime.date(2026, 8, 21), -1234, payee="Walmart", account_id="acct-card", transaction_id="t-wm"),
        ],
    )
    summary = anticipated.reconcile(database, snap, settings, today=TODAY)
    assert summary["matched"] == 1
    assert summary["expired"] == 1
    assert summary["open"] == []
    settled = database.get_anticipated_charge(row["id"])
    assert settled["status"] == "matched"
    assert settled["matched_transaction_id"] == "t-bb"
    assert settled["match_reason"] == "amount and merchant"
    assert database.get_anticipated_charge(stale["id"])["status"] == "expired"


def test_reconcile_leaves_an_unmatched_charge_open_and_counting(database, settings):
    src = source(database)
    anticipated.record_notification(
        database, settings, source=src, posted_at_ms=int(datetime.datetime(2026, 8, 21, 15, tzinfo=datetime.UTC).timestamp() * 1000),
        title="", text="A charge of $12.34 at SHOP was approved.",
    )
    snap = snapshot(accounts=[account("Card", account_id="acct-card")], transactions=[])
    summary = anticipated.reconcile(database, snap, settings, today=TODAY)
    assert summary["matched"] == 0
    assert len(summary["open"]) == 1
    assert summary["open"][0]["counts"] is True
    report = budget_report(snap, settings, today=TODAY, anticipated=summary["open"])
    assert report["anticipated_cents"] == 1234


def test_reconcile_does_nothing_when_the_feature_is_off(database, settings):
    settings.anticipated_enabled = False
    src = source(database)
    anticipated.record_notification(
        database, settings, source=src, posted_at_ms=1, title="", text="A charge of $1.00 at A was approved.",
    )
    summary = anticipated.reconcile(database, snapshot(transactions=[]), settings, today=TODAY)
    assert summary == {"open": [], "matched": 0, "expired": 0, "categorized": 0}


# ------------------------------------------------------- provisional category


def steam_history(**overrides):
    """A budget where Steam has been filed under Dining (any category) three times."""
    rows = [
        transaction(datetime.date(2026, 6, 1 + i), -1999, payee="Steam", category_id="cat-dining", account_id="acct-card")
        for i in range(3)
    ]
    return snapshot(accounts=[account("Card", account_id="acct-card")], transactions=rows, **overrides)


def test_a_known_merchant_is_categorized_on_the_spot(database, settings):
    src = source(database)
    row, _ = anticipated.record_notification(
        database, settings, source=src, posted_at_ms=int(datetime.datetime(2026, 8, 21, tzinfo=datetime.UTC).timestamp() * 1000),
        title="", text="Your purchase for $19.99 at Steam was approved.",
    )
    summary = anticipated.reconcile(database, steam_history(), settings, today=TODAY)
    assert summary["categorized"] == 1
    current = database.get_anticipated_charge(row["id"])
    assert current["category_id"] == "cat-dining"
    assert current["category_source"] == "memory"
    assert current["category_confidence"] > 0.75
    report = budget_report(steam_history(budgeted={"cat-dining": 5000}), settings, today=TODAY, anticipated=summary["open"])
    # Dining carries a budget, so the anticipated charge draws on it rather than on free money.
    assert report["anticipated_committed_cents"] == 1999
    assert report["committed_spent_cents"] == 1999
    assert report["spent_cents"] == 0


def test_an_unknown_merchant_stays_uncategorized_and_discretionary(database, settings):
    src = source(database)
    anticipated.record_notification(
        database, settings, source=src, posted_at_ms=int(datetime.datetime(2026, 8, 21, tzinfo=datetime.UTC).timestamp() * 1000), title="", text="Your purchase for $3.19 at Valve was approved.",
    )
    summary = anticipated.reconcile(database, steam_history(), settings, today=TODAY)
    assert summary["categorized"] == 0
    assert summary["open"][0]["category_id"] == ""
    report = budget_report(steam_history(), settings, today=TODAY, anticipated=summary["open"])
    assert report["uncategorized_cents"] == 319


def test_an_alias_lets_the_bank_s_history_categorize_the_phone_s_name(database, settings):
    database.upsert_alias("valve", "steam", source="taught")
    src = source(database)
    row, _ = anticipated.record_notification(
        database, settings, source=src, posted_at_ms=int(datetime.datetime(2026, 8, 21, tzinfo=datetime.UTC).timestamp() * 1000), title="", text="Your purchase for $3.19 at Valve was approved.",
    )
    anticipated.reconcile(database, steam_history(), settings, today=TODAY)
    current = database.get_anticipated_charge(row["id"])
    assert current["category_id"] == "cat-dining"
    assert current["category_source"] == "memory"


def test_settling_learns_the_alias_and_the_next_notification_matches_by_merchant(database, settings):
    src = source(database)
    first, _ = anticipated.record_notification(
        database, settings, source=src, posted_at_ms=int(datetime.datetime(2026, 8, 20, tzinfo=datetime.UTC).timestamp() * 1000),
        title="", text="Your purchase for $3.19 at Valve was approved.",
    )
    snap = snapshot(
        accounts=[account("Card", account_id="acct-card")],
        transactions=[
            transaction(datetime.date(2026, 8, 21), -319, payee="Steam", account_id="acct-card", transaction_id="t-steam"),
            transaction(datetime.date(2026, 8, 20), -319, payee="Walmart", account_id="acct-card", transaction_id="t-wm"),
        ],
    )
    anticipated.reconcile(database, snap, settings, today=TODAY)
    # Nothing related the names the first time, so the nearer date (Walmart) won.
    assert database.get_anticipated_charge(first["id"])["matched_transaction_id"] == "t-wm"
    assert database.alias_map() == {"valve": "walmart"}

    database.delete_alias("valve")
    database.upsert_alias("valve", "steam", source="taught")
    second, _ = anticipated.record_notification(
        database, settings, source=src, posted_at_ms=int(datetime.datetime(2026, 8, 20, tzinfo=datetime.UTC).timestamp() * 1000),
        title="", text="Your purchase for $3.19 at Valve was approved!",
    )
    snap2 = snapshot(
        accounts=[account("Card", account_id="acct-card")],
        transactions=[
            transaction(datetime.date(2026, 8, 21), -319, payee="Steam", account_id="acct-card", transaction_id="t-steam-2"),
            transaction(datetime.date(2026, 8, 20), -319, payee="Walmart", account_id="acct-card", transaction_id="t-wm-2"),
        ],
    )
    anticipated.reconcile(database, snap2, settings, today=TODAY)
    settled = database.get_anticipated_charge(second["id"])
    assert settled["matched_transaction_id"] == "t-steam-2"
    assert settled["match_reason"] == "amount and merchant"
    # A taught alias is not overwritten by what settled.
    assert database.alias_map() == {"valve": "steam"}


def test_teaching_a_category_writes_memory_for_the_merchant_and_its_alias(database, settings):
    src = source(database)
    row, _ = anticipated.record_notification(
        database, settings, source=src, posted_at_ms=int(datetime.datetime(2026, 8, 21, tzinfo=datetime.UTC).timestamp() * 1000), title="", text="Your purchase for $3.19 at Valve was approved.",
    )
    anticipated.teach_category(database, row, category_id="cat-games", category_name="Games")
    assert database.get_anticipated_charge(row["id"])["category_source"] == "taught"
    assert [m["category_id"] for m in database.memory_for("valve")] == ["cat-games"]
    assert database.memory_for("valve")[0]["corrections"] == 1
    # Memory does not overwrite what was taught.
    anticipated.reconcile(database, steam_history(), settings, today=TODAY)
    assert database.get_anticipated_charge(row["id"])["category_id"] == "cat-games"
    # Teaching the alias afterwards carries the category to the bank's key too.
    anticipated.teach_alias(database, database.get_anticipated_charge(row["id"]), payee="Steam")
    assert [m["category_id"] for m in database.memory_for("steam")] == ["cat-games"]


def test_a_taught_category_reaches_the_bank_s_key_when_the_charge_settles(database, settings):
    src = source(database)
    row, _ = anticipated.record_notification(
        database, settings, source=src, posted_at_ms=int(datetime.datetime(2026, 8, 20, tzinfo=datetime.UTC).timestamp() * 1000),
        title="", text="Your purchase for $3.19 at Valve was approved.",
    )
    anticipated.teach_category(database, row, category_id="cat-games", category_name="Games")
    snap = snapshot(
        accounts=[account("Card", account_id="acct-card")],
        transactions=[transaction(datetime.date(2026, 8, 21), -319, payee="Steam", account_id="acct-card", transaction_id="t-steam")],
    )
    anticipated.reconcile(database, snap, settings, today=TODAY)
    assert database.alias_map() == {"valve": "steam"}
    assert [m["category_id"] for m in database.memory_for("steam")] == ["cat-games"]


def test_older_ledgers_gain_the_category_columns(tmp_path):
    import sqlite3

    from actual_clerk.db import Database
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        "CREATE TABLE anticipated_charges (id TEXT PRIMARY KEY, source_id TEXT NOT NULL, "
        "actual_account_id TEXT NOT NULL DEFAULT '', notification_key TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'charge', "
        "amount_cents INTEGER NOT NULL DEFAULT 0, merchant TEXT NOT NULL DEFAULT '', merchant_key TEXT NOT NULL DEFAULT '', "
        "title TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '', noticed_at REAL NOT NULL, noticed_date TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'open', matched_transaction_id TEXT NOT NULL DEFAULT '', matched_payee TEXT NOT NULL DEFAULT '', "
        "matched_date TEXT NOT NULL DEFAULT '', match_reason TEXT NOT NULL DEFAULT '', resolved_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL);"
        "INSERT INTO anticipated_charges(id,source_id,notification_key,noticed_at,noticed_date,created_at,updated_at) VALUES('c','s','k',1,'2026-08-21',1,1);"
    )
    raw.close()
    database = Database(path)
    database.initialize()
    row = database.get_anticipated_charge("c")
    assert row["category_id"] == "" and row["category_source"] == "" and row["category_confidence"] == 0
