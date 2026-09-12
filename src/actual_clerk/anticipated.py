"""Keep anticipated charges in step with what Actual actually holds.

Runs every time the overview is rebuilt -- after a sync, a filing run, a
connection check, the morning report, or a manual refresh -- so an
anticipation is settled the moment the bank's row is read, whichever job
happened to read it. Nothing here writes to Actual; the only state that
changes is Clerk's own ledger of what the phone has seen.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
from typing import Any

from actual_clerk.categorize import build_memory
from actual_clerk.config import Settings
from actual_clerk.db import Database
from actual_clerk.domain.anticipated import (
    KIND_CHARGE,
    KIND_CREDIT,
    AnticipatedCharge,
    CandidateTransaction,
    expired_charges,
    match_charges,
    parse_notification,
)
from actual_clerk.domain.merchants import normalize_merchant

log = logging.getLogger(__name__)

# Where a provisional category came from. A taught one is the user's word and
# is never overwritten by memory; a memory one is re-derived on every pass so
# it follows the evidence.
SOURCE_MEMORY = "memory"
SOURCE_TAUGHT = "taught"

# Statuses an anticipation can be in. Only `open` charges count in the budget.
OPEN = "open"
MATCHED = "matched"
EXPIRED = "expired"
DISMISSED = "dismissed"
# A notification Clerk could read but never anticipates: a declined
# authorisation, or one with no amount in it. Kept so the phone log explains
# itself, closed from the start.
IGNORED = "ignored"


def notification_key(package_name: str, posted_at_ms: int, title: str, text: str) -> str:
    """A stable identity for one notification, so a re-delivery is not a second charge."""

    digest = hashlib.sha256(f"{title}\n{text}".encode()).hexdigest()[:16]
    return f"{package_name}:{posted_at_ms}:{digest}"


def record_notification(
    database: Database,
    settings: Settings,
    *,
    source: dict[str, Any],
    posted_at_ms: int,
    title: str,
    text: str,
    key: str = "",
) -> tuple[dict[str, Any], bool]:
    """Turn one forwarded notification into an anticipated charge, once."""

    parsed = parse_notification(title, text)
    noticed_at = posted_at_ms / 1000 if posted_at_ms > 0 else datetime.datetime.now(datetime.UTC).timestamp()
    noticed_date = datetime.datetime.fromtimestamp(noticed_at, settings.zone).date().isoformat()
    status = OPEN if parsed.kind in (KIND_CHARGE, KIND_CREDIT) else IGNORED
    row, created = database.add_anticipated_charge(
        {
            "source_id": source["id"],
            "actual_account_id": source.get("actual_account_id") or "",
            "notification_key": key or notification_key(source["package_name"], posted_at_ms, title, text),
            "kind": parsed.kind,
            "amount_cents": parsed.amount_cents,
            "merchant": parsed.merchant,
            "merchant_key": parsed.merchant_key,
            "title": title,
            "text": text,
            "noticed_at": noticed_at,
            "noticed_date": noticed_date,
            "status": status,
        }
    )
    return row, created


def reconcile(
    database: Database,
    snapshot: dict[str, Any],
    settings: Settings,
    *,
    today: datetime.date,
) -> dict[str, Any]:
    """Settle open anticipations against the snapshot and retire stale ones.

    Returns the anticipations still open afterwards, plus counts of what
    changed, so the caller can build the report from the same list.
    """

    open_rows = database.list_anticipated_charges(status=OPEN, limit=1000)
    summary: dict[str, Any] = {"open": [], "matched": 0, "expired": 0, "categorized": 0}
    if not open_rows or not settings.anticipated_enabled:
        summary["open"] = [public_charge(row) for row in open_rows] if settings.anticipated_enabled else []
        return summary

    aliases = database.alias_map()
    charges = [_as_charge(row) for row in open_rows]
    candidates = [
        CandidateTransaction(
            id=str(item.get("id") or ""),
            account_id=str(item.get("account_id") or ""),
            amount_cents=int(item.get("amount_cents", 0)),
            date=item["date"],
            merchant_key=str(item.get("merchant_key") or ""),
            payee_name=str(item.get("payee_name") or item.get("imported_description") or ""),
            imported=bool(str(item.get("imported_id") or "").strip()),
        )
        for item in snapshot.get("transactions") or []
        if item.get("date") is not None
        and not item.get("is_child")
        and not item.get("is_starting_balance")
    ]
    matches = match_charges(
        charges,
        candidates,
        window_days=settings.anticipated_match_window_days,
        used_transaction_ids=database.matched_transaction_ids(),
        aliases=aliases,
    )
    by_id = {row["id"]: row for row in open_rows}
    by_transaction = {candidate.id: candidate for candidate in candidates}
    for match in matches:
        if database.resolve_anticipated_charge(
            match.charge_id,
            MATCHED,
            matched_transaction_id=match.transaction_id,
            matched_payee=match.payee_name,
            matched_date=match.date.isoformat(),
            match_reason=match.reason,
        ):
            summary["matched"] += 1
            _learn_from_settlement(
                database, by_id[match.charge_id], by_transaction[match.transaction_id], aliases
            )
    settled = {match.charge_id for match in matches}
    for charge_id in expired_charges(
        [charge for charge in charges if charge.id not in settled],
        today=today,
        expire_days=settings.anticipated_expire_days,
    ):
        if database.resolve_anticipated_charge(charge_id, EXPIRED, match_reason="never posted"):
            summary["expired"] += 1
    if summary["matched"] or summary["expired"]:
        log.info(
            "Anticipated charges: %d settled against Actual, %d expired unposted",
            summary["matched"],
            summary["expired"],
        )
    remaining = database.list_anticipated_charges(status=OPEN, limit=1000)
    summary["categorized"] = classify(database, snapshot, settings, remaining, today=today)
    summary["open"] = [
        public_charge(row) for row in database.list_anticipated_charges(status=OPEN, limit=1000)
    ]
    return summary


def classify(
    database: Database,
    snapshot: dict[str, Any],
    settings: Settings,
    rows: list[dict[str, Any]],
    *,
    today: datetime.date,
) -> int:
    """Give each open anticipation a provisional category from memory.

    The same evidence and the same thresholds as the filing cascade's memory
    path: the user's own filed history plus Clerk's applied decisions. A
    notification's merchant is looked up through its alias first (the bank's
    name for the shop is what the history was filed under), then under its own
    key. Nothing is asked of the model; a taught category is left alone.
    Returns how many rows changed.
    """

    candidates = [row for row in rows if row.get("category_source") != SOURCE_TAUGHT]
    if not candidates:
        return 0
    aliases = database.alias_map()
    keys: set[str] = set()
    for row in candidates:
        key = str(row.get("merchant_key") or "")
        if key:
            keys.add(key)
            if key in aliases:
                keys.add(aliases[key])
    stored = []
    for key in keys:
        for entry in database.memory_for(key):
            stored.append(
                {
                    **entry,
                    "last_seen_date": datetime.datetime.fromtimestamp(
                        entry["last_seen"], datetime.UTC
                    ).date(),
                }
            )
    memory = build_memory(snapshot, stored, today=today)
    known = {
        str(category.get("id") or ""): str(category.get("name") or "")
        for category in snapshot.get("categories") or []
        if not category.get("is_income")
    }
    changed = 0
    for row in candidates:
        key = str(row.get("merchant_key") or "")
        match = None
        for lookup_key in (aliases.get(key), key):
            if not lookup_key:
                continue
            match = memory.lookup(
                lookup_key,
                min_observations=settings.memory_min_observations,
                min_confidence=settings.memory_min_confidence,
                allowed_categories=set(known),
            )
            if match is not None:
                break
        if match is None:
            if row.get("category_id"):
                database.set_anticipated_category(
                    row["id"], category_id="", category_name="", source="", confidence=0.0
                )
                changed += 1
            continue
        name = known.get(match.category_id) or match.category_name
        if (
            row.get("category_id") != match.category_id
            or row.get("category_source") != SOURCE_MEMORY
        ):
            database.set_anticipated_category(
                row["id"],
                category_id=match.category_id,
                category_name=name,
                source=SOURCE_MEMORY,
                confidence=match.confidence,
            )
            changed += 1
    return changed


def teach_category(
    database: Database,
    row: dict[str, Any],
    *,
    category_id: str,
    category_name: str,
) -> None:
    """The user names the category: remember it for the merchant and its alias.

    Recorded as a correction, which is how a deliberate human choice is
    weighted, under the notification's key and, when the bank's name for the
    shop is known, under that key too -- so the real row files itself when it
    lands and the next notification is categorized on sight.
    """

    database.set_anticipated_category(
        row["id"],
        category_id=category_id,
        category_name=category_name,
        source=SOURCE_TAUGHT if category_id else "",
        confidence=1.0 if category_id else 0.0,
    )
    if not category_id:
        return
    key = str(row.get("merchant_key") or "")
    if key:
        database.record_memory(key, category_id, category_name, correction=True)
        target = database.alias_map().get(key)
        if target:
            database.record_memory(target, category_id, category_name, correction=True)


def teach_alias(database: Database, row: dict[str, Any], *, payee: str) -> dict[str, Any] | None:
    """The user names the bank's payee for this notification's merchant."""

    key = str(row.get("merchant_key") or "")
    target = normalize_merchant(payee)
    if not key or not target:
        return None
    alias = database.upsert_alias(
        key,
        target,
        alias_label=str(row.get("merchant") or ""),
        merchant_label=payee,
        source=SOURCE_TAUGHT,
    )
    if alias and row.get("category_id") and row.get("category_source") == SOURCE_TAUGHT:
        database.record_memory(
            target, row["category_id"], row.get("category_name") or "", correction=True
        )
    return alias


def _learn_from_settlement(
    database: Database,
    row: dict[str, Any],
    transaction: CandidateTransaction,
    aliases: dict[str, str],
) -> None:
    """What a settlement teaches: the bank's name for the shop, and a taught category."""

    charge_key = str(row.get("merchant_key") or "")
    posted_key = transaction.merchant_key
    if charge_key and posted_key and charge_key != posted_key:
        alias = database.upsert_alias(
            charge_key,
            posted_key,
            alias_label=str(row.get("merchant") or ""),
            merchant_label=transaction.payee_name,
        )
        if alias:
            aliases[charge_key] = alias["merchant_key"]
    if row.get("category_source") == SOURCE_TAUGHT and row.get("category_id") and posted_key:
        # The user said where this goes before the bank had a row for it. Say
        # so under the bank's key as well, so the filing run that follows this
        # sync files the real row the same way.
        database.record_memory(
            posted_key, row["category_id"], row.get("category_name") or "", correction=True
        )


def public_charge(row: dict[str, Any]) -> dict[str, Any]:
    """The shape the overview, the web UI, and the phone all read."""

    item = dict(row)
    for field in ("noticed_at", "resolved_at", "created_at", "updated_at"):
        value = item.get(field)
        item[field] = (
            datetime.datetime.fromtimestamp(float(value), datetime.UTC).isoformat()
            if value
            else None
        )
    item["counts"] = item.get("status") == OPEN and item.get("kind") == KIND_CHARGE
    return item


def _as_charge(row: dict[str, Any]) -> AnticipatedCharge:
    try:
        noticed = datetime.date.fromisoformat(str(row.get("noticed_date") or ""))
    except ValueError:
        noticed = datetime.datetime.fromtimestamp(float(row["noticed_at"]), datetime.UTC).date()
    return AnticipatedCharge(
        id=str(row["id"]),
        account_id=str(row.get("actual_account_id") or ""),
        amount_cents=int(row.get("amount_cents", 0)),
        noticed_date=noticed,
        merchant_key=str(row.get("merchant_key") or ""),
    )
