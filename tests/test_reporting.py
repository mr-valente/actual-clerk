from __future__ import annotations

import datetime

from actual_clerk.reporting import (
    budget_report,
    freshness,
    recurring_report,
    spending_trend,
    to_actual_accounts,
    to_simplefin_accounts,
    upcoming_charges,
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


def test_recurring_detection_ignores_transfers_and_off_budget_accounts(settings):
    charges = [
        transaction(
            TODAY - datetime.timedelta(days=30 * index + 6),
            -1599,
            payee="Netflix",
            category_id="cat-subscriptions",
        )
        for index in range(4)
    ]
    noise = [
        transaction(
            TODAY - datetime.timedelta(days=30 * index + 6),
            -2000,
            payee="Brokerage",
            off_budget=True,
        )
        for index in range(4)
    ] + [
        transaction(
            TODAY - datetime.timedelta(days=30 * index + 6),
            -3000,
            payee="Savings Move",
            is_transfer=True,
        )
        for index in range(4)
    ]
    series, summary = recurring_report(snapshot(transactions=charges + noise), today=TODAY)
    assert [item["label"] for item in series] == ["Netflix"]
    assert summary["count"] == 1


def test_upcoming_charges_are_bounded_and_ordered():
    series = [
        {"label": "Late", "next_expected": "2026-08-30"},
        {"label": "Soon", "next_expected": "2026-08-23"},
        {"label": "Far", "next_expected": "2026-09-30"},
        {"label": "Past", "next_expected": "2026-08-10"},
        {"label": "Broken"},
    ]
    upcoming = upcoming_charges(series, today=TODAY, within_days=10)
    assert [item["label"] for item in upcoming] == ["Soon", "Late"]


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


def test_the_trend_sums_on_budget_outflow_per_month():
    snap = snapshot(
        transactions=[
            transaction(datetime.date(2026, 7, 3), -5000, payee="A"),
            transaction(datetime.date(2026, 7, 9), -2500, payee="B"),
            transaction(datetime.date(2026, 8, 4), -1000, payee="C"),
            transaction(datetime.date(2026, 8, 5), 40000, payee="Pay"),
            transaction(datetime.date(2026, 8, 6), -9999, payee="Moved", is_transfer=True),
            transaction(datetime.date(2026, 8, 7), -8888, payee="Brokerage", off_budget=True),
        ]
    )
    assert spending_trend(snap, today=TODAY) == [
        {"month": "2026-07", "spent_cents": 7500},
        {"month": "2026-08", "spent_cents": 1000},
    ]


def test_the_trend_is_bounded_to_the_requested_months():
    transactions = [
        transaction(datetime.date(2025, month, 5), -1000, payee="X") for month in range(1, 13)
    ]
    assert len(spending_trend(snapshot(transactions=transactions), today=TODAY, months=6)) == 6


def test_snapshot_accounts_convert_for_the_health_check():
    snap = snapshot(accounts=[account("Checking", balance_cents=12345)])
    [converted] = to_actual_accounts(snap)
    assert converted.name == "Checking"
    assert converted.balance_cents == 12345
    assert converted.external_id == "sf-acct-checking"


def test_missing_simplefin_data_converts_to_nothing():
    assert to_simplefin_accounts(None) == []
    assert to_simplefin_accounts({"accounts": []}) == []
    [converted] = to_simplefin_accounts(
        {"accounts": [{"id": "sf1", "name": "Checking", "balance_cents": 100, "org_name": "Bank"}]}
    )
    assert converted.org_name == "Bank"
