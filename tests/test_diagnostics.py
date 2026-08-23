"""The diagnostic exists to explain a wrong number, so its findings are the
part worth testing: each one stands for a specific way the Overview can drift
away from Actual, and each must fire on that cause and stay quiet otherwise.
"""

from __future__ import annotations

import datetime

import pytest

from actual_clerk.diagnostics import Redactor, build_report, money
from tests.conftest import TODAY
from tests.factories import account, category, snapshot, transaction

MONTH = TODAY.strftime("%Y-%m")


def report(settings, **kwargs) -> str:
    base: dict = {
        "settings": settings,
        "today": TODAY,
        "snapshot": snapshot(),
        "stored_overview": None,
        "probe": None,
        "gateway_status": {"connected": True, "last_error": ""},
        "jobs": [],
        "last_sync": None,
        "health": [],
        "unmonitored": set(),
        "counts": {},
    }
    base.update(kwargs)
    return build_report(**base)


def findings(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip().startswith(">> [")]


# ------------------------------------------------------------------ basics


def test_the_report_renders_without_a_live_read(settings):
    text = report(settings, snapshot=None, snapshot_error="Actual refused the request")
    assert "Actual refused the request" in text
    assert "ACTUAL CLERK DIAGNOSTIC" in text
    # Sections that need a live read must not be half-rendered.
    assert "8. BUDGET REPORT" not in text


def test_a_healthy_budget_produces_no_findings(settings):
    snap = snapshot(
        transactions=[
            transaction(TODAY, 400000, payee="Work", category_id="cat-paycheck"),
            transaction(TODAY, -120000, payee="Landlord", category_id="cat-rent"),
        ],
        budgeted={"cat-paycheck": 400000, "cat-rent": 120000},
        budgeted_history={MONTH: {"cat-paycheck": 400000, "cat-rent": 120000}},
    )
    text = report(
        settings,
        snapshot=snap,
        stored_overview={
            "snapshot_updated_at": datetime.datetime.now(datetime.UTC).timestamp(),
            "budget": {"month": MONTH},
        },
        last_sync={"completed_at": TODAY.isoformat()},
    )
    assert findings(text) == []


# --------------------------------------------------------------- findings


def test_a_committed_group_matching_no_actual_group_is_reported(settings):
    settings.committed_groups = ["Recurring Bills"]
    text = report(settings, snapshot=snapshot(budgeted={"cat-rent": 120000}))
    assert any("match no group in Actual" in item for item in findings(text))


def test_a_committed_group_that_does_match_is_not_reported(settings):
    settings.committed_groups = ["Bills"]
    text = report(settings, snapshot=snapshot(budgeted={"cat-rent": 120000}))
    assert not any("match no group in Actual" in item for item in findings(text))


def test_group_matching_ignores_case(settings):
    settings.committed_groups = ["bILLs"]
    text = report(settings, snapshot=snapshot(budgeted={"cat-rent": 120000}))
    assert not any("match no group in Actual" in item for item in findings(text))


def test_an_empty_budget_month_is_reported(settings):
    text = report(settings, snapshot=snapshot(budgeted={}))
    assert any("holds no budgeted amounts" in item for item in findings(text))


def test_budget_kept_in_the_table_clerk_does_not_read_is_reported(settings):
    """The failure that makes every budgeted amount silently zero."""
    probe = {
        "budget_type_preference": None,
        "reads_table": "zero_budgets",
        "is_tracking": False,
        "tables": {
            "zero_budgets": {"rows": 0, "non_zero": 0, "total_cents": 0},
            "reflect_budgets": {"rows": 40, "non_zero": 12, "total_cents": 500000},
        },
        "budget_name": "Household",
        "budget_id": "abc",
    }
    text = report(settings, probe=probe, snapshot=snapshot(budgeted={"cat-rent": 1}))
    assert any("budget of zero" in item for item in findings(text))


def test_a_tracking_budget_reading_its_own_table_is_not_reported(settings):
    probe = {
        "budget_type_preference": "tracking",
        "reads_table": "reflect_budgets",
        "is_tracking": True,
        "tables": {
            "zero_budgets": {"rows": 0, "non_zero": 0, "total_cents": 0},
            "reflect_budgets": {"rows": 40, "non_zero": 12, "total_cents": 500000},
        },
        "budget_name": "Household",
        "budget_id": "abc",
    }
    text = report(settings, probe=probe, snapshot=snapshot(budgeted={"cat-rent": 1}))
    assert not any("budget of zero" in item for item in findings(text))
    assert "TRACKING" in text


def test_a_stale_stored_overview_is_reported(settings):
    stale = datetime.datetime.now(datetime.UTC).timestamp() - 30 * 3600
    text = report(settings, stored_overview={"snapshot_updated_at": stale, "budget": {}})
    assert any("hours ago" in item for item in findings(text))


def test_a_stored_overview_for_last_month_is_reported(settings):
    text = report(
        settings,
        stored_overview={
            "snapshot_updated_at": datetime.datetime.now(datetime.UTC).timestamp(),
            "budget": {"month": "2026-01"},
        },
    )
    assert any("not been rebuilt this month" in item for item in findings(text))


def test_drift_between_the_stored_and_live_numbers_is_reported(settings):
    snap = snapshot(
        transactions=[transaction(TODAY, 400000, payee="Work", category_id="cat-paycheck")],
        budgeted={"cat-paycheck": 400000},
    )
    stored = {
        "snapshot_updated_at": datetime.datetime.now(datetime.UTC).timestamp(),
        "budget": {"month": MONTH, "free_cents": 999999, "committed_cents": 12345},
    }
    text = report(settings, snapshot=snap, stored_overview=stored)
    assert any("differ between the stored Overview and a live read" in item for item in findings(text))
    assert "free_cents" in text


def test_inferred_income_is_reported_but_budgeted_income_is_not(settings):
    inferred = snapshot(
        transactions=[transaction(TODAY, 400000, payee="Work", category_id="cat-paycheck")],
        budgeted={"cat-rent": 120000},
    )
    assert any("inferred" in item for item in findings(report(settings, snapshot=inferred)))

    declared = snapshot(
        transactions=[transaction(TODAY, 400000, payee="Work", category_id="cat-paycheck")],
        budgeted={"cat-paycheck": 400000, "cat-rent": 120000},
    )
    assert not any("inferred" in item for item in findings(report(settings, snapshot=declared)))


def test_a_history_window_starting_mid_month_is_reported(settings):
    snap = snapshot(budgeted={"cat-rent": 1})
    snap["history_start"] = TODAY
    assert any("history window starts after" in item.lower() for item in findings(report(settings, snapshot=snap)))


def test_uncategorized_spending_is_reported(settings):
    snap = snapshot(
        transactions=[transaction(TODAY, -5000, payee="Mystery")],
        budgeted={"cat-rent": 1},
    )
    assert any("uncategorized" in item for item in findings(report(settings, snapshot=snap)))


def test_committed_categories_with_no_budget_are_reported(settings):
    """What checking a group box does that the budgeted>0 rule never could."""
    settings.committed_groups = ["Everyday"]
    snap = snapshot(
        transactions=[transaction(TODAY, -5000, payee="Cafe", category_id="cat-coffee")],
        budgeted={"cat-rent": 120000},
    )
    text = report(settings, snapshot=snap)
    assert any("no budget this month" in item for item in findings(text))
    assert "zero-budget committed:" in text


# ------------------------------------------------------------ explanations


def test_the_report_explains_the_gap_against_actuals_projected_savings(settings):
    snap = snapshot(
        transactions=[transaction(TODAY, 400000, payee="Work", category_id="cat-paycheck")],
        budgeted={"cat-paycheck": 400000, "cat-rent": 120000, "cat-groceries": 40000},
    )
    text = report(settings, snapshot=snap)
    assert "Actual's Projected Savings" in text
    assert "Clerk's free money" in text


def test_both_quiet_account_thresholds_are_shown(settings):
    settings.transaction_stale_days = 20
    snap = snapshot(
        accounts=[account("Checking")],
        transactions=[transaction(TODAY - datetime.timedelta(days=40), -100, payee="Old")],
        budgeted={"cat-rent": 1},
    )
    text = report(settings, snapshot=snap)
    assert "> 20 days" in text
    assert "40d" in text


def test_a_muted_account_is_labelled(settings):
    snap = snapshot(accounts=[account("Checking")], budgeted={"cat-rent": 1})
    text = report(settings, snapshot=snap, unmonitored={"acct-checking"})
    assert "MUTED" in text


# --------------------------------------------------------------- redaction


def test_redaction_replaces_names_consistently_and_keeps_amounts(settings):
    snap = snapshot(
        categories=[
            category("Paycheck", "Income", is_income=True),
            category("Renters Insurance", "Annual Bills"),
        ],
        accounts=[account("Ally Savings")],
        transactions=[transaction(TODAY, -18000, payee="Insurer", category_id="cat-renters-insurance")],
        budgeted={"cat-renters-insurance": 18000},
    )
    text = report(settings, snapshot=snap, redact=True)
    for secret in ("Renters Insurance", "Annual Bills", "Ally Savings"):
        assert secret not in text
    assert "Group 1" in text
    assert "180.00" in text, "amounts are the point of the report and must survive redaction"


def test_redaction_off_keeps_real_names(settings):
    snap = snapshot(accounts=[account("Ally Savings")], budgeted={"cat-rent": 1})
    assert "Ally Savings" in report(settings, snapshot=snap, redact=False)


def test_the_redactor_is_stable_and_scoped_per_kind():
    hide = Redactor(True)
    assert hide("Group", "Bills") == "Group 1"
    assert hide("Group", "bills") == "Group 1", "same name, same label"
    assert hide("Group", "Everyday") == "Group 2"
    assert hide("Account", "Bills") == "Account 1", "kinds number independently"


def test_the_redactor_is_a_no_op_when_disabled():
    hide = Redactor(False)
    assert hide("Group", "Bills") == "Bills"


@pytest.mark.parametrize(
    ("cents", "expected"),
    [(0, "0.00"), (-1250, "-12.50"), (123456789, "1,234,567.89")],
)
def test_money_formatting(cents, expected):
    assert money(cents).strip() == expected


def test_money_handles_a_missing_value():
    assert money(None).strip() == "--"


def test_budget_stranded_outside_the_checked_groups_is_reported(settings):
    """Checking some boxes but not all silently stops that budget counting."""
    settings.committed_groups = ["Bills"]
    snap = snapshot(
        categories=[
            category("Paycheck", "Income", is_income=True),
            category("Rent", "Bills"),
            category("Renters Insurance", "Annual"),
        ],
        budgeted={"cat-rent": 120000, "cat-renters-insurance": 6000},
    )
    text = report(settings, snapshot=snap)
    assert any("did not check as committed" in item for item in findings(text))
    assert any("60.00" in item for item in findings(text))
    assert "BUDGETED, NOT COMMITTED" in text


def test_no_stranded_budget_when_every_budgeted_group_is_checked(settings):
    settings.committed_groups = ["Bills", "Annual"]
    snap = snapshot(
        categories=[
            category("Paycheck", "Income", is_income=True),
            category("Rent", "Bills"),
            category("Renters Insurance", "Annual"),
        ],
        budgeted={"cat-rent": 120000, "cat-renters-insurance": 6000},
    )
    text = report(settings, snapshot=snap)
    assert not any("did not check as committed" in item for item in findings(text))


def test_stranded_budget_is_not_reported_when_no_group_is_checked(settings):
    """With no boxes ticked the budgeted>0 rule already commits everything."""
    settings.committed_groups = []
    snap = snapshot(budgeted={"cat-rent": 120000, "cat-groceries": 40000})
    text = report(settings, snapshot=snap)
    assert not any("did not check as committed" in item for item in findings(text))


# ------------------------------------------ categories Actual has deleted


def test_spending_on_a_deleted_category_is_reported_by_name(settings):
    snap = snapshot(
        transactions=[
            transaction(TODAY, -250456, payee="Old Thing", category_id="cat-gone-9f2"),
        ],
        budgeted={"cat-rent": 120000},
    )
    probe = {
        "budget_type_preference": "tracking",
        "reads_table": "reflect_budgets",
        "is_tracking": True,
        "tables": {
            "zero_budgets": {"rows": 0, "non_zero": 0, "total_cents": 0},
            "reflect_budgets": {"rows": 9, "non_zero": 4, "total_cents": 400000},
        },
        "budget_name": "Household",
        "budget_id": "b",
        "deleted_categories": {"cat-gone-9f2": "Old Group / Amazon"},
    }
    text = report(settings, snapshot=snap, probe=probe)
    assert any("Actual cannot resolve" in item for item in findings(text))
    assert any("2,504.56" in item for item in findings(text))
    assert "Old Group / Amazon" in text
    assert "cat-gone-9f2" in text


def test_a_deleted_category_id_is_still_reported_without_a_probe(settings):
    """The id alone is enough to find the transactions in Actual."""
    snap = snapshot(
        transactions=[transaction(TODAY, -250456, payee="Old", category_id="cat-gone-9f2")],
        budgeted={"cat-rent": 120000},
    )
    text = report(settings, snapshot=snap, probe=None)
    assert any("Actual cannot resolve" in item for item in findings(text))
    assert "cat-gone-9f2" in text


def test_no_orphan_finding_when_every_category_resolves(settings):
    snap = snapshot(
        transactions=[transaction(TODAY, -5000, payee="Cafe", category_id="cat-coffee")],
        budgeted={"cat-rent": 120000},
    )
    assert not any("cannot resolve" in item for item in findings(report(settings, snapshot=snap)))


def test_deleted_category_names_are_redacted_but_ids_are_not(settings):
    """The id is what you paste into Actual to find the transactions."""
    snap = snapshot(
        transactions=[transaction(TODAY, -250456, payee="Old", category_id="cat-gone-9f2")],
        budgeted={"cat-rent": 120000},
    )
    probe = {
        "budget_type_preference": "tracking", "reads_table": "reflect_budgets",
        "is_tracking": True, "tables": {}, "budget_name": "b", "budget_id": "b",
        "deleted_categories": {"cat-gone-9f2": "Old Group / Amazon"},
    }
    text = report(settings, snapshot=snap, probe=probe, redact=True)
    assert "Amazon" not in text
    assert "cat-gone-9f2" in text


def test_the_spending_buckets_partition_the_month(settings):
    """Every dollar lands in exactly one bucket, so the parts sum to the whole."""
    snap = snapshot(
        transactions=[
            transaction(TODAY, -10580, payee="Landlord", category_id="cat-rent"),
            transaction(TODAY, -31171, payee="Diner", category_id="cat-dining"),
            transaction(TODAY, -2934, payee="Mystery"),
            transaction(TODAY, -250456, payee="Old", category_id="cat-gone"),
            transaction(TODAY, -9999, payee="Moved", is_transfer=True),
        ],
        budgeted={"cat-rent": 120000},
    )
    text = report(settings, snapshot=snap)
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if "SPENDING ADD UP" in line)
    values = {}
    for line in lines[start + 2 :]:
        # The rows stop at the first finding, blank line or rule.
        if not line.startswith("  ") or line.startswith("  >>"):
            break
        label, sep, amount = line.partition(":")
        if not sep:
            break
        values[label.strip()] = float(amount.replace("USD", "").replace(",", ""))
    assert values["committed"] == 105.80
    assert values["discretionary"] == 311.71
    assert values["orphaned"] == 2504.56
    assert values["uncategorized"] == 29.34
    assert values["total"] == pytest.approx(105.80 + 311.71 + 2504.56 + 29.34)


# --------------------------------------------- budget nothing lays claim to


def test_budget_addressed_to_an_unknown_category_is_reported(settings):
    """The rent case: section 4's month total exceeds section 9's breakdown."""
    snap = snapshot(budgeted={"cat-rent": 12000, "f008bda6-dead": 255000})
    text = report(settings, snapshot=snap)
    assert any("no live category claims" in item for item in findings(text))
    assert any("2,550.00" in item for item in findings(text))
    assert "f008bda6-dead" in text


def test_a_redirect_target_is_named_when_one_exists(settings):
    snap = snapshot(budgeted={"f008bda6-dead": 255000})
    probe = {
        "budget_type_preference": "tracking", "reads_table": "reflect_budgets",
        "is_tracking": True, "tables": {}, "budget_name": "b", "budget_id": "b",
        "redirect_map": {"f008bda6-dead": "cat-rent"},
    }
    text = report(settings, snapshot=snap, probe=probe)
    assert "redirects to cat-rent" in text


def test_no_unclaimed_budget_finding_when_every_row_resolves(settings):
    snap = snapshot(budgeted={"cat-rent": 255000})
    assert not any(
        "no live category claims" in item for item in findings(report(settings, snapshot=snap))
    )


def test_a_zero_budget_row_is_not_reported_as_unclaimed(settings):
    snap = snapshot(budgeted={"cat-rent": 12000, "some-stale-id": 0})
    assert not any(
        "no live category claims" in item for item in findings(report(settings, snapshot=snap))
    )


# ----------------------------------------------------- raw budget row dump


def _probe_with_rows(rows):
    return {
        "budget_type_preference": "tracking", "reads_table": "reflect_budgets",
        "is_tracking": True, "tables": {}, "budget_name": "b", "budget_id": "b",
        "budget_rows": rows,
    }


def test_a_budget_row_with_no_month_is_reported_as_dropped(settings):
    probe = _probe_with_rows([
        {"month": 202608, "category_id": "cat-rent", "amount_cents": 120000, "carryover": 0},
        {"month": None, "category_id": "cat-rent", "amount_cents": 2000, "carryover": 0},
    ])
    text = report(settings, snapshot=snapshot(budgeted={"cat-rent": 120000}), probe=probe)
    assert "NO MONTH" in text
    assert any("no month or no" in item for item in findings(text))
    assert any("20.00" in item for item in findings(text))


def test_a_budget_row_with_no_category_is_reported_as_dropped(settings):
    probe = _probe_with_rows([
        {"month": 202608, "category_id": "", "amount_cents": 2500, "carryover": 0},
    ])
    text = report(settings, snapshot=snapshot(budgeted={"cat-rent": 1}), probe=probe)
    assert "NO CATEGORY" in text
    assert any("25.00" in item for item in findings(text))


def test_every_row_is_listed_with_its_kind(settings):
    probe = _probe_with_rows([
        {"month": 202608, "category_id": "cat-paycheck", "amount_cents": 520266, "carryover": 0},
        {"month": 202608, "category_id": "cat-rent", "amount_cents": 255000, "carryover": 0},
    ])
    text = report(settings, snapshot=snapshot(budgeted={"cat-rent": 255000}), probe=probe)
    assert "income " in text and "expense" in text
    assert "Income / Paycheck" in text
    assert "Bills / Rent" in text


def test_a_redirected_budget_row_is_labelled(settings):
    probe = _probe_with_rows([
        {"month": 202608, "category_id": "f008bda6-dead", "amount_cents": 255000, "carryover": 0},
    ])
    probe["redirect_map"] = {"f008bda6-dead": "cat-rent"}
    text = report(settings, snapshot=snapshot(budgeted={"cat-rent": 255000}), probe=probe)
    assert "[redirected]" in text
    assert "Bills / Rent" in text


def test_no_dropped_finding_when_every_row_is_well_formed(settings):
    probe = _probe_with_rows([
        {"month": 202608, "category_id": "cat-rent", "amount_cents": 120000, "carryover": 0},
    ])
    text = report(settings, snapshot=snapshot(budgeted={"cat-rent": 120000}), probe=probe)
    assert not any("dropped" in item for item in findings(text))


def test_an_unresolvable_budget_row_shows_its_raw_category_id(settings):
    """The id is the only handle on a row nothing else can name."""
    probe = _probe_with_rows([
        {"month": 202608, "category_id": "9c1f-orphan", "amount_cents": 2000, "carryover": 0},
    ])
    text = report(settings, snapshot=snapshot(budgeted={"cat-rent": 1}), probe=probe)
    assert "no category claims this row: 9c1f-orphan" in text


def test_a_null_category_is_labelled_as_null(settings):
    probe = _probe_with_rows([
        {"month": None, "category_id": "", "amount_cents": 2000, "carryover": 0},
    ])
    text = report(settings, snapshot=snapshot(budgeted={"cat-rent": 1}), probe=probe)
    assert "(null)" in text


def test_the_report_says_how_to_settle_a_disagreement_with_actual(settings):
    probe = _probe_with_rows([
        {"month": 202608, "category_id": "cat-rent", "amount_cents": 12000, "carryover": 0},
    ])
    text = report(settings, snapshot=snapshot(budgeted={"cat-rent": 120000}), probe=probe)
    assert "Reset budget cache" in text


# ------------------------------------- the budget screen, laid out to diff


def test_every_category_is_listed_even_with_no_budget_row(settings):
    """A category Actual shows a figure for and Clerk does not is the bug."""
    snap = snapshot(
        categories=[
            category("Paycheck", "Income", is_income=True),
            category("Rent", "Bills"),
            category("Streaming", "Bills"),
        ],
        budgeted={"cat-rent": 255000},
    )
    text = report(settings, snapshot=snap)
    section = text.split("5B. EVERY CATEGORY")[1]
    assert "Rent" in section
    assert "Streaming" in section
    assert "no budget row" in section, "a category with no row must still appear"


def test_group_subtotals_and_an_expense_total_are_printed(settings):
    snap = snapshot(
        categories=[
            category("Paycheck", "Income", is_income=True),
            category("Rent", "Bills"),
            category("Petrol", "Travel"),
        ],
        budgeted={"cat-paycheck": 520266, "cat-rent": 255000, "cat-petrol": 2500},
    )
    text = report(settings, snapshot=snap)
    section = text.split("5B. EVERY CATEGORY")[1]
    assert "2,550.00" in section and "25.00" in section
    assert "TOTAL across expense groups" in text
    total = next(
        line for line in text.splitlines() if "TOTAL across expense groups" in line
    )
    assert "2,575.00" in total, "income must be excluded from the expense total"


def test_a_hidden_category_is_marked_in_the_listing(settings):
    snap = snapshot(
        categories=[
            category("Paycheck", "Income", is_income=True),
            category("Old Thing", "Bills", hidden=True),
        ],
        budgeted={"cat-old-thing": 2500},
    )
    text = report(settings, snapshot=snap)
    assert "(hidden)" in text
