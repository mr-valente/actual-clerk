"""The free-money budget Clerk reports on.

The model is deliberately small enough to hold in your head:

    free money = expected income - what is already committed
    available  = free money + money handed back by a month already reported
    remaining  = available - what has been spent since the 1st

Everything the user sets up as a recurring bill lives in Actual as a budgeted
amount. Clerk reads those amounts, subtracts them from expected income, and
tracks the rest against the month's discretionary spending. Overspending a
committed category also eats into free money, because the money has to come
from somewhere.

A refund belongs to the month that was charged for the purchase. Within its own
category it cancels this month's spending first. Whatever is left over reverses
a charge from a month that has already been reported and closed: that report
stands, and the money is not spending driven below zero now -- it is money this
month has back to spend. Free money itself is left alone, so it remains the
figure Actual shows as Projected Savings, and the returned money sits beside it.
"""

from __future__ import annotations

import calendar
import datetime
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

CENTS = 100
# How far back an unspent commitment stays available to its own category. A
# year covers an annual bill accrued a twelfth at a time, which is the longest
# cycle this model needs to hold.
CARRYOVER_MONTHS = 12


@dataclass(frozen=True)
class CategoryInfo:
    id: str
    name: str
    group_name: str
    is_income: bool = False
    hidden: bool = False


@dataclass(frozen=True)
class TransactionInfo:
    id: str
    date: datetime.date
    amount_cents: int
    category_id: str | None = None
    account_id: str = ""
    account_name: str = ""
    payee_name: str = ""
    off_budget: bool = False
    is_transfer: bool = False
    is_starting_balance: bool = False

    @property
    def spend_cents(self) -> int:
        """Money leaving the budget, as a positive number."""
        return -self.amount_cents if self.amount_cents < 0 else 0


@dataclass(frozen=True)
class AnticipatedInfo:
    """A charge a card app announced that the bank feed has not delivered yet."""

    id: str
    amount_cents: int  # Actual's sign: negative for money going out
    account_id: str = ""
    off_budget: bool = False
    merchant: str = ""
    # Provisional, from memory or taught: decides which budget line the charge
    # draws on before the bank's row exists. Empty means "nobody knows yet".
    category_id: str = ""

    @property
    def spend_cents(self) -> int:
        return -self.amount_cents if self.amount_cents < 0 else 0


@dataclass
class BudgetReport:
    month: str
    days_in_month: int
    day_of_month: int
    days_remaining: int
    configured: bool
    income_basis: str
    expected_income_cents: int
    income_received_cents: int
    income_average_cents: int
    income_budgeted_cents: int
    committed_cents: int
    committed_spent_cents: int
    committed_overspend_cents: int
    # Budgeted in earlier months and never spent, so still available to its own
    # category. Free money was already reduced by it at the time.
    committed_carried_cents: int
    free_cents: int
    # Refunded beyond what this month spent in that category, so it reverses a
    # charge an earlier report already counted. Money back, not negative spend.
    returned_cents: int
    available_cents: int
    discretionary_spent_cents: int
    uncategorized_cents: int
    uncategorized_count: int
    # Announced by a card app but not yet posted by the bank. Counted as spent
    # now, held outside Actual, and released when the real row arrives. A
    # charge with a provisional category is charged to that category (and so
    # to its budget when committed); the rest is discretionary spending.
    anticipated_cents: int
    anticipated_count: int
    anticipated_committed_cents: int
    spent_cents: int
    remaining_cents: int
    remaining_percent: float
    spent_percent: float
    daily_safe_to_spend_cents: int
    pace_expected_cents: int
    pace_delta_cents: int
    on_track: bool
    projected_spend_cents: int
    projected_remaining_cents: int
    committed_groups: list[str] = field(default_factory=list)
    flexible_groups: list[str] = field(default_factory=list)
    top_categories: list[dict[str, Any]] = field(default_factory=list)
    committed_lines: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in self.__dataclass_fields__}


def month_bounds(day: datetime.date) -> tuple[datetime.date, datetime.date]:
    days = calendar.monthrange(day.year, day.month)[1]
    return day.replace(day=1), day.replace(day=days)


def month_key(day: datetime.date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def months_before(start: datetime.date, count: int) -> list[str]:
    """The `count` whole months immediately before `start`, oldest first."""
    keys = []
    year, month = start.year, start.month
    for _ in range(count):
        month -= 1
        if month == 0:
            year, month = year - 1, 12
        keys.append(f"{year:04d}-{month:02d}")
    return list(reversed(keys))


def committed_carryover(
    *,
    committed_ids: set[str],
    budgeted_history: dict[str, dict[str, int]],
    transactions: Sequence[TransactionInfo],
    start: datetime.date,
    months: int = CARRYOVER_MONTHS,
) -> dict[str, int]:
    """What each committed category still holds from months already paid for.

    A bill budgeted a twelfth at a time is money taken out of free money every
    month. When the yearly invoice finally lands, charging it against that one
    month's budget alone would bill the user twice for the same money -- once
    while they were setting it aside, and again when they spent it. Unspent
    budget therefore stays available to its own category.

    Overspending is not carried the other way. A month that ran over was
    already charged to free money then, so it never becomes a debt the
    following month has to clear as well.
    """

    if not committed_ids or months <= 0:
        return {}
    keys = months_before(start, months)
    window = set(keys)
    spent: dict[str, dict[str, int]] = {}
    for transaction in transactions:
        if transaction.date >= start or not _is_budget_spending(transaction):
            continue
        category_id = transaction.category_id or ""
        if category_id not in committed_ids:
            continue
        key = month_key(transaction.date)
        if key not in window:
            continue
        spent.setdefault(key, {})
        spent[key][category_id] = spent[key].get(category_id, 0) - transaction.amount_cents

    carried: dict[str, int] = {}
    for key in keys:
        budgets = budgeted_history.get(key, {})
        month_spend = spent.get(key, {})
        for category_id in committed_ids:
            available = carried.get(category_id, 0) + max(0, budgets.get(category_id, 0))
            carried[category_id] = max(0, available - month_spend.get(category_id, 0))
    return {key: value for key, value in carried.items() if value}


def _is_budget_spending(transaction: TransactionInfo) -> bool:
    """Only on-budget, non-transfer outflow competes for free money."""
    return not (
        transaction.off_budget or transaction.is_transfer or transaction.is_starting_balance
    )


def average_monthly_income(
    history: Sequence[tuple[datetime.date, int]], *, months: int, before: datetime.date
) -> int:
    """Trailing average income over whole months, ignoring the current one.

    The current month is excluded on purpose: half a month of pay would drag the
    average down and make free money look smaller than it is.
    """

    if months <= 0:
        return 0
    start_of_month = before.replace(day=1)
    totals: dict[tuple[int, int], int] = {}
    for month_date, cents in history:
        if month_date >= start_of_month:
            continue
        totals[(month_date.year, month_date.month)] = (
            totals.get((month_date.year, month_date.month), 0) + cents
        )
    if not totals:
        return 0
    recent = [totals[key] for key in sorted(totals, reverse=True)[:months]]
    return round(sum(recent) / len(recent))


def build_budget_report(
    *,
    today: datetime.date,
    categories: Sequence[CategoryInfo],
    budgeted: dict[str, int],
    transactions: Sequence[TransactionInfo],
    income_history: Sequence[tuple[datetime.date, int]] = (),
    budgeted_history: dict[str, dict[str, int]] | None = None,
    committed_groups: Sequence[str] = (),
    income_override_cents: int = 0,
    income_lookback_months: int = 3,
    anticipated: Sequence[AnticipatedInfo] = (),
) -> BudgetReport:
    """Compute the month-to-date free-money report.

    `transactions` may span more than the reporting month; anything outside it
    is used only to work out what earlier months left available to a committed
    category, never as this month's spending.

    `anticipated` charges are money the card has authorised that the bank has
    not delivered. One carrying a provisional category is charged to that
    category exactly as a posted transaction would be, so a bill that is
    already budgeted draws on its own budget rather than on free money. One
    without a category is discretionary: nothing yet says where it will land,
    and the conservative reading of a charge is that it competes for free
    money. The day the bank posts the real row, it is that row that counts.
    """

    start, end = month_bounds(today)
    days_in_month = end.day
    day_of_month = min(today.day, days_in_month)
    days_remaining = max(1, days_in_month - day_of_month + 1)

    by_id = {category.id: category for category in categories}
    committed_set = {name.casefold() for name in committed_groups}

    def is_committed(category: CategoryInfo) -> bool:
        if category.is_income:
            return False
        if committed_set:
            return category.group_name.casefold() in committed_set
        # Default shape of the setup guide: anything you budgeted for this
        # month is a bill you have already promised to pay.
        return budgeted.get(category.id, 0) > 0

    committed_ids = {category.id for category in categories if is_committed(category)}
    income_ids = {category.id for category in categories if category.is_income}

    committed_cents = sum(
        max(0, budgeted.get(category_id, 0)) for category_id in committed_ids
    )

    income_received = 0
    committed_spent: dict[str, int] = {}
    # Charges and refunds stay apart until the whole month is in hand: a refund
    # can only be netted against its own category once that category's spending
    # for the month is known.
    discretionary_charges: dict[str, int] = {}
    discretionary_refunds: dict[str, int] = {}
    uncategorized_count = 0

    for transaction in transactions:
        if not (start <= transaction.date <= end):
            continue
        if transaction.is_starting_balance:
            continue
        category_id = transaction.category_id or ""
        # Deleting a category in Actual tombstones it rather than removing it,
        # and every transaction that referenced it keeps pointing at it. Money
        # sitting in a category that no longer exists is uncategorized in the
        # only sense that matters, and counting it as such is what puts it back
        # in front of you instead of silently swelling discretionary spending.
        if category_id and category_id not in by_id:
            category_id = ""
        if category_id and category_id in income_ids:
            # Income lands on-budget only; a transfer is not new money.
            if not transaction.off_budget and not transaction.is_transfer:
                income_received += max(0, transaction.amount_cents)
            continue
        if not _is_budget_spending(transaction):
            continue
        spend = transaction.spend_cents
        if spend <= 0:
            # Money coming back. A committed category keeps the simpler rule --
            # it draws on its own budget rather than on free money -- while
            # which month a discretionary refund belongs to is settled below.
            if category_id in committed_ids:
                committed_spent[category_id] = (
                    committed_spent.get(category_id, 0) + transaction.amount_cents * -1
                )
            else:
                discretionary_refunds[category_id] = (
                    discretionary_refunds.get(category_id, 0) + transaction.amount_cents
                )
            continue
        if category_id in committed_ids:
            committed_spent[category_id] = committed_spent.get(category_id, 0) + spend
        else:
            discretionary_charges[category_id] = (
                discretionary_charges.get(category_id, 0) + spend
            )
            if not category_id:
                uncategorized_count += 1

    # An anticipated charge is charged the way the bank's row will be once it
    # exists: to its provisional category, or to the uncategorized discretionary
    # lump when nothing yet says where it belongs. It is deliberately not an
    # uncategorized *transaction*: there is nothing in Actual to file.
    anticipated_open = [item for item in anticipated if not item.off_budget and item.spend_cents > 0]
    anticipated_cents = sum(item.spend_cents for item in anticipated_open)
    anticipated_committed = 0
    for item in anticipated_open:
        category_id = item.category_id if item.category_id in by_id else ""
        if category_id and category_id in income_ids:
            category_id = ""
        if category_id in committed_ids:
            committed_spent[category_id] = committed_spent.get(category_id, 0) + item.spend_cents
            anticipated_committed += item.spend_cents
        else:
            discretionary_charges[category_id] = (
                discretionary_charges.get(category_id, 0) + item.spend_cents
            )

    # A refund cancels its own category's spending for this month first. What it
    # cannot cancel there reverses a charge this month never counted, so it is
    # money handed back rather than spending pushed below zero. That is what
    # keeps "spent" a quantity of money that actually left, and keeps the
    # remaining share at or under 100% of what the month has to spend.
    discretionary_by_category: dict[str, int] = {}
    uncategorized_cents = 0
    returned_cents = 0
    # Charged categories keep the order they were seen in, so a tie between two
    # equal spends still breaks the way it did before refunds were held back.
    ordered = list(discretionary_charges) + [
        category_id
        for category_id in discretionary_refunds
        if category_id not in discretionary_charges
    ]
    for category_id in ordered:
        net = discretionary_charges.get(category_id, 0) - discretionary_refunds.get(
            category_id, 0
        )
        if net < 0:
            returned_cents -= net
            net = 0
        if category_id:
            discretionary_by_category[category_id] = net
        else:
            uncategorized_cents = net
    discretionary_total = sum(discretionary_by_category.values()) + uncategorized_cents

    committed_spent_total = sum(committed_spent.values())
    carried = committed_carryover(
        committed_ids=committed_ids,
        budgeted_history=budgeted_history or {},
        transactions=transactions,
        start=start,
    )
    # A category may draw on what earlier months set aside for it before any of
    # its spending counts as an overspend.
    committed_overspend = sum(
        max(
            0,
            spent - max(0, budgeted.get(category_id, 0)) - carried.get(category_id, 0),
        )
        for category_id, spent in committed_spent.items()
    )

    income_average = average_monthly_income(
        income_history, months=income_lookback_months, before=today
    )
    # Actual's tracking budget asks you to budget expected income into income
    # categories, and its own Projected Savings is built from that figure. When
    # it is there, it is a deliberate statement about the month, so it outranks
    # anything Clerk could infer -- and free money then matches the number
    # Actual itself shows. Envelope budgets have no such row and fall through.
    income_budgeted = sum(
        max(0, budgeted.get(category_id, 0)) for category_id in income_ids
    )
    if income_override_cents > 0:
        expected_income, basis = income_override_cents, "override"
    elif income_budgeted > 0:
        expected_income, basis = income_budgeted, "budgeted"
    elif income_received >= income_average and income_received > 0:
        expected_income, basis = income_received, "received"
    elif income_average > 0:
        expected_income, basis = income_average, "average"
    else:
        expected_income, basis = income_received, "unknown"

    # Free money is expected income minus commitments and nothing else, so it
    # stays the number Actual shows as Projected Savings and the setup guides
    # tell you to check it against. Returned money is added beside it, and it is
    # the sum the month is actually measured against.
    free_cents = expected_income - committed_cents
    available_cents = free_cents + returned_cents
    spent_cents = discretionary_total + committed_overspend
    remaining_cents = available_cents - spent_cents
    configured = expected_income > 0

    if available_cents > 0:
        remaining_percent = remaining_cents / available_cents
        spent_percent = spent_cents / available_cents
        pace_expected = round(available_cents * day_of_month / days_in_month)
    else:
        remaining_percent = 0.0
        spent_percent = 0.0
        pace_expected = 0

    projected_spend = round(spent_cents * days_in_month / day_of_month) if day_of_month else 0

    groups = {
        category.group_name
        for category in categories
        if category.group_name and not category.is_income
    }
    committed_group_names = sorted(
        {
            by_id[category_id].group_name
            for category_id in committed_ids
            if by_id[category_id].group_name
        }
    )

    top_categories = [
        {
            "category_id": category_id,
            "category_name": by_id[category_id].name if category_id in by_id else "Uncategorized",
            "group_name": by_id[category_id].group_name if category_id in by_id else "",
            "spent_cents": spent,
        }
        for category_id, spent in sorted(
            discretionary_by_category.items(), key=lambda item: item[1], reverse=True
        )[:8]
        if spent > 0
    ]

    committed_lines = sorted(
        (
            {
                "category_id": category_id,
                "category_name": by_id[category_id].name,
                "group_name": by_id[category_id].group_name,
                "budgeted_cents": max(0, budgeted.get(category_id, 0)),
                "carried_cents": carried.get(category_id, 0),
                "spent_cents": committed_spent.get(category_id, 0),
                "overspent_cents": max(
                    0,
                    committed_spent.get(category_id, 0)
                    - max(0, budgeted.get(category_id, 0))
                    - carried.get(category_id, 0),
                ),
            }
            for category_id in committed_ids
            if category_id in by_id
        ),
        key=lambda line: (-line["overspent_cents"], -line["budgeted_cents"]),
    )

    return BudgetReport(
        month=start.strftime("%Y-%m"),
        days_in_month=days_in_month,
        day_of_month=day_of_month,
        days_remaining=days_remaining,
        configured=configured,
        income_basis=basis,
        expected_income_cents=expected_income,
        income_received_cents=income_received,
        income_average_cents=income_average,
        income_budgeted_cents=income_budgeted,
        committed_cents=committed_cents,
        committed_spent_cents=committed_spent_total,
        committed_overspend_cents=committed_overspend,
        committed_carried_cents=sum(carried.values()),
        free_cents=free_cents,
        returned_cents=returned_cents,
        available_cents=available_cents,
        discretionary_spent_cents=discretionary_total,
        uncategorized_cents=uncategorized_cents,
        uncategorized_count=uncategorized_count,
        anticipated_cents=anticipated_cents,
        anticipated_count=len(anticipated_open),
        anticipated_committed_cents=anticipated_committed,
        spent_cents=spent_cents,
        remaining_cents=remaining_cents,
        remaining_percent=remaining_percent,
        spent_percent=spent_percent,
        daily_safe_to_spend_cents=max(0, remaining_cents) // days_remaining,
        pace_expected_cents=pace_expected,
        pace_delta_cents=pace_expected - spent_cents,
        on_track=spent_cents <= pace_expected or available_cents <= 0,
        projected_spend_cents=projected_spend,
        projected_remaining_cents=available_cents - projected_spend,
        committed_groups=committed_group_names,
        flexible_groups=sorted(groups - set(committed_group_names)),
        top_categories=top_categories,
        committed_lines=committed_lines,
    )


def format_money(cents: int, currency: str = "USD") -> str:
    """A compact, unambiguous money string for notifications and the UI."""
    symbol = {"USD": "$", "EUR": "€", "GBP": "£", "CAD": "$", "AUD": "$"}.get(
        currency.upper(), ""
    )
    sign = "-" if cents < 0 else ""
    whole, remainder = divmod(abs(cents), CENTS)
    body = f"{whole:,}.{remainder:02d}"
    return f"{sign}{symbol}{body}" if symbol else f"{sign}{body} {currency.upper()}"
