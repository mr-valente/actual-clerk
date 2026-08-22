from __future__ import annotations

import datetime

from actual_clerk.domain.recurring import Charge, detect_recurring, summarize_recurring

TODAY = datetime.date(2026, 8, 21)


def series(amounts, *, key="netflix", label="Netflix", step=30, category="cat-subs", last=None):
    """A regular series whose final charge lands shortly before today."""
    last = last or TODAY - datetime.timedelta(days=6)
    start = last - datetime.timedelta(days=step * (len(amounts) - 1))
    return [
        Charge(
            date=start + datetime.timedelta(days=step * index),
            amount_cents=amount,
            merchant_key=key,
            payee_name=label,
            category_id=category,
            category_name="Subscriptions",
            account_name="Visa",
        )
        for index, amount in enumerate(amounts)
    ]


def test_a_fixed_monthly_charge_is_a_subscription():
    [found] = detect_recurring(series([-1599] * 4), today=TODAY)
    assert found.kind == "subscription"
    assert found.cadence == "monthly"
    assert found.typical_amount_cents == 1599
    assert found.monthly_cost_cents == 1599


def test_a_variable_monthly_bill_is_recurring_not_a_subscription():
    charges = series(
        [-9800, -11200, -8700, -13400, -10100],
        key="pg&e",
        label="PG&E",
        step=31,
    )
    [found] = detect_recurring(charges, today=TODAY)
    assert found.kind == "recurring"
    assert found.cadence == "monthly"


def test_a_subscription_that_raised_its_price_is_still_a_subscription():
    charges = series([-1599, -1599, -1599, -1799, -1799, -1799])
    [found] = detect_recurring(charges, today=TODAY)
    assert found.kind == "subscription"
    assert found.latest_amount_cents == 1799
    # The monthly cost follows the current price, not the historical median.
    assert found.monthly_cost_cents == 1799
    assert round(found.price_change_percent, 2) == 0.13
    assert any("Price increased" in flag for flag in found.flags)


def test_a_yearly_renewal_is_amortized_across_the_months():
    charges = series([-9900] * 3, key="domains", label="Domains", step=365)
    [found] = detect_recurring(charges, today=TODAY)
    assert found.cadence == "yearly"
    assert found.monthly_cost_cents == round(9900 * 30.44 / 365.25)


def test_a_charge_that_failed_to_arrive_is_flagged_as_overdue():
    charges = series([-4500] * 4, key="gym", label="Planet Fitness", last=datetime.date(2026, 5, 15))
    [found] = detect_recurring(charges, today=TODAY)
    assert found.days_overdue > 0
    assert any("has not arrived" in flag for flag in found.flags)


def test_irregular_visits_to_the_same_shop_are_not_recurring():
    charges = [
        Charge(datetime.date(2026, 8, day), -650, "blue bottle", "Blue Bottle")
        for day in (3, 9, 11, 12, 27)
    ]
    assert detect_recurring(charges, today=TODAY) == []


def test_two_charges_are_not_enough_to_establish_a_schedule():
    assert detect_recurring(series([-1599] * 2), today=TODAY) == []


def test_a_split_payment_on_one_day_is_not_two_billing_cycles():
    charges = series([-1599] * 4)
    charges.append(
        Charge(charges[-1].date, -1599, "netflix", "Netflix", "cat-subs", "Subscriptions", "Visa")
    )
    [found] = detect_recurring(charges, today=TODAY)
    assert found.occurrences == 4


def test_income_and_refunds_are_never_treated_as_commitments():
    charges = series([400000] * 4, key="paycheck", label="Employer")
    assert detect_recurring(charges, today=TODAY) == []


def test_an_uncategorized_or_unbudgeted_series_says_so():
    uncategorized = series([-1599] * 4, category=None)
    [found] = detect_recurring(uncategorized, today=TODAY)
    assert "Not categorized in Actual" in found.flags

    [budgeted] = detect_recurring(
        series([-1599] * 4),
        today=TODAY,
        budgeted_by_category={"cat-subs": 2000},
    )
    assert budgeted.budgeted is True
    assert budgeted.flags == []


def test_the_summary_adds_up_what_the_month_is_committed_to():
    charges = (
        series([-1599] * 4)
        + series([-9800, -11200, -8700, -13400], key="pg&e", label="PG&E", step=31, category="cat-power")
        + series([-4500] * 4, key="gym", label="Gym", category=None, last=datetime.date(2026, 5, 15))
    )
    found = detect_recurring(charges, today=TODAY, budgeted_by_category={"cat-power": 12000})
    summary = summarize_recurring(found)

    assert summary["count"] == 3
    assert summary["subscription_count"] == 2
    assert summary["overdue_count"] == 1
    assert summary["unbudgeted_count"] == 2
    assert summary["monthly_total_cents"] == sum(item.monthly_cost_cents for item in found)
    assert summary["price_changes"] == []


def test_results_are_ordered_by_what_they_cost_per_month():
    charges = (
        series([-500] * 4, key="cheap", label="Cheap")
        + series([-9900] * 4, key="pricey", label="Pricey")
    )
    found = detect_recurring(charges, today=TODAY)
    assert [item.label for item in found] == ["Pricey", "Cheap"]


def test_a_weekly_habit_is_not_a_commitment():
    """A weekly grocery run repeats as reliably as a subscription does."""
    habit = series([-8200, -9140, -7710, -8940], key="trader joes", label="Trader Joes", step=7)
    assert detect_recurring(habit, today=TODAY) == []


def test_a_genuinely_weekly_subscription_still_counts():
    box = series([-5999] * 4, key="meal box", label="Meal Box", step=7)
    [found] = detect_recurring(box, today=TODAY)
    assert found.cadence == "weekly"
    assert found.kind == "subscription"
    assert found.monthly_cost_cents == round(5999 * 30.44 / 7)


def test_a_weekly_charge_that_drifts_a_little_is_still_a_habit():
    drifting = series([-640, -640, -680, -700], key="cafe", label="Cafe", step=7)
    assert detect_recurring(drifting, today=TODAY) == []


def test_a_few_cents_is_not_a_price_increase():
    charges = series([-320, -320, -320, -350, -350, -350], key="small", label="Small")
    [found] = detect_recurring(charges, today=TODAY, budgeted_by_category={"cat-subs": 500})
    assert found.price_change_percent > 0.05
    assert found.flags == []
    assert summarize_recurring([found])["price_changes"] == []
