"""Turn one account's Plaid change stream into Actual operations.

Plaid's ``/transactions/sync`` hands back ``added``, ``modified``, and
``removed`` for an Item. Actual's import can take the additions as they are
-- it deduplicates by ``imported_id``, fuzzy-matches manual rows, and runs the
user's rules -- but three cases need Clerk to decide first, because Actual's
public import will not:

- **Pending to posted.** Plaid retires the pending transaction (``removed``)
  and issues the posted one under a new id, naming the old one in
  ``pending_transaction_id``. Actual's import would not match the two (both
  carry ids), so the existing row is *adopted*: its ``imported_id`` becomes
  the posted id, it clears, and its amount settles.
- **The cutover boundary.** Rows that another provider imported around the
  cutover date carry that provider's ids. A Plaid transaction with the same
  amount within a few days adopts such a row rather than duplicating it.
- **Amount or date changes.** Actual's import never rewrites an existing
  row's amount or date, so a ``modified`` transaction that still matches by
  id is settled directly.

Everything here is pure: dictionaries in, a plan out, nothing touched.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

# Plaid transaction ids are opaque base-62 strings of this shape. A row whose
# imported_id looks like this was written by Plaid (through Clerk) and is
# never adopted by a *different* Plaid transaction.
PLAID_ID = re.compile(r"^[A-Za-z0-9]{20,64}$")
CENTS = Decimal(100)


class PlaidTransactionError(ValueError):
    pass


def plaid_amount_to_cents(value: Any) -> int:
    """Plaid: positive is money out. Actual: negative is money out."""
    try:
        cents = int((Decimal(str(value)) * CENTS).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise PlaidTransactionError(f"Unreadable Plaid amount: {value!r}") from exc
    return -cents


def _date(value: Any) -> datetime.date | None:
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def looks_like_plaid_id(value: str) -> bool:
    return bool(value) and bool(PLAID_ID.match(value))


def convert(transaction: dict[str, Any]) -> dict[str, Any]:
    """One Plaid transaction as an Actual import row."""
    date = _date(transaction.get("date"))
    if date is None:
        raise PlaidTransactionError("A Plaid transaction without a date cannot be imported")
    name = str(transaction.get("name") or "").strip()
    merchant = str(transaction.get("merchant_name") or "").strip()
    original = str(transaction.get("original_description") or "").strip()
    return {
        "date": date,
        "amount_cents": plaid_amount_to_cents(transaction.get("amount")),
        "payee_name": merchant or name or "Unknown",
        # What the bank actually printed: Clerk's merchant memory keys on this.
        "imported_payee": original or name or merchant,
        "imported_id": str(transaction.get("transaction_id") or ""),
        "cleared": not bool(transaction.get("pending")),
    }


@dataclass
class AccountPlan:
    """What one sync should do to one Actual account."""

    actual_account_id: str
    external_account_id: str
    imports: list[dict[str, Any]] = field(default_factory=list)
    # Existing rows given a new id / settled: {transaction_id, imported_id,
    # cleared, amount_cents, date, previous_imported_id, reason}
    adoptions: list[dict[str, Any]] = field(default_factory=list)
    deletions: list[dict[str, Any]] = field(default_factory=list)
    # Removed by the bank but cleared (or reconciled) in Actual: left alone.
    kept: list[dict[str, Any]] = field(default_factory=list)
    skipped_before_cutover: int = 0
    unknown_removed: int = 0

    @property
    def empty(self) -> bool:
        return not (self.imports or self.adoptions or self.deletions)

    def summary(self) -> dict[str, int]:
        return {
            "imports": len(self.imports),
            "adoptions": len(self.adoptions),
            "deletions": len(self.deletions),
            "kept": len(self.kept),
            "skipped_before_cutover": self.skipped_before_cutover,
            "unknown_removed": self.unknown_removed,
        }


def plan_account(
    *,
    actual_account_id: str,
    external_account_id: str,
    cutover: datetime.date,
    added: list[dict[str, Any]],
    modified: list[dict[str, Any]],
    removed: list[dict[str, Any]],
    existing: list[dict[str, Any]],
    adopt_window_days: int = 14,
) -> AccountPlan:
    """Decide imports, adoptions, and deletions for one account.

    ``existing`` is the account's current Actual rows (id, date, amount_cents,
    imported_id, cleared, reconciled, is_child, is_starting_balance); the
    caller reads them from a window around the cutover and forward.
    """

    plan = AccountPlan(actual_account_id, external_account_id)
    rows = [
        row for row in existing
        if not row.get("is_child") and not row.get("is_starting_balance")
    ]
    by_imported_id = {row["imported_id"]: row for row in rows if row.get("imported_id")}
    claimed: set[str] = set()
    # Rows carrying a foreign provider's id near the cutover are the only
    # candidates for amount-and-date adoption. A manual row (no id) is left to
    # Actual's own fuzzy match, and a Plaid row is never re-adopted.
    window = datetime.timedelta(days=adopt_window_days)
    foreign = [
        row
        for row in rows
        if row.get("imported_id")
        and not looks_like_plaid_id(row["imported_id"])
        and row["date"] is not None
        and cutover - window <= row["date"] <= cutover + window
    ]

    incoming: dict[str, dict[str, Any]] = {}
    for transaction in [*added, *modified]:
        transaction_id = str(transaction.get("transaction_id") or "")
        if transaction_id and transaction.get("account_id") == external_account_id:
            incoming[transaction_id] = transaction

    swapped_pending: set[str] = set()
    for transaction_id, transaction in incoming.items():
        date = _date(transaction.get("date"))
        if date is None or date < cutover:
            plan.skipped_before_cutover += 1
            continue
        row = by_imported_id.get(transaction_id)
        reason = "settled"
        if row is None:
            pending_id = str(transaction.get("pending_transaction_id") or "")
            candidate = by_imported_id.get(pending_id) if pending_id else None
            if candidate is not None and candidate["id"] not in claimed:
                row = candidate
                reason = "posted"
                swapped_pending.add(pending_id)
        if row is None:
            row = _adopt_foreign(transaction, date, foreign, claimed, window)
            reason = "cutover"
        if row is None:
            plan.imports.append(convert(transaction))
            continue
        claimed.add(row["id"])
        settled = _settlement(row, transaction, transaction_id, date)
        if settled or reason != "settled":
            plan.adoptions.append(
                {
                    "transaction_id": row["id"],
                    "previous_imported_id": row.get("imported_id") or "",
                    "reason": reason,
                    **settled,
                }
            )

    for entry in removed:
        transaction_id = str(entry.get("transaction_id") or "")
        if not transaction_id or entry.get("account_id") != external_account_id:
            continue
        if transaction_id in swapped_pending:
            continue
        row = by_imported_id.get(transaction_id)
        if row is None or row["id"] in claimed:
            plan.unknown_removed += 1 if row is None else 0
            continue
        claimed.add(row["id"])
        if row.get("cleared") or row.get("reconciled"):
            plan.kept.append({"transaction_id": row["id"], "imported_id": transaction_id,
                              "amount_cents": row.get("amount_cents", 0), "date": row.get("date")})
        else:
            plan.deletions.append({"transaction_id": row["id"], "imported_id": transaction_id,
                                   "amount_cents": row.get("amount_cents", 0), "date": row.get("date")})
    return plan


def _adopt_foreign(
    transaction: dict[str, Any],
    date: datetime.date,
    foreign: list[dict[str, Any]],
    claimed: set[str],
    window: datetime.timedelta,
) -> dict[str, Any] | None:
    amount = plaid_amount_to_cents(transaction.get("amount"))
    best: dict[str, Any] | None = None
    best_distance: datetime.timedelta | None = None
    for row in foreign:
        if row["id"] in claimed or row.get("amount_cents") != amount:
            continue
        distance = abs(row["date"] - date)
        if distance > window:
            continue
        if best is None or distance < best_distance:
            best, best_distance = row, distance
    return best


def _settlement(
    row: dict[str, Any], transaction: dict[str, Any], transaction_id: str, date: datetime.date
) -> dict[str, Any]:
    """The fields on an existing row that the Plaid transaction changes.

    The date is one of them: a posted transaction usually lands a day or
    more after the pending one Actual already holds, and Actual's import
    would never move it.
    """
    fields: dict[str, Any] = {}
    if (row.get("imported_id") or "") != transaction_id:
        fields["imported_id"] = transaction_id
    cleared = not bool(transaction.get("pending"))
    if bool(row.get("cleared")) != cleared:
        fields["cleared"] = cleared
    amount = plaid_amount_to_cents(transaction.get("amount"))
    if row.get("amount_cents") != amount:
        fields["amount_cents"] = amount
    if row.get("date") != date:
        fields["date"] = date
    return fields


def starting_balance_cents(
    *, current_balance_cents: int | None, imports: list[dict[str, Any]]
) -> int | None:
    """The opening balance that makes imported history add up to the bank's balance.

    Only cleared imports count: Plaid's current balance is what the bank has
    posted, and a pending charge has not moved it yet.
    """

    if current_balance_cents is None:
        return None
    posted = sum(item["amount_cents"] for item in imports if item.get("cleared", True))
    return current_balance_cents - posted
