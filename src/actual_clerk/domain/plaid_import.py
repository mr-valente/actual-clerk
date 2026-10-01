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

A row is dated on the day the purchase was made, not the day the bank posted
it: Plaid's ``authorized_date`` when it has one, its ``date`` otherwise. The
two differ by a few days for most card charges, and across a month's end
that difference decides which month's budget the money comes out of. The
posting ``date`` is still what the cutover and adoption windows measure,
since that is the date the previous provider's rows carry.

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


def transaction_date(transaction: dict[str, Any]) -> datetime.date | None:
    """The day the purchase was made, as far as Plaid knows it.

    Plaid's ``date`` is the posting date once a transaction posts, and its
    ``authorized_date`` is the day the card was used. The latter is the one a
    person means by "when I bought it"; not every bank supplies it. Nothing
    happens after it posts, so an authorisation date later than the posting
    date (a dividend credited on the 30th that Plaid "authorises" on the 1st)
    is a bank quirk and the posting date stands.
    """
    authorized = _date(transaction.get("authorized_date"))
    posted = _date(transaction.get("date"))
    if authorized is None or posted is None:
        return authorized or posted
    return min(authorized, posted)


def looks_like_plaid_id(value: str) -> bool:
    return bool(value) and bool(PLAID_ID.match(value))


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


# Counterparty kinds that can stand in for a payee, best first. A payment
# terminal (Square, Toast) names the rail the money went through, not who it
# went to.
_COUNTERPARTY_RANK = {
    "merchant": 0,
    "marketplace": 1,
    "income_source": 2,
    "financial_institution": 3,
    "payment_app": 4,
}

# Bank lines whose wording carries no payee at all, and the name SimpleFIN
# gave them. Matched on the first word.
_KNOWN_LINES = (("dividend", "Dividend"),)

# ACH detail fields banks append after the originator's name:
# `OAK KNOLL SCHOOL TYPE: PAYROLL CO: OAK KNOLL SCHOOL`.
_FIELD_LABEL = re.compile(
    r"\s+(?:type|co|name|id|ind ?id|indn|ref|des|desc|entry|trace|sec)\s*:", re.IGNORECASE
)

_WORD = re.compile(r"[A-Za-z][A-Za-z']*")


def short_payee(description: str) -> str:
    """A payee-sized name from a bank line that Plaid could not match to a merchant.

    The full line still goes into the notes, so this keeps only the part that
    names someone: the originator before any ACH detail fields, and the words
    before the first one carrying a number (a store id, a rate, a date).
    """

    text = _clean(description)
    if not text:
        return ""
    known = _known_line(text)
    if known:
        return known
    head = _FIELD_LABEL.split(text, maxsplit=1)[0].split(":", 1)[0]
    tokens = head.split()
    if not tokens:
        return text
    kept = tokens[:1]
    for token in tokens[1:]:
        if any(character.isdigit() for character in token):
            break
        kept.append(token)
    short = " ".join(kept).strip(" -*#/.,")
    if len(short) < 3:
        return text
    return _WORD.sub(lambda word: word.group(0).capitalize(), short) if short.isupper() else short


def _known_line(text: str) -> str:
    first = text.split()[0].casefold() if text else ""
    return next((label for prefix, label in _KNOWN_LINES if first.startswith(prefix)), "")


def _counterparty(transaction: dict[str, Any]) -> str:
    best: tuple[int, str] | None = None
    for entry in transaction.get("counterparties") or []:
        if not isinstance(entry, dict):
            continue
        name = _clean(entry.get("name"))
        rank = _COUNTERPARTY_RANK.get(str(entry.get("type") or ""))
        if not name or rank is None:
            continue
        if str(entry.get("confidence_level") or "").upper() == "LOW":
            continue
        if best is None or rank < best[0]:
            best = (rank, name)
    return best[1] if best else ""


def payee_for(transaction: dict[str, Any]) -> str:
    """Plaid's merchant when it found one, otherwise who the bank line names.

    Plaid leaves ``merchant_name`` empty for anything that is not a shop:
    dividends, payroll, transfers, fees. Its ``name`` is then the whole bank
    line, which reads as a note, not a payee.
    """

    merchant = _clean(transaction.get("merchant_name"))
    if merchant:
        return merchant
    description = _clean(transaction.get("name")) or _clean(transaction.get("original_description"))
    return (
        _known_line(description)
        or _counterparty(transaction)
        or short_payee(description)
        or "Unknown"
    )


def convert(transaction: dict[str, Any]) -> dict[str, Any]:
    """One Plaid transaction as an Actual import row."""
    date = transaction_date(transaction)
    if date is None:
        raise PlaidTransactionError("A Plaid transaction without a date cannot be imported")
    name = _clean(transaction.get("name"))
    merchant = _clean(transaction.get("merchant_name"))
    original = _clean(transaction.get("original_description"))
    return {
        "date": date,
        "amount_cents": plaid_amount_to_cents(transaction.get("amount")),
        "payee_name": payee_for(transaction),
        # What the bank actually printed: Clerk's merchant memory keys on this.
        "imported_payee": original or name or merchant,
        # The bank line in full, where SimpleFIN put it. Actual keeps a
        # matched row's own notes, so a phone charge's are not overwritten.
        "notes": original or name or None,
        "imported_id": str(transaction.get("transaction_id") or ""),
        "cleared": not bool(transaction.get("pending")),
    }


_LETTERS_AND_DIGITS = re.compile(r"[^0-9a-z]+")
_TAG = re.compile(r"#[^\s#]+")


def _same_line(left: str, right: str) -> bool:
    left = _LETTERS_AND_DIGITS.sub("", left.casefold())
    return bool(left) and left == _LETTERS_AND_DIGITS.sub("", right.casefold())


def retidy(row: dict[str, Any]) -> dict[str, str]:
    """What a row imported before ``payee_for`` existed needs to read like a new one.

    Those imports took Plaid's whole bank line as the payee whenever it found
    no merchant, and left the notes empty. ``row`` is one of the gateway's
    account rows; the answer holds ``payee_name`` and/or ``notes``, or is
    empty when the row is fine as it is. A payee the user (or an Actual rule)
    renamed no longer matches the bank line and is left alone, and so are
    notes that hold anything besides tags.
    """

    if row.get("is_child") or row.get("is_starting_balance"):
        return {}
    if not looks_like_plaid_id(str(row.get("imported_id") or "")):
        return {}
    imported = _clean(row.get("imported_description"))
    if not imported:
        return {}
    changes: dict[str, str] = {}
    payee = _clean(row.get("payee_name"))
    if not row.get("is_transfer") and _same_line(payee, imported):
        short = short_payee(payee)
        if short and short != payee:
            changes["payee_name"] = short
    notes = str(row.get("notes") or "").strip()
    if not _TAG.sub("", notes).strip():
        changes["notes"] = f"{imported} {notes}".strip()
    return changes


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
    pinned_dates: set[str] | frozenset[str] = frozenset(),
) -> AccountPlan:
    """Decide imports, adoptions, and deletions for one account.

    ``existing`` is the account's current Actual rows (id, date, amount_cents,
    imported_id, cleared, reconciled, is_parent, is_child, is_starting_balance); the
    caller reads them from a window around the cutover and forward.
    ``pinned_dates`` holds the ids of rows whose date came from somewhere
    better than the bank -- the phone saw the purchase happen -- and is
    never moved by a later change from Plaid.
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

    # One read can span several of Plaid's updates, so a transaction added
    # early in it may already be removed later on. Removed is the final word.
    withdrawn = {str(entry.get("transaction_id") or "") for entry in removed}
    incoming: dict[str, dict[str, Any]] = {}
    for transaction in [*added, *modified]:
        transaction_id = str(transaction.get("transaction_id") or "")
        if (
            transaction_id
            and transaction_id not in withdrawn
            and transaction.get("account_id") == external_account_id
        ):
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
        settled = _settlement(
            row, transaction, transaction_id, keep_date=row["id"] in pinned_dates
        )
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
    row: dict[str, Any],
    transaction: dict[str, Any],
    transaction_id: str,
    *,
    keep_date: bool = False,
) -> dict[str, Any]:
    """The fields on an existing row that the Plaid transaction changes.

    The date is one of them when the bank corrects the day of the purchase;
    a pending row and its posted successor share an authorisation date, so
    posting alone no longer moves a row into the next month. A reconciled
    row only ever takes the id, and a row the phone dated keeps its date.
    """
    fields: dict[str, Any] = {}
    if (row.get("imported_id") or "") != transaction_id:
        fields["imported_id"] = transaction_id
    if row.get("reconciled"):
        # The user has reconciled this row against a statement. It takes the
        # Plaid id so later changes find it, but its money stays as agreed.
        return fields
    # Clearing only ever goes one way, as in Actual's own import: a row the
    # user cleared by hand is not un-cleared because the bank still lists
    # the charge as pending.
    if not transaction.get("pending") and not row.get("cleared"):
        fields["cleared"] = True
    amount = plaid_amount_to_cents(transaction.get("amount"))
    # A split's children must add up to its parent, so a parent's amount is
    # the user's to change.
    if row.get("amount_cents") != amount and not row.get("is_parent"):
        fields["amount_cents"] = amount
    date = transaction_date(transaction)
    if date is not None and not keep_date and row.get("date") != date:
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
