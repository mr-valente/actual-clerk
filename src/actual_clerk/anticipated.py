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

log = logging.getLogger(__name__)

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
    summary: dict[str, Any] = {"open": [], "matched": 0, "expired": 0}
    if not open_rows or not settings.anticipated_enabled:
        summary["open"] = [public_charge(row) for row in open_rows] if settings.anticipated_enabled else []
        return summary

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
    )
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
    summary["open"] = [
        public_charge(row) for row in database.list_anticipated_charges(status=OPEN, limit=1000)
    ]
    return summary


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
