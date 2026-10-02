from __future__ import annotations

import datetime

from actual_clerk.reporting import (
    budget_report,
    freshness,
    monthly_reports,
    to_actual_accounts,
    to_simplefin_accounts,
    yesterday_allowance,
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


def test_only_aged_bank_imports_count_as_uncleared_for_the_health_check():
    """Manual entries and split children say nothing about what the bank did."""

    def row(**overrides):
        item = transaction(TODAY, -100, account_id="acct-checking")
        item.update(overrides)
        return item

    snap = snapshot(
        accounts=[account("Checking")],
        transactions=[
            row(id="bank-uncleared", cleared=False, imported_id="imp-1"),
            row(id="bank-cleared", cleared=True, imported_id="imp-2"),
            row(id="manual-uncleared", cleared=False, imported_id=""),
            row(id="split-child", cleared=False, imported_id="imp-3", is_child=True),
        ],
    )
    [converted] = to_actual_accounts(snap)
    assert [item.transaction_id for item in converted.uncleared_imports] == ["bank-uncleared"]


def test_missing_simplefin_data_converts_to_nothing():
    assert to_simplefin_accounts(None) == []
    assert to_simplefin_accounts({"accounts": []}) == []
    [converted] = to_simplefin_accounts(
        {"accounts": [{"id": "sf1", "name": "Checking", "balance_cents": 100, "org_name": "Bank"}]}
    )
    assert converted.org_name == "Bank"


def test_clerk_managed_links_overlay_the_snapshot_and_keep_what_actual_said():
    from actual_clerk.reporting import apply_bank_links, to_actual_accounts

    snapshot = {
        "accounts": [
            {"id": "acct-1", "name": "Card", "sync_source": "", "external_id": "",
             "bank_name": "", "balance_cents": 0, "last_sync": None, "off_budget": False,
             "closed": False},
            {"id": "acct-2", "name": "Checking", "sync_source": "simpleFin",
             "external_id": "sf-2", "bank_name": "Bank", "balance_cents": 0,
             "last_sync": None, "off_budget": False, "closed": False},
        ],
        "transactions": [],
    }
    apply_bank_links(
        snapshot,
        [
            {"actual_account_id": "acct-1", "provider": "plaid", "item_id": "item-1",
             "external_account_id": "plaid-1", "institution": "Platypus", "enabled": True},
            {"actual_account_id": "acct-2", "provider": "plaid",
             "external_account_id": "plaid-2", "enabled": False},
        ],
    )
    card, checking = snapshot["accounts"]
    assert card["sync_source"] == "plaid"
    assert card["connection_id"] == "item-1"
    assert card["external_id"] == "plaid-1"
    assert card["bank_name"] == "Platypus"
    assert card["managed_by_clerk"] is True
    assert card["actual_sync_source"] == ""
    # A disabled link changes nothing.
    assert checking["sync_source"] == "simpleFin"
    assert checking["managed_by_clerk"] is False
    assert checking["actual_sync_source"] == "simpleFin"
    infos = {item.id: item for item in to_actual_accounts(snapshot)}
    assert infos["acct-1"].managed_by_clerk is True
    assert infos["acct-1"].sync_source == "plaid"
    assert infos["acct-1"].connection_id == "item-1"
    assert infos["acct-2"].managed_by_clerk is False


def test_remote_accounts_carry_their_provider():
    from actual_clerk.reporting import to_remote_accounts

    [reading] = to_remote_accounts({"accounts": [{"id": "p", "name": "Card"}]}, provider="plaid")
    assert reading.provider == "plaid"
    [reading] = to_remote_accounts({"accounts": [{"id": "p", "name": "Card", "provider": "x"}]})
    assert reading.provider == "x"
    assert to_simplefin_accounts({"accounts": [{"id": "s", "name": "C"}]})[0].provider == "simpleFin"


# ------------------------------------------------------------------ reports


def _reports_snapshot():
    return snapshot(
        transactions=[
            # July: before budgeting began in Actual, so not a reported month.
            transaction(datetime.date(2026, 7, 1), 400000, payee="Employer", category_id="cat-paycheck"),
            transaction(datetime.date(2026, 7, 5), -50000, payee="Shop", category_id="cat-groceries"),
            # August: 4,000 in, 1,800 of bills, 300 spent -> 1,900 left.
            transaction(datetime.date(2026, 8, 1), 400000, payee="Employer", category_id="cat-paycheck"),
            transaction(datetime.date(2026, 8, 2), -180000, payee="Landlord", category_id="cat-rent"),
            transaction(datetime.date(2026, 8, 31), -30000, payee="Shop", category_id="cat-groceries"),
            # September: 2,500 spent against 2,200 free -> 300 over.
            transaction(datetime.date(2026, 9, 1), 400000, payee="Employer", category_id="cat-paycheck"),
            transaction(datetime.date(2026, 9, 2), -180000, payee="Landlord", category_id="cat-rent"),
            transaction(datetime.date(2026, 9, 20), -250000, payee="Shop", category_id="cat-groceries"),
            # October so far.
            transaction(datetime.date(2026, 10, 1), 400000, payee="Employer", category_id="cat-paycheck"),
            transaction(datetime.date(2026, 10, 1), -1000, payee="Cafe", category_id="cat-dining"),
        ],
        budgeted_history={
            "2026-07": {},
            "2026-08": {"cat-rent": 180000},
            "2026-09": {"cat-rent": 180000},
            "2026-10": {"cat-rent": 180000},
        },
    )


def test_each_budgeted_month_is_reported_as_it_stood_on_its_last_day(settings):
    snap = _reports_snapshot()
    result = monthly_reports(snap, settings, today=datetime.date(2026, 10, 1))
    months = {item["month"]: item for item in result["months"]}
    assert list(months) == ["2026-08", "2026-09", "2026-10"]
    august, september, october = months["2026-08"], months["2026-09"], months["2026-10"]
    assert (august["available_cents"], august["spent_cents"], august["remaining_cents"]) == (220000, 30000, 190000)
    assert august["status"] == "final"
    assert august["day_of_month"] == 31
    assert september["remaining_cents"] == -30000
    assert september["status"] == "settling"
    assert september["settles_on"] == "2026-10-07"
    assert october["status"] == "in_progress"
    assert october["spent_cents"] == 1000
    # Only months that have ended add up.
    assert result["total_remaining_cents"] == 160000
    assert (result["months_under"], result["months_over"]) == (1, 1)


def test_a_month_settles_after_a_week_unless_a_phone_charge_is_still_waiting(settings):
    snap = _reports_snapshot()
    snap["accounts"].append(account("Card", account_id="acct-card"))
    later = datetime.date(2026, 10, 9)
    assert {m["month"]: m["status"] for m in monthly_reports(snap, settings, today=later)["months"]}["2026-09"] == "final"
    waiting = [{"id": "c1", "status": "open", "actual_account_id": "acct-card", "amount_cents": -6338,
                "noticed_date": "2026-09-30", "merchant": "eBay"}]
    months = {m["month"]: m for m in monthly_reports(snap, settings, today=later, anticipated=waiting)["months"]}
    assert months["2026-09"]["status"] == "settling"
    assert months["2026-09"]["anticipated_cents"] == 6338
    assert months["2026-09"]["remaining_cents"] == -30000 - 6338
    assert months["2026-10"]["anticipated_cents"] == 0


def test_a_month_only_partly_inside_the_history_is_left_out(settings):
    snap = _reports_snapshot()
    snap["history_start"] = datetime.date(2026, 8, 15)
    months = [m["month"] for m in monthly_reports(snap, settings, today=datetime.date(2026, 10, 1))["months"]]
    assert months == ["2026-09", "2026-10"]


def _august(*spending):
    return snapshot(
        transactions=[
            transaction(datetime.date(2026, 8, 1), 400000, payee="Employer", category_id="cat-paycheck"),
            transaction(datetime.date(2026, 8, 2), -180000, payee="Landlord", category_id="cat-rent"),
            transaction(datetime.date(2026, 8, 9), -4500, payee="Cafe", category_id="cat-dining"),
            *spending,
        ],
        budgeted={"cat-rent": 180000},
        budgeted_history={"2026-08": {"cat-rent": 180000}},
    )


def test_yesterday_is_measured_against_the_allowance_it_started_with(settings):
    snap = _august(
        transaction(datetime.date(2026, 8, 20), -1500, payee="Cafe", category_id="cat-dining"),
        transaction(datetime.date(2026, 8, 20), -12000, payee="Market", category_id="cat-groceries"),
        # Today's spending belongs to today's grade, tomorrow.
        transaction(datetime.date(2026, 8, 21), -9999, payee="Shop", category_id="cat-dining"),
    )
    result = budget_report(snap, settings, today=TODAY)["yesterday"]
    # 2,155.00 left at the start of the 20th, over the 12 days from it to the 31st.
    assert result["date"] == "2026-08-20"
    assert result["allowance_cents"] == 215500 // 12
    assert result["spent_cents"] == 13500
    assert result["grade"] == "green"


def test_a_phone_charge_from_yesterday_counts_against_it(settings):
    snap = _august(
        transaction(datetime.date(2026, 8, 20), -13500, payee="Market", category_id="cat-groceries"),
    )
    snap["accounts"].append(account("Card", account_id="acct-card"))
    waiting = [{"id": "c1", "status": "open", "actual_account_id": "acct-card",
                "amount_cents": -3000, "noticed_date": "2026-08-20", "merchant": "eBay"}]
    result = yesterday_allowance(snap, settings, today=TODAY, anticipated=waiting)
    assert result["spent_cents"] == 16500
    assert result["grade"] == "yellow"
    # 92% of the allowance is green for someone content to keep 5% of it.
    settings.digest_good_day_percent = 5
    result = yesterday_allowance(snap, settings, today=TODAY, anticipated=waiting)
    assert result["grade"] == "green"


def test_any_spending_is_red_once_free_money_is_gone(settings):
    snap = _august(
        transaction(datetime.date(2026, 8, 10), -230000, payee="Airline", category_id="cat-dining"),
        transaction(datetime.date(2026, 8, 20), -500, payee="Cafe", category_id="cat-coffee"),
    )
    result = yesterday_allowance(snap, settings, today=TODAY)
    assert result["allowance_cents"] == 0
    assert result["spent_cents"] == 500
    assert result["grade"] == "red"


def test_on_the_first_yesterday_closes_the_month_before(settings):
    snap = _august(
        transaction(datetime.date(2026, 8, 31), -20000, payee="Market", category_id="cat-groceries"),
    )
    snap["budgeted"] = {}
    result = yesterday_allowance(snap, settings, today=datetime.date(2026, 9, 1))
    # 2,155.00 left with only the 31st to go.
    assert result["allowance_cents"] == 215500
    assert result["spent_cents"] == 20000
    assert result["grade"] == "green"
    snap["budgeted_history"] = {}
    assert yesterday_allowance(snap, settings, today=datetime.date(2026, 9, 1)) is None
