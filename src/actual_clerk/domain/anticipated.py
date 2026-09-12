"""Anticipated charges: card notifications read from a phone before the bank posts.

A card's own phone app announces a purchase the moment it is authorised, days
before the bank's feed carries it and, for some issuers, without ever
exposing it as a pending transaction. The companion app forwards those
notifications to Clerk, which turns each into an *anticipated charge*: money
that is already spent as far as the budget is concerned, but that must never
be written into Actual. The bank feed remains the record. When the matching
transaction finally arrives through the ordinary import, the anticipation is
settled against it and drops out of the report.

Everything here is pure: the parser reads a notification's words, and the
matcher pairs open anticipations with transactions the snapshot already
holds. Nothing knows about the database or the network.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from actual_clerk.domain.merchants import keys_related, normalize_merchant

KIND_CHARGE = "charge"
KIND_CREDIT = "credit"
KIND_DECLINED = "declined"
KIND_UNKNOWN = "unknown"

# How many days *before* the notification a matching transaction may be dated.
# A card authorises on the purchase date, but a time-zone boundary or a
# merchant that batches overnight can put the bank's date one day earlier.
LEAD_DAYS = 1

_MONEY = re.compile(
    r"(?<![\w.])(?:\$|USD\s?)\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?(?!\d)",
)
_DECLINED = re.compile(r"\b(declined|denied|not approved|was blocked)\b")
_CREDIT = re.compile(
    r"\b(credit(?:ed)?|refund(?:ed)?|returned|reversal|reversed|"
    r"payment\b.{0,80}?\b(?:received|posted|processed|scheduled|applied)|"
    r"thank you for your payment|deposit(?:ed)?|cash ?back)\b"
)
# Preposition-led merchant phrases. Each ends at a connective that introduces
# the card, the time, or the outcome rather than more of the merchant's name.
# A sentence-ending mark only ends the merchant when a space or the end of the
# text follows it: "AMAZON.COM" keeps its dot, "AMAZON. Thank you" does not.
_MERCHANT_END = (
    r"(?=\s+(?:on|with|using|for|was|has|is|ending|from|in the amount|to your|"
    r"at \d)\b|\s+\(|[.!,;](?=\s|$)|\s*$)"
)
# "at 3:45 PM" names a time, not a shop; refusing it here lets the search move
# on to the next "at" in the same sentence.
_NOT_A_TIME = r"(?!\d{1,2}(?::\d{2})?\s*(?:am|pm)\b)"
_MERCHANT_PATTERNS = (
    re.compile(r"\bat\s+" + _NOT_A_TIME + r"(.+?)" + _MERCHANT_END, re.IGNORECASE),
    re.compile(r"\bfrom\s+(.+?)" + _MERCHANT_END, re.IGNORECASE),
)
_TIME_LIKE = re.compile(r"^\d{1,2}(?::\d{2})?\s*(?:am|pm)?\.?$", re.IGNORECASE)
_TRAILING_NOISE = re.compile(r"[\s\-–—:.,;!]+$")


@dataclass(frozen=True)
class ParsedNotification:
    kind: str
    amount_cents: int  # in Actual's sign: negative for money leaving the account
    merchant: str
    merchant_key: str

    @property
    def counts(self) -> bool:
        """Only a recognised charge anticipates spending."""
        return self.kind == KIND_CHARGE and self.amount_cents < 0


def _cents(match: re.Match[str]) -> int:
    whole = int(match.group(1).replace(",", ""))
    fraction = (match.group(2) or "0").ljust(2, "0")
    return whole * 100 + int(fraction)


def _clean_merchant(value: str) -> str:
    value = " ".join(value.replace("“", '"').replace("”", '"').split())
    value = value.strip("\"'` ")
    value = _TRAILING_NOISE.sub("", value)
    return value[:80]


def extract_merchant(text: str) -> str:
    """The merchant named in a notification, or an empty string."""

    for pattern in _MERCHANT_PATTERNS:
        for match in pattern.finditer(text):
            candidate = _clean_merchant(match.group(1))
            if not candidate or _TIME_LIKE.match(candidate):
                continue
            # "at your local branch" and the like: a phrase that is all common
            # words has no merchant in it.
            if not any(character.isalnum() for character in candidate):
                continue
            return candidate
    return ""


def parse_notification(title: str, text: str) -> ParsedNotification:
    """Read a card app's notification into an amount, a direction, and a merchant.

    The wording is the issuer's, so this is deliberately generous about form
    and strict about substance: without an amount nothing is anticipated, and
    a declined authorisation is recorded but never counted.
    """

    combined = " ".join(part for part in (title, text) if part).strip()
    lowered = combined.casefold()
    money = _MONEY.search(combined)
    merchant = extract_merchant(text) or extract_merchant(title)
    key = normalize_merchant(merchant)
    if money is None:
        return ParsedNotification(KIND_UNKNOWN, 0, merchant, key)
    cents = _cents(money)
    if _DECLINED.search(lowered):
        return ParsedNotification(KIND_DECLINED, -cents, merchant, key)
    if _CREDIT.search(lowered):
        return ParsedNotification(KIND_CREDIT, cents, merchant, key)
    return ParsedNotification(KIND_CHARGE, -cents, merchant, key)


# --------------------------------------------------------------- matching


@dataclass(frozen=True)
class AnticipatedCharge:
    id: str
    account_id: str
    amount_cents: int
    noticed_date: datetime.date
    merchant_key: str = ""


@dataclass(frozen=True)
class CandidateTransaction:
    id: str
    account_id: str
    amount_cents: int
    date: datetime.date
    merchant_key: str = ""
    payee_name: str = ""
    imported: bool = True


@dataclass(frozen=True)
class ChargeMatch:
    charge_id: str
    transaction_id: str
    reason: str
    payee_name: str
    date: datetime.date


def match_charges(
    charges: Sequence[AnticipatedCharge],
    transactions: Iterable[CandidateTransaction],
    *,
    window_days: int,
    used_transaction_ids: Iterable[str] = (),
) -> list[ChargeMatch]:
    """Pair each open anticipation with the transaction that settles it.

    A candidate must sit on the same account, carry exactly the same amount,
    and be dated within the window around the notification. Among candidates
    the one whose merchant reads as the same shop wins, then the nearest date,
    then a bank-imported row over a hand-entered one. Each transaction settles
    at most one anticipation, and earlier notifications choose first, so two
    identical coffees on one morning settle one each.
    """

    used = set(used_transaction_ids)
    by_account: dict[tuple[str, int], list[CandidateTransaction]] = {}
    for transaction in transactions:
        by_account.setdefault((transaction.account_id, transaction.amount_cents), []).append(
            transaction
        )
    matches: list[ChargeMatch] = []
    for charge in sorted(charges, key=lambda item: item.noticed_date):
        earliest = charge.noticed_date - datetime.timedelta(days=LEAD_DAYS)
        latest = charge.noticed_date + datetime.timedelta(days=window_days)
        candidates = [
            transaction
            for transaction in by_account.get((charge.account_id, charge.amount_cents), [])
            if transaction.id not in used and earliest <= transaction.date <= latest
        ]
        if not candidates:
            continue
        best = min(candidates, key=lambda transaction, charge=charge: _rank(charge, transaction))
        related = _rank(charge, best)[0] == 0
        used.add(best.id)
        matches.append(
            ChargeMatch(
                charge_id=charge.id,
                transaction_id=best.id,
                reason="amount and merchant" if related else "amount and date",
                payee_name=best.payee_name,
                date=best.date,
            )
        )
    return matches


def _rank(charge: AnticipatedCharge, transaction: CandidateTransaction) -> tuple[int, int, int]:
    """Lower sorts first: same merchant, then nearest date, then a bank-imported row."""
    related = bool(charge.merchant_key) and keys_related(charge.merchant_key, transaction.merchant_key)
    return (
        0 if related else 1,
        abs((transaction.date - charge.noticed_date).days),
        0 if transaction.imported else 1,
    )


def expired_charges(
    charges: Sequence[AnticipatedCharge], *, today: datetime.date, expire_days: int
) -> list[str]:
    """Anticipations old enough that the bank is evidently never going to post them."""

    return [
        charge.id
        for charge in charges
        if (today - charge.noticed_date).days > expire_days
    ]
