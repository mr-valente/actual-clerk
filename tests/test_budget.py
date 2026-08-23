from __future__ import annotations

import datetime

from actual_clerk.domain.budget import (
    CategoryInfo,
    TransactionInfo,
    average_monthly_income,
    build_budget_report,
    format_money,
    month_bounds,
)

TODAY = datetime.date(2026, 8, 21)

CATEGORIES = [
    CategoryInfo("inc", "Paycheck", "Income", is_income=True),
    CategoryInfo("rent", "Rent", "Bills"),
    CategoryInfo("power", "Electric", "Bills"),
    CategoryInfo("food", "Groceries", "Everyday"),
    CategoryInfo("out", "Dining", "Everyday"),
]
BUDGETED = {"rent": 180000, "power": 12000}


def report(transactions, **kwargs):
    return build_budget_report(
        today=kwargs.pop("today", TODAY),
        categories=kwargs.pop("categories", CATEGORIES),
        budgeted=kwargs.pop("budgeted", BUDGETED),
        transactions=transactions,
        **kwargs,
    )


def test_free_money_is_income_minus_what_is_already_budgeted():
    result = report(
        [
            TransactionInfo("1", datetime.date(2026, 8, 1), 400000, "inc"),
            TransactionInfo("2", datetime.date(2026, 8, 2), -180000, "rent"),
            TransactionInfo("3", datetime.date(2026, 8, 7), -9000, "food"),
        ]
    )
    assert result.expected_income_cents == 400000
    assert result.committed_cents == 192000
    assert result.free_cents == 208000
    assert result.discretionary_spent_cents == 9000
    assert result.remaining_cents == 199000
    assert round(result.remaining_percent, 4) == round(199000 / 208000, 4)


def test_overspending_a_committed_category_eats_free_money():
    result = report(
        [
            TransactionInfo("1", datetime.date(2026, 8, 1), 400000, "inc"),
            TransactionInfo("2", datetime.date(2026, 8, 5), -13500, "power"),
        ]
    )
    assert result.committed_overspend_cents == 1500
    assert result.spent_cents == 1500
    assert result.remaining_cents == result.free_cents - 1500


def test_transfers_off_budget_and_starting_balances_never_count():
    result = report(
        [
            TransactionInfo("1", datetime.date(2026, 8, 1), 400000, "inc"),
            TransactionInfo("2", datetime.date(2026, 8, 3), -50000, "food", is_transfer=True),
            TransactionInfo("3", datetime.date(2026, 8, 4), -60000, "food", off_budget=True),
            TransactionInfo("4", datetime.date(2026, 8, 5), -70000, None, is_starting_balance=True),
        ]
    )
    assert result.discretionary_spent_cents == 0
    assert result.uncategorized_count == 0


def test_an_uncategorized_charge_still_spends_real_money():
    result = report(
        [
            TransactionInfo("1", datetime.date(2026, 8, 1), 400000, "inc"),
            TransactionInfo("2", datetime.date(2026, 8, 11), -2500, None),
        ]
    )
    assert result.uncategorized_count == 1
    assert result.uncategorized_cents == 2500
    assert result.discretionary_spent_cents == 2500


def test_a_refund_gives_the_money_back():
    result = report(
        [
            TransactionInfo("1", datetime.date(2026, 8, 1), 400000, "inc"),
            TransactionInfo("2", datetime.date(2026, 8, 7), -9000, "food"),
            TransactionInfo("3", datetime.date(2026, 8, 9), 3000, "food"),
        ]
    )
    assert result.discretionary_spent_cents == 6000


def test_a_refund_on_a_committed_category_reduces_its_overspend():
    result = report(
        [
            TransactionInfo("1", datetime.date(2026, 8, 1), 400000, "inc"),
            TransactionInfo("2", datetime.date(2026, 8, 5), -13500, "power"),
            TransactionInfo("3", datetime.date(2026, 8, 6), 1500, "power"),
        ]
    )
    assert result.committed_overspend_cents == 0


def test_explicit_committed_groups_replace_the_budgeted_default():
    transactions = [
        TransactionInfo("1", datetime.date(2026, 8, 1), 400000, "inc"),
        TransactionInfo("2", datetime.date(2026, 8, 7), -9000, "food"),
    ]
    default = report(transactions)
    grouped = report(transactions, committed_groups=["Everyday"])
    # Groceries is committed under the explicit grouping, so it stops competing
    # for free money and its budget of zero makes the whole spend an overspend.
    assert default.discretionary_spent_cents == 9000
    assert grouped.discretionary_spent_cents == 0
    assert grouped.committed_overspend_cents == 9000
    assert grouped.committed_groups == ["Everyday"]


def test_income_falls_back_to_a_trailing_average_before_payday():
    result = report(
        [TransactionInfo("1", datetime.date(2026, 8, 3), -9000, "food")],
        income_history=[
            (datetime.date(2026, 5, 1), 380000),
            (datetime.date(2026, 6, 1), 400000),
            (datetime.date(2026, 7, 1), 420000),
        ],
    )
    assert result.income_basis == "average"
    assert result.expected_income_cents == 400000


def test_income_already_received_wins_over_a_lower_average():
    result = report(
        [TransactionInfo("1", datetime.date(2026, 8, 1), 500000, "inc")],
        income_history=[(datetime.date(2026, 7, 1), 400000)],
    )
    assert result.income_basis == "received"
    assert result.expected_income_cents == 500000


def test_an_explicit_income_override_wins_over_everything():
    result = report(
        [TransactionInfo("1", datetime.date(2026, 8, 1), 500000, "inc")],
        income_override_cents=450000,
    )
    assert result.income_basis == "override"
    assert result.expected_income_cents == 450000


def test_a_budget_with_no_income_reports_itself_unconfigured():
    result = report([TransactionInfo("1", datetime.date(2026, 8, 7), -9000, "food")])
    assert result.configured is False
    assert result.income_basis == "unknown"
    # Never divide by a free-money figure that does not exist.
    assert result.remaining_percent == 0.0
    assert result.daily_safe_to_spend_cents == 0


def test_committed_spending_above_income_leaves_no_free_money():
    result = report(
        [TransactionInfo("1", datetime.date(2026, 8, 1), 100000, "inc")],
    )
    assert result.free_cents < 0
    assert result.on_track is True  # nothing to pace against
    assert result.daily_safe_to_spend_cents == 0


def test_pace_and_projection_use_the_day_of_the_month():
    result = report(
        [
            TransactionInfo("1", datetime.date(2026, 8, 1), 400000, "inc"),
            TransactionInfo("2", datetime.date(2026, 8, 7), -9000, "food"),
        ]
    )
    assert result.day_of_month == 21
    assert result.days_in_month == 31
    assert result.days_remaining == 11
    assert result.pace_expected_cents == round(208000 * 21 / 31)
    assert result.on_track is True
    assert result.projected_spend_cents == round(9000 * 31 / 21)


def test_spending_past_the_pace_is_reported_as_behind():
    result = report(
        [
            TransactionInfo("1", datetime.date(2026, 8, 1), 400000, "inc"),
            TransactionInfo("2", datetime.date(2026, 8, 7), -200000, "food"),
        ]
    )
    # Money is left, but far less than an even burn would have kept back.
    assert result.on_track is False
    assert result.pace_delta_cents < 0
    assert result.remaining_cents == 8000
    assert result.daily_safe_to_spend_cents == 8000 // 11


def test_overspending_the_month_reports_no_safe_daily_spend():
    result = report(
        [
            TransactionInfo("1", datetime.date(2026, 8, 1), 400000, "inc"),
            TransactionInfo("2", datetime.date(2026, 8, 7), -300000, "food"),
        ]
    )
    assert result.remaining_cents < 0
    assert result.remaining_percent < 0
    assert result.daily_safe_to_spend_cents == 0


def test_transactions_outside_the_month_are_dropped():
    result = report(
        [
            TransactionInfo("1", datetime.date(2026, 8, 1), 400000, "inc"),
            TransactionInfo("2", datetime.date(2026, 7, 30), -50000, "food"),
            TransactionInfo("3", datetime.date(2026, 9, 2), -50000, "food"),
        ]
    )
    assert result.discretionary_spent_cents == 0


def test_top_categories_and_committed_lines_describe_the_month():
    result = report(
        [
            TransactionInfo("1", datetime.date(2026, 8, 1), 400000, "inc"),
            TransactionInfo("2", datetime.date(2026, 8, 7), -9000, "food"),
            TransactionInfo("3", datetime.date(2026, 8, 8), -4000, "out"),
            TransactionInfo("4", datetime.date(2026, 8, 5), -13500, "power"),
        ]
    )
    assert [item["category_name"] for item in result.top_categories] == ["Groceries", "Dining"]
    overspent = next(line for line in result.committed_lines if line["category_id"] == "power")
    assert overspent["overspent_cents"] == 1500
    assert result.flexible_groups == ["Everyday"]


def test_average_income_ignores_the_current_month():
    average = average_monthly_income(
        [
            (datetime.date(2026, 8, 1), 999999),
            (datetime.date(2026, 7, 1), 400000),
            (datetime.date(2026, 6, 1), 400000),
        ],
        months=3,
        before=TODAY,
    )
    assert average == 400000
    assert average_monthly_income([], months=3, before=TODAY) == 0
    assert average_monthly_income([(datetime.date(2026, 7, 1), 1)], months=0, before=TODAY) == 0


def test_month_bounds_and_money_formatting():
    start, end = month_bounds(datetime.date(2026, 2, 10))
    assert (start, end) == (datetime.date(2026, 2, 1), datetime.date(2026, 2, 28))
    assert format_money(123456) == "$1,234.56"
    assert format_money(-500) == "-$5.00"
    assert format_money(100, "SEK") == "1.00 SEK"


def test_money_that_has_not_cleared_the_bank_is_still_spent():
    """Committed money is committed; waiting for the bank does not give it back."""
    result = report(
        [
            TransactionInfo("1", datetime.date(2026, 8, 1), 400000, "inc"),
            TransactionInfo("2", datetime.date(2026, 8, 19), -9000, "food"),
            TransactionInfo("3", datetime.date(2026, 8, 20), -4500, "out"),
        ]
    )
    # The report never inspects a cleared flag, so both count in full.
    assert result.discretionary_spent_cents == 13500
    assert result.remaining_cents == result.free_cents - 13500


# --------------------------------------------------- Actual's tracking budget


def test_income_budgeted_in_actual_is_used_as_expected_income():
    """Tracking budgets state expected income; that is the month's intent."""
    result = report(
        [TransactionInfo("1", datetime.date(2026, 8, 3), -9000, "food")],
        budgeted={"inc": 420000, "rent": 180000, "power": 12000},
    )
    assert result.income_basis == "budgeted"
    assert result.expected_income_cents == 420000
    assert result.income_budgeted_cents == 420000
    # Budgeted income is income, never a bill.
    assert result.committed_cents == 192000
    assert result.free_cents == 228000


def test_free_money_equals_actuals_projected_savings():
    """Budget income and bills only, and the two apps agree on the number."""
    budgeted = {"inc": 420000, "rent": 180000, "power": 12000}
    result = report([], budgeted=budgeted)
    projected_savings = budgeted["inc"] - (budgeted["rent"] + budgeted["power"])
    assert result.free_cents == projected_savings


def test_budgeted_income_outranks_what_has_landed_so_far():
    result = report(
        [TransactionInfo("1", datetime.date(2026, 8, 1), 500000, "inc")],
        budgeted={"inc": 420000, "rent": 180000, "power": 12000},
    )
    assert result.expected_income_cents == 420000
    # What actually arrived stays visible next to it.
    assert result.income_received_cents == 500000


def test_a_clerk_override_still_beats_a_budgeted_figure():
    result = report(
        [],
        budgeted={"inc": 420000, "rent": 180000, "power": 12000},
        income_override_cents=450000,
    )
    assert result.income_basis == "override"
    assert result.expected_income_cents == 450000


def test_an_envelope_budget_has_no_income_row_and_is_unaffected():
    result = report(
        [TransactionInfo("1", datetime.date(2026, 8, 1), 400000, "inc")],
        budgeted=BUDGETED,
    )
    assert result.income_basis == "received"
    assert result.income_budgeted_cents == 0


# ---------------------------------------- variable and annual commitments


def monthly_budget(months, amounts):
    """`budgeted_history` with the same amounts repeated across months."""
    return {month: dict(amounts) for month in months}


# The twelve whole months before March 2026, which is the carryover window
# a bill accrued from March to February would have filled.
YEAR_BEFORE_MARCH = [f"2025-{month:02d}" for month in range(3, 13)] + ["2026-01", "2026-02"]


def test_an_annual_bill_accrued_monthly_is_not_charged_twice():
    """The failure that made the divide-by-twelve advice wrong."""
    categories = [
        CategoryInfo("inc", "Paycheck", "Income", is_income=True),
        CategoryInfo("ins", "Insurance", "Bills"),
    ]
    march = datetime.date(2026, 3, 20)
    # A twelfth budgeted every month, nothing spent until the invoice lands.
    history = monthly_budget(YEAR_BEFORE_MARCH, {"ins": 5000})
    # Twelve prior months of accrual, then the bill.
    transactions = [
        TransactionInfo("pay", datetime.date(2026, 3, 1), 420000, "inc"),
        TransactionInfo("bill", datetime.date(2026, 3, 3), -60000, "ins"),
    ]
    result = build_budget_report(
        today=march,
        categories=categories,
        budgeted={"ins": 5000},
        transactions=transactions,
        budgeted_history=history,
        income_override_cents=420000,
    )
    # Twelve months at $50 were set aside and never spent.
    assert result.committed_carried_cents == 60000
    assert result.committed_overspend_cents == 0
    assert result.remaining_cents == result.free_cents


def test_an_annual_bill_larger_than_what_was_accrued_still_overspends():
    categories = [
        CategoryInfo("inc", "Paycheck", "Income", is_income=True),
        CategoryInfo("ins", "Insurance", "Bills"),
    ]
    # Only three months of accrual before a $600 bill.
    history = monthly_budget(["2025-12", "2026-01", "2026-02", "2026-03"], {"ins": 5000})
    result = build_budget_report(
        today=datetime.date(2026, 3, 20),
        categories=categories,
        budgeted={"ins": 5000},
        transactions=[TransactionInfo("bill", datetime.date(2026, 3, 3), -60000, "ins")],
        budgeted_history=history,
        income_override_cents=420000,
    )
    # $150 accrued plus $50 this month leaves $400 genuinely unfunded.
    assert result.committed_carried_cents == 15000
    assert result.committed_overspend_cents == 40000


def test_a_variable_bill_absorbs_a_high_month_from_its_own_cushion():
    categories = [
        CategoryInfo("inc", "Paycheck", "Income", is_income=True),
        CategoryInfo("power", "Electric", "Bills"),
    ]
    history = {
        "2026-06": {"power": 12000},
        "2026-07": {"power": 12000},
        "2026-08": {"power": 12000},
    }
    transactions = [
        TransactionInfo("jun", datetime.date(2026, 6, 12), -9800, "power"),
        TransactionInfo("jul", datetime.date(2026, 7, 12), -8700, "power"),
        TransactionInfo("aug", datetime.date(2026, 8, 12), -17000, "power"),
    ]
    result = build_budget_report(
        today=datetime.date(2026, 8, 21),
        categories=categories,
        budgeted={"power": 12000},
        transactions=transactions,
        budgeted_history=history,
        income_override_cents=420000,
    )
    # Two quiet months left $55 in the category, which covers the $50 spike.
    assert result.committed_carried_cents == 5500
    assert result.committed_overspend_cents == 0


def test_a_month_that_ran_over_is_not_charged_again_next_month():
    """Overspending is settled when it happens; it never becomes a debt."""
    categories = [
        CategoryInfo("inc", "Paycheck", "Income", is_income=True),
        CategoryInfo("power", "Electric", "Bills"),
    ]
    history = {"2026-07": {"power": 12000}, "2026-08": {"power": 12000}}
    result = build_budget_report(
        today=datetime.date(2026, 8, 21),
        categories=categories,
        budgeted={"power": 12000},
        transactions=[
            TransactionInfo("jul", datetime.date(2026, 7, 12), -30000, "power"),
            TransactionInfo("aug", datetime.date(2026, 8, 12), -11000, "power"),
        ],
        budgeted_history=history,
        income_override_cents=420000,
    )
    assert result.committed_carried_cents == 0
    assert result.committed_overspend_cents == 0


def test_the_cushion_is_reported_per_category():
    categories = [
        CategoryInfo("inc", "Paycheck", "Income", is_income=True),
        CategoryInfo("ins", "Insurance", "Bills"),
        CategoryInfo("rent", "Rent", "Bills"),
    ]
    history = monthly_budget(YEAR_BEFORE_MARCH, {"ins": 5000, "rent": 180000})
    result = build_budget_report(
        today=datetime.date(2026, 3, 20),
        categories=categories,
        budgeted={"ins": 5000, "rent": 180000},
        transactions=[
            TransactionInfo("r1", datetime.date(2026, 1, 2), -180000, "rent"),
            TransactionInfo("r2", datetime.date(2026, 2, 2), -180000, "rent"),
        ],
        budgeted_history=history,
        income_override_cents=420000,
    )
    lines = {line["category_id"]: line for line in result.committed_lines}
    # Rent was budgeted every month and mostly paid; insurance never was.
    assert lines["ins"]["carried_cents"] == 60000
    assert lines["rent"]["carried_cents"] == 10 * 180000


def test_the_carryover_window_does_not_reach_beyond_a_year():
    categories = [
        CategoryInfo("inc", "Paycheck", "Income", is_income=True),
        CategoryInfo("ins", "Insurance", "Bills"),
    ]
    # Two years of accrual; only the last twelve months may count.
    history = monthly_budget(
        [f"{year}-{month:02d}" for year in (2024, 2025) for month in range(1, 13)]
        + [f"2026-{month:02d}" for month in range(1, 9)],
        {"ins": 5000},
    )
    result = build_budget_report(
        today=datetime.date(2026, 8, 21),
        categories=categories,
        budgeted={"ins": 5000},
        transactions=[],
        budgeted_history=history,
        income_override_cents=420000,
    )
    assert result.committed_carried_cents == 12 * 5000


def test_without_budget_history_nothing_is_carried():
    result = report(
        [
            TransactionInfo("1", datetime.date(2026, 8, 1), 400000, "inc"),
            TransactionInfo("2", datetime.date(2026, 8, 5), -13500, "power"),
        ]
    )
    assert result.committed_carried_cents == 0
    assert result.committed_overspend_cents == 1500


def test_month_helpers():
    from actual_clerk.domain.budget import month_key, months_before

    assert month_key(datetime.date(2026, 3, 9)) == "2026-03"
    assert months_before(datetime.date(2026, 3, 1), 3) == ["2025-12", "2026-01", "2026-02"]
    assert months_before(datetime.date(2026, 1, 1), 2) == ["2025-11", "2025-12"]
    assert months_before(datetime.date(2026, 3, 1), 0) == []


# ----------------------------------------------- categories Actual deleted


def test_spending_on_a_deleted_category_counts_as_uncategorized():
    """Actual tombstones a deleted category and leaves its transactions
    pointing at it, so the id survives with nothing to resolve it to."""
    result = report(
        [
            TransactionInfo("1", datetime.date(2026, 8, 1), 400000, "inc"),
            TransactionInfo("2", datetime.date(2026, 8, 11), -250456, "cat-that-was-deleted"),
        ]
    )
    assert result.uncategorized_cents == 250456
    assert result.uncategorized_count == 1
    # It is still real spending, so it still competes for free money.
    assert result.discretionary_spent_cents == 250456


def test_a_deleted_category_never_appears_as_a_named_top_category():
    result = report(
        [
            TransactionInfo("1", datetime.date(2026, 8, 1), 400000, "inc"),
            TransactionInfo("2", datetime.date(2026, 8, 11), -250456, "cat-that-was-deleted"),
            TransactionInfo("3", datetime.date(2026, 8, 12), -3000, "out"),
        ]
    )
    assert [line["category_id"] for line in result.top_categories] == ["out"]


def test_a_refund_on_a_deleted_category_gives_the_money_back():
    result = report(
        [
            TransactionInfo("1", datetime.date(2026, 8, 1), 400000, "inc"),
            TransactionInfo("2", datetime.date(2026, 8, 11), -9000, "gone"),
            TransactionInfo("3", datetime.date(2026, 8, 13), 3000, "gone"),
        ]
    )
    assert result.uncategorized_cents == 6000
    assert result.discretionary_spent_cents == 6000


def test_a_hidden_category_is_still_a_real_category():
    """Hidden is not deleted: Actual still returns it, so it keeps its name."""
    categories = [*CATEGORIES, CategoryInfo("old", "Old Hobby", "Everyday", hidden=True)]
    result = report(
        [
            TransactionInfo("1", datetime.date(2026, 8, 1), 400000, "inc"),
            TransactionInfo("2", datetime.date(2026, 8, 11), -4200, "old"),
        ],
        categories=categories,
    )
    assert result.uncategorized_cents == 0
    assert [line["category_id"] for line in result.top_categories] == ["old"]
