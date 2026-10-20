"""A refund files where the charge it gives back was filed.

The budget nets a refund against its own category's spending for the month,
so a charge and its reversal cancel -- but only when they share a category.
A refund filed anywhere else, or left uncategorized, leaves the charge's
category looking overspent and shows the refund as money returned from an
earlier month. Which category the refund belongs to is not a question about
the merchant at all: it is the category of the purchase it undoes.

A refund names its purchase when it comes back on the same account, from
the same shop, for exactly the amount of a filed charge. That covers a
merchant's mistake reversed the next day and a full return weeks later
alike. A partial refund names no charge and is filed like anything else.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping, Sequence
from typing import Any

from actual_clerk.domain.intelligence import canonical_key
from actual_clerk.domain.merchants import keys_related, keys_share_words

# Long enough for a return posted after a 30-day return window; a refund
# older than this is rarely for the full amount of anything.
REFUND_WINDOW_DAYS = 60


def _same_shop(left: str, right: str, aliases: Mapping[str, str]) -> bool:
    # The exact amount and the account already have to agree, so names with
    # the same words are evidence enough: a refund is often decorated
    # differently from its charge.
    left, right = canonical_key(left, aliases), canonical_key(right, aliases)
    return keys_related(left, right) or keys_share_words(left, right)


def refunded_charge(
    refund: Mapping[str, Any],
    transactions: Sequence[Mapping[str, Any]],
    *,
    aliases: Mapping[str, str] | None = None,
    window_days: int = REFUND_WINDOW_DAYS,
) -> Mapping[str, Any] | None:
    """The filed charge this incoming row gives back, when there is one answer.

    Several identical charges filed the same way are one answer and the
    latest stands for them; filed different ways, the refund could be for
    either, and nothing is decided here.
    """

    amount = int(refund.get("amount_cents") or 0)
    key = str(refund.get("merchant_key") or "")
    if amount <= 0 or not key:
        return None
    aliases = aliases or {}
    date: datetime.date = refund["date"]
    earliest = date - datetime.timedelta(days=window_days)
    candidates = [
        item
        for item in transactions
        if item.get("id") != refund.get("id")
        and item.get("account_id") == refund.get("account_id")
        and item.get("amount_cents") == -amount
        and item.get("category_id")
        and not item.get("is_transfer")
        and not item.get("off_budget")
        and not item.get("is_starting_balance")
        and earliest <= item["date"] <= date
        and _same_shop(key, str(item.get("merchant_key") or ""), aliases)
    ]
    if len({item["category_id"] for item in candidates}) != 1:
        return None
    return max(candidates, key=lambda item: (item["date"], str(item.get("id") or "")))
