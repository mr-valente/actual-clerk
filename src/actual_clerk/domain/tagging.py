"""Cross-cutting labels that a category tree cannot express.

A category answers *which budget line* a transaction belongs to. A tag answers
*what kind of spending it was*: a subscription that renews whether or not you
use it, a bill that varies, a charge far outside a merchant's usual size, a
refund. Almost every tag here is derived from the transaction history rather
than from a model, because cadence and size are facts, not judgements.

Actual stores tags inline in a transaction's notes as `#tag`, and keeps colour
and description for them in its own tag table.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

# Actual reads a tag as `#name` terminated by a space or the end of the note.
_TAG_PATTERN = re.compile(r"#([^\s#]+)")
_VALID_TAG = re.compile(r"^[^\s#]+$")

UNUSUAL = "unusual"
REFUND = "refund"

# Colours come from Actual's own palette so Clerk's tags look native.
TAG_DEFINITIONS: dict[str, tuple[str, str]] = {
    UNUSUAL: ("#b88115", "Much larger than this merchant's usual charge"),
    REFUND: ("#147d64", "Money returned rather than spent"),
}
CLERK_TAG_COLOR = "#690cb0"
CLERK_TAG_DESCRIPTION = "Categorized or tagged by Actual Clerk"

# How many times a merchant's usual charge an amount must reach before it is
# worth pointing at, and how much history that judgement needs.
_UNUSUAL_MULTIPLE = 3.0
_UNUSUAL_MIN_HISTORY = 4


@dataclass(frozen=True)
class MerchantStats:
    """Amount history for one merchant, used to spot an outlier charge."""

    typical_cents: int
    sample_size: int

    @classmethod
    def from_amounts(cls, amounts: Sequence[int]) -> MerchantStats:
        outflow = [abs(amount) for amount in amounts if amount < 0]
        if not outflow:
            return cls(typical_cents=0, sample_size=0)
        return cls(typical_cents=round(statistics.median(outflow)), sample_size=len(outflow))


def parse_tags(notes: str | None) -> list[str]:
    """Tags already written into a note, in the order they appear."""
    if not notes:
        return []
    seen: dict[str, str] = {}
    for match in _TAG_PATTERN.finditer(notes):
        tag = match.group(1).rstrip(".,;:!?")
        if tag:
            seen.setdefault(tag.casefold(), tag)
    return list(seen.values())


def has_tag(notes: str | None, tag: str) -> bool:
    return tag.casefold() in {item.casefold() for item in parse_tags(notes)}


def normalize_tag(tag: str) -> str:
    tag = tag.strip().lstrip("#")
    return tag if _VALID_TAG.match(tag) else ""


def apply_tags(notes: str | None, tags: Sequence[str]) -> str:
    """Append missing tags to a note without disturbing what is already there.

    Idempotent by construction: running the tagger twice over the same
    transaction produces the same note, which matters because Clerk re-reads
    its own writes on the next sync.
    """

    existing = {item.casefold() for item in parse_tags(notes)}
    additions = []
    for tag in tags:
        clean = normalize_tag(tag)
        if clean and clean.casefold() not in existing:
            existing.add(clean.casefold())
            additions.append(f"#{clean}")
    if not additions:
        return notes or ""
    base = (notes or "").rstrip()
    return f"{base} {' '.join(additions)}".strip()


def remove_tag(notes: str | None, tag: str) -> str:
    """Strip one tag from a note, leaving the surrounding text intact."""
    clean = normalize_tag(tag)
    if not clean or not notes:
        return notes or ""
    pattern = re.compile(rf"(?:(?<=\s)|^)#{re.escape(clean)}(?=\s|$)", re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", pattern.sub("", notes)).strip()


def derive_tags(
    *,
    amount_cents: int,
    merchant_key: str,
    stats: MerchantStats | None = None,
    tag_anomalies: bool = True,
) -> list[str]:
    """Decide which descriptive tags a transaction has earned."""

    tags: list[str] = []
    if tag_anomalies:
        if amount_cents > 0:
            tags.append(REFUND)
        elif (
            stats is not None
            and stats.sample_size >= _UNUSUAL_MIN_HISTORY
            and stats.typical_cents > 0
            and abs(amount_cents) >= stats.typical_cents * _UNUSUAL_MULTIPLE
        ):
            tags.append(UNUSUAL)
    return tags


def tag_catalog(clerk_tag: str = "") -> list[dict[str, Any]]:
    """Every tag Clerk may write, with the colour and description to register."""
    catalog = [
        {"tag": name, "color": color, "description": description}
        for name, (color, description) in TAG_DEFINITIONS.items()
    ]
    clean = normalize_tag(clerk_tag)
    if clean:
        catalog.insert(
            0,
            {"tag": clean, "color": CLERK_TAG_COLOR, "description": CLERK_TAG_DESCRIPTION},
        )
    return catalog
