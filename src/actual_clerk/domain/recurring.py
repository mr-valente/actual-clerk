"""Find the charges that come back every month whether you plan for them or not.

Recurring spending is the part of a budget that is easiest to under-count and
most expensive to get wrong. Clerk detects it from transaction history rather
than asking the user to list it: a merchant charged at a regular interval three
or more times is a commitment, and the report says what it costs per month,
whether the price moved, and whether an expected charge failed to arrive.
"""

from __future__ import annotations

import datetime
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

DAYS_PER_MONTH = 30.44
_MIN_OCCURRENCES = 3
# How far the intervals may wander before a series stops looking scheduled.
_MAX_INTERVAL_SPREAD = 0.28
# A subscription bills the same amount every time; a utility bill does not.
_FIXED_AMOUNT_SPREAD = 0.035
# How far a subscription's whole history may move across price changes before
# it is better described as a variable bill.
_STEP_AMOUNT_SPREAD = 0.25
_PRICE_CHANGE_THRESHOLD = 0.05
# A percentage change on a small charge is not news. Both tests must pass
# before a price move is worth reporting.
_PRICE_CHANGE_MIN_CENTS = 100
# Cadences faster than monthly are usually habits rather than commitments: a
# weekly grocery run and a weekly subscription box look identical to an interval
# test and completely different to an amount test. Only a charge that repeats at
# very nearly the same amount survives.
_SUB_MONTHLY_CADENCES = frozenset({"weekly", "biweekly"})
_SUB_MONTHLY_AMOUNT_SPREAD = 0.005

CADENCES: tuple[tuple[str, float, float, float], ...] = (
    # label, minimum days, maximum days, nominal days
    ("weekly", 6, 8.5, 7),
    ("biweekly", 12, 16.5, 14),
    ("monthly", 26, 35, DAYS_PER_MONTH),
    ("bimonthly", 55, 70, 60.9),
    ("quarterly", 84, 96, 91.3),
    ("semiannual", 175, 195, 182.6),
    ("yearly", 350, 380, 365.25),
)


@dataclass(frozen=True)
class Charge:
    date: datetime.date
    amount_cents: int
    merchant_key: str
    payee_name: str = ""
    category_id: str | None = None
    category_name: str = ""
    account_name: str = ""


@dataclass
class RecurringSeries:
    merchant_key: str
    label: str
    cadence: str
    interval_days: float
    occurrences: int
    typical_amount_cents: int
    latest_amount_cents: int
    first_amount_cents: int
    monthly_cost_cents: int
    first_seen: str
    last_seen: str
    next_expected: str
    days_overdue: int
    kind: str
    category_id: str | None
    category_name: str
    account_name: str
    price_change_percent: float
    budgeted: bool = False
    budgeted_cents: int = 0
    flags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in self.__dataclass_fields__}


def _classify_cadence(interval_days: float) -> tuple[str, float] | None:
    for label, low, high, nominal in CADENCES:
        if low <= interval_days <= high:
            return label, nominal
    return None


def _spread(values: Sequence[float]) -> float:
    """Relative dispersion, resistant to a single unusual member.

    A median absolute deviation is used rather than a standard deviation so one
    double-billed month does not disqualify an otherwise regular series.
    """

    if len(values) < 2:
        return 0.0
    middle = statistics.median(values)
    if middle == 0:
        return 0.0
    return statistics.median([abs(value - middle) for value in values]) / abs(middle)


def detect_recurring(
    charges: Sequence[Charge],
    *,
    today: datetime.date | None = None,
    budgeted_by_category: dict[str, int] | None = None,
) -> list[RecurringSeries]:
    """Group charges by merchant and keep the ones arriving on a schedule."""

    today = today or datetime.date.today()
    budgeted_by_category = budgeted_by_category or {}
    grouped: dict[str, list[Charge]] = {}
    for charge in charges:
        if not charge.merchant_key or charge.amount_cents >= 0:
            continue
        grouped.setdefault(charge.merchant_key, []).append(charge)

    series: list[RecurringSeries] = []
    for merchant_key, items in grouped.items():
        found = _series_for(merchant_key, items, today, budgeted_by_category)
        if found:
            series.append(found)
    series.sort(key=lambda item: item.monthly_cost_cents, reverse=True)
    return series


def _series_for(
    merchant_key: str,
    items: list[Charge],
    today: datetime.date,
    budgeted_by_category: dict[str, int],
) -> RecurringSeries | None:
    items = sorted(items, key=lambda charge: charge.date)
    # Two charges on one day are a split payment, not two billing cycles.
    collapsed: list[Charge] = []
    for charge in items:
        if collapsed and collapsed[-1].date == charge.date:
            continue
        collapsed.append(charge)
    if len(collapsed) < _MIN_OCCURRENCES:
        return None

    intervals = [
        (later.date - earlier.date).days
        for earlier, later in zip(collapsed, collapsed[1:], strict=False)
    ]
    intervals = [interval for interval in intervals if interval > 0]
    if len(intervals) < _MIN_OCCURRENCES - 1:
        return None

    median_interval = statistics.median(intervals)
    if _spread(intervals) > _MAX_INTERVAL_SPREAD:
        return None
    cadence = _classify_cadence(median_interval)
    if cadence is None:
        return None
    label, nominal_days = cadence

    amounts = [abs(charge.amount_cents) for charge in collapsed]
    typical = round(statistics.median(amounts))
    # A subscription that raised its price is still a subscription. Judge the
    # billing behaviour on the current price level rather than on the whole
    # history, which a single increase would otherwise make look variable.
    steady_now = _spread(amounts[-3:]) <= _FIXED_AMOUNT_SPREAD
    kind = "subscription" if steady_now and _spread(amounts) <= _STEP_AMOUNT_SPREAD else "recurring"

    if label in _SUB_MONTHLY_CADENCES and (
        kind != "subscription" or _spread(amounts[-4:]) > _SUB_MONTHLY_AMOUNT_SPREAD
    ):
        # A weekly habit, not a weekly commitment.
        return None

    first_amount, latest_amount = amounts[0], amounts[-1]
    price_change = (latest_amount - first_amount) / first_amount if first_amount else 0.0

    last_charge = collapsed[-1]
    next_expected = last_charge.date + datetime.timedelta(days=round(median_interval))
    grace = max(3, round(median_interval * 0.25))
    days_overdue = max(0, (today - next_expected).days - grace)

    category_id = last_charge.category_id
    budgeted_cents = budgeted_by_category.get(category_id or "", 0)

    flags: list[str] = []
    if (
        kind == "subscription"
        and abs(price_change) >= _PRICE_CHANGE_THRESHOLD
        and abs(latest_amount - first_amount) >= _PRICE_CHANGE_MIN_CENTS
    ):
        direction = "increased" if price_change > 0 else "decreased"
        flags.append(f"Price {direction} {abs(price_change):.0%} since the first charge")
    if days_overdue > 0:
        flags.append(f"Expected {days_overdue} day(s) ago and has not arrived")
    if not category_id:
        flags.append("Not categorized in Actual")
    elif budgeted_cents <= 0:
        flags.append("No budget set for its category this month")

    return RecurringSeries(
        merchant_key=merchant_key,
        label=last_charge.payee_name or merchant_key.title(),
        cadence=label,
        interval_days=round(median_interval, 1),
        occurrences=len(collapsed),
        typical_amount_cents=typical,
        latest_amount_cents=latest_amount,
        first_amount_cents=first_amount,
        monthly_cost_cents=round(
            (latest_amount if kind == "subscription" else typical) * DAYS_PER_MONTH / nominal_days
        ),
        first_seen=collapsed[0].date.isoformat(),
        last_seen=last_charge.date.isoformat(),
        next_expected=next_expected.isoformat(),
        days_overdue=days_overdue,
        kind=kind,
        category_id=category_id,
        category_name=last_charge.category_name,
        account_name=last_charge.account_name,
        price_change_percent=round(price_change, 4),
        budgeted=budgeted_cents > 0,
        budgeted_cents=budgeted_cents,
        flags=flags,
    )


def summarize_recurring(series: Sequence[RecurringSeries]) -> dict[str, Any]:
    subscriptions = [item for item in series if item.kind == "subscription"]
    return {
        "count": len(series),
        "subscription_count": len(subscriptions),
        "monthly_total_cents": sum(item.monthly_cost_cents for item in series),
        "subscription_monthly_cents": sum(item.monthly_cost_cents for item in subscriptions),
        "unbudgeted_monthly_cents": sum(
            item.monthly_cost_cents for item in series if not item.budgeted
        ),
        "unbudgeted_count": sum(1 for item in series if not item.budgeted),
        "overdue_count": sum(1 for item in series if item.days_overdue > 0),
        "price_changes": [
            {"label": item.label, "percent": item.price_change_percent}
            for item in series
            if item.kind == "subscription"
            and abs(item.price_change_percent) >= _PRICE_CHANGE_THRESHOLD
            and abs(item.latest_amount_cents - item.first_amount_cents)
            >= _PRICE_CHANGE_MIN_CENTS
        ],
    }
