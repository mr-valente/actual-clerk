from __future__ import annotations

import datetime

from actual_clerk.reporting import (
    budget_report,
    freshness,
    to_actual_accounts,
    to_simplefin_accounts,
)

from .factories import account, snapshot, transaction

TODAY = datetime.date(2026, 8, 21)


def test_the_budget_report_is_built_from_a_snapshot(settings):
    snap = snapshot(
        transactions=[
            transaction(datetime.date(2026, 8, 1), 400000, payee="Employer", category_id="cat-paycheck"),
            transaction(datetime.date(2026, 8, 2), -180000, payee="Landlord", category_id="cat-rent"),
            transaction(datetime.date(2026, 8, 9), -4500, payee="Cafe", category_id="cat-dining"),
        ],
        budgeted={"cat-rent": 180000},
    )
    report = budget_report(snap, settings, today=TODAY)
    assert report["expected_income_cents"] == 400000
    assert report["committed_cents"] == 180000
    assert report["free_cents"] == 220000
    assert report["discretionary_spent_cents"] == 4500


def test_the_report_honours_configured_committed_groups(settings):
    settings.committed_groups = ["Bills"]
    snap = snapshot(
        transactions=[
            transaction(datetime.date(2026, 8, 1), 400000, payee="Employer", category_id="cat-paycheck"),
        ],
        budgeted={"cat-rent": 180000, "cat-dining": 5000},
    )
    report = budget_report(snap, settings, today=TODAY)
    # Dining carries a budget but is not in a committed group, so it is not a bill.
    assert report["committed_cents"] == 180000


def test_an_income_override_reaches_the_report(settings):
    settings.monthly_income_override = 5000.0
    report = budget_report(snapshot(transactions=[]), settings, today=TODAY)
    assert report["expected_income_cents"] == 500000
    assert report["income_basis"] == "override"


def test_freshness_flags_a_synced_account_that_stopped_delivering(settings):
    settings.transaction_stale_days = 4
    snap = snapshot(
        accounts=[account("Checking"), account("Cash", sync_source="", external_id="")],
        transactions=[
            transaction(TODAY - datetime.timedelta(days=20), -100, payee="Old", account_id="acct-checking"),
            transaction(TODAY - datetime.timedelta(days=90), -100, payee="Older", account_id="acct-cash", account_name="Cash"),
        ],
    )
    result = freshness(snap, settings, today=TODAY)
    assert result["stale_accounts"] == 1
    assert result["up_to_date"] is False
    assert result["days_since_newest"] == 20
    # A manual account cannot be stale: nothing was supposed to arrive.
    cash = next(item for item in result["accounts"] if item["account_name"] == "Cash")
    assert cash["stale"] is False


def test_a_current_account_reports_as_up_to_date(settings):
    snap = snapshot(
        accounts=[account("Checking")],
        transactions=[transaction(TODAY, -100, payee="Today", account_id="acct-checking")],
    )
    result = freshness(snap, settings, today=TODAY)
    assert result["up_to_date"] is True
    assert result["days_since_newest"] == 0


def test_a_synced_account_with_no_history_is_stale(settings):
    snap = snapshot(accounts=[account("Checking")], transactions=[])
    result = freshness(snap, settings, today=TODAY)
    assert result["stale_accounts"] == 1
    assert result["newest_transaction_date"] is None


def test_closed_accounts_are_left_out_of_freshness(settings):
    snap = snapshot(accounts=[account("Old", closed=True)], transactions=[])
    assert freshness(snap, settings, today=TODAY)["accounts"] == []


def test_snapshot_accounts_convert_for_the_health_check():
    checking = account("Checking", balance_cents=12345)
    checking["unconfirmed_transfers"] = [
        {"id": "transfer-1", "date": TODAY, "amount_cents": 2500}
    ]
    snap = snapshot(accounts=[checking])
    [converted] = to_actual_accounts(snap)
    assert converted.name == "Checking"
    assert converted.balance_cents == 12345
    assert converted.external_id == "sf-acct-checking"
    assert converted.unconfirmed_transfers[0].amount_cents == 2500
    assert converted.unconfirmed_transfers[0].transaction_id == "transfer-1"


def test_missing_simplefin_data_converts_to_nothing():
    assert to_simplefin_accounts(None) == []
    assert to_simplefin_accounts({"accounts": []}) == []
    [converted] = to_simplefin_accounts(
        {"accounts": [{"id": "sf1", "name": "Checking", "balance_cents": 100, "org_name": "Bank"}]}
    )
    assert converted.org_name == "Bank"
