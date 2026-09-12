"""Turn one budget snapshot into everything the dashboard and digest show.

Kept separate from the job machinery so the numbers can be tested against a
plain dictionary, with no Actual server, no SimpleFIN, and no model anywhere in
the picture.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from typing import Any

from actual_clerk.config import Settings
from actual_clerk.domain.budget import (
    AnticipatedInfo,
    CategoryInfo,
    TransactionInfo,
    build_budget_report,
    month_bounds,
)
from actual_clerk.domain.health import (
    SIMPLEFIN,
    ActualAccountInfo,
    RemoteAccountInfo,
    StuckImportInfo,
    UnconfirmedTransferInfo,
)


def apply_bank_links(
    snapshot: dict[str, Any], links: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """Overlay the bank links Clerk manages onto a budget snapshot, in place.

    Actual only knows about the links it holds itself. An account whose feed
    Clerk delivers looks like a manual account to Actual, so its provider,
    external id, and institution are taken from Clerk's own link table. What
    Actual said is kept under ``actual_sync_source`` for the migration views.
    """

    by_account = {
        str(link.get("actual_account_id") or ""): link
        for link in links
        if link.get("enabled", True)
    }
    for account in snapshot.get("accounts") or []:
        account.setdefault("actual_sync_source", account.get("sync_source", ""))
        account.setdefault("actual_external_id", account.get("external_id", ""))
        account.setdefault("managed_by_clerk", False)
        link = by_account.get(str(account.get("id") or ""))
        if not link:
            continue
        account["sync_source"] = str(link.get("provider") or "")
        account["external_id"] = str(link.get("external_account_id") or "")
        account["bank_name"] = str(link.get("institution") or account.get("bank_name") or "")
        account["connection_id"] = str(link.get("item_id") or "")
        account["managed_by_clerk"] = True
    return snapshot


def to_category_infos(snapshot: dict[str, Any]) -> list[CategoryInfo]:
    return [
        CategoryInfo(
            id=category["id"],
            name=category["name"],
            group_name=category["group_name"],
            is_income=category["is_income"],
            hidden=category["hidden"],
        )
        for category in snapshot["categories"]
    ]


def to_transaction_infos(
    snapshot: dict[str, Any], *, start: datetime.date, end: datetime.date
) -> list[TransactionInfo]:
    return [
        TransactionInfo(
            id=item["id"],
            date=item["date"],
            amount_cents=item["amount_cents"],
            category_id=item.get("category_id"),
            account_id=item.get("account_id", ""),
            account_name=item.get("account_name", ""),
            payee_name=item.get("payee_name", ""),
            off_budget=item.get("off_budget", False),
            is_transfer=item.get("is_transfer", False),
            is_starting_balance=item.get("is_starting_balance", False),
        )
        for item in snapshot["transactions"]
        if start <= item["date"] <= end
    ]


def _uncleared_imports_by_account(
    snapshot: dict[str, Any],
) -> dict[str, tuple[StuckImportInfo, ...]]:
    """Group the bank-imported rows Actual still holds as uncleared.

    Only rows carrying an import id: a manual entry is uncleared until the user
    clears it, which says nothing about the bank. Split children are skipped
    because the parent carries the cleared flag for the whole transaction.
    """

    grouped: dict[str, list[StuckImportInfo]] = {}
    for item in snapshot.get("transactions") or []:
        if item.get("cleared") or item.get("is_child") or item.get("is_starting_balance"):
            continue
        if not str(item.get("imported_id") or "").strip():
            continue
        account_id = str(item.get("account_id") or "")
        if not account_id:
            continue
        grouped.setdefault(account_id, []).append(
            StuckImportInfo(
                amount_cents=int(item.get("amount_cents", 0)),
                date=item["date"],
                transaction_id=str(item.get("id") or ""),
            )
        )
    return {account_id: tuple(rows) for account_id, rows in grouped.items()}


def to_actual_accounts(snapshot: dict[str, Any]) -> list[ActualAccountInfo]:
    uncleared_imports = _uncleared_imports_by_account(snapshot)
    return [
        ActualAccountInfo(
            id=account["id"],
            name=account["name"],
            sync_source=account["sync_source"],
            external_id=account["external_id"],
            bank_name=account["bank_name"],
            balance_cents=account["balance_cents"],
            cleared_balance_cents=account.get("cleared_balance_cents"),
            unconfirmed_transfers=tuple(
                UnconfirmedTransferInfo(
                    amount_cents=int(item.get("amount_cents", 0)),
                    date=item["date"],
                    transaction_id=str(item.get("id") or ""),
                )
                for item in account.get("unconfirmed_transfers", [])
            ),
            uncleared_imports=uncleared_imports.get(account["id"], ()),
            last_sync=account["last_sync"],
            last_transaction_date=account.get("last_transaction_date"),
            off_budget=account["off_budget"],
            closed=account["closed"],
            managed_by_clerk=bool(account.get("managed_by_clerk", False)),
            actual_sync_source=str(account.get("actual_sync_source") or ""),
            connection_id=str(account.get("connection_id") or ""),
        )
        for account in snapshot["accounts"]
    ]


def to_remote_accounts(
    payload: dict[str, Any] | None, *, provider: str = SIMPLEFIN
) -> list[RemoteAccountInfo]:
    """Convert one provider's account payload into health readings."""

    if not payload:
        return []
    return [
        RemoteAccountInfo(
            id=account["id"],
            name=account["name"],
            org_name=account.get("org_name", ""),
            connection_id=account.get("connection_id", ""),
            balance_cents=account.get("balance_cents", 0),
            balance_date=account.get("balance_date"),
            available_cents=account.get("available_cents"),
            last_transaction_date=account.get("last_transaction_date"),
            currency=account.get("currency", "USD"),
            provider=str(account.get("provider") or provider),
        )
        for account in payload.get("accounts", [])
    ]


def to_simplefin_accounts(payload: dict[str, Any] | None) -> list[RemoteAccountInfo]:
    return to_remote_accounts(payload, provider=SIMPLEFIN)


def to_anticipated_infos(
    snapshot: dict[str, Any], charges: Sequence[dict[str, Any]]
) -> list[AnticipatedInfo]:
    """Open anticipations, with each account's budget standing read off the snapshot.

    A charge on an off-budget or closed account never competes for free money,
    and one whose account Actual no longer knows is left out the same way.
    """

    accounts = {
        str(account.get("id") or ""): account for account in snapshot.get("accounts") or []
    }
    infos = []
    for charge in charges:
        if charge.get("status", "open") != "open":
            continue
        account = accounts.get(str(charge.get("actual_account_id") or ""))
        if account is None or account.get("closed"):
            continue
        infos.append(
            AnticipatedInfo(
                id=str(charge.get("id") or ""),
                amount_cents=int(charge.get("amount_cents", 0)),
                account_id=str(charge.get("actual_account_id") or ""),
                off_budget=bool(account.get("off_budget")),
                merchant=str(charge.get("merchant") or ""),
                category_id=str(charge.get("category_id") or ""),
            )
        )
    return infos


def budget_report(
    snapshot: dict[str, Any],
    settings: Settings,
    *,
    today: datetime.date,
    anticipated: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    _, end = month_bounds(today)
    report = build_budget_report(
        today=today,
        categories=to_category_infos(snapshot),
        budgeted=snapshot["budgeted"],
        # Earlier months are needed to work out what a committed category still
        # holds; the report itself only counts spending inside the month.
        transactions=to_transaction_infos(snapshot, start=datetime.date.min, end=end),
        income_history=snapshot["income_history"],
        budgeted_history=snapshot.get("budgeted_history") or {},
        committed_groups=settings.committed_groups,
        income_override_cents=settings.monthly_income_override_cents,
        income_lookback_months=settings.income_lookback_months,
        anticipated=to_anticipated_infos(snapshot, anticipated),
    )
    return report.as_dict()


def freshness(
    snapshot: dict[str, Any],
    settings: Settings,
    *,
    today: datetime.date,
    unmonitored_ids: set[str] | None = None,
) -> dict[str, Any]:
    """How current Actual's transaction history is, per account and overall.

    A budget that silently stops receiving transactions looks exactly like a
    quiet month, so staleness is measured against each account's own history
    rather than against the calendar -- except for accounts the user has turned
    monitoring off for, which are expected to be quiet.
    """

    unmonitored_ids = unmonitored_ids or set()
    accounts = []
    newest: datetime.date | None = None
    for account in snapshot["accounts"]:
        if account["closed"]:
            continue
        last = account.get("last_transaction_date")
        days = (today - last).days if last else None
        if last and (newest is None or last > newest):
            newest = last
        accounts.append(
            {
                "account_id": account["id"],
                "account_name": account["name"],
                "last_transaction_date": last.isoformat() if last else None,
                "days_since_transaction": days,
                "last_sync": account["last_sync"].isoformat() if account["last_sync"] else None,
                "sync_source": account["sync_source"],
                "monitored": account["id"] not in unmonitored_ids,
                "stale": bool(
                    account["sync_source"]
                    and account["id"] not in unmonitored_ids
                    and (days is None or days > settings.transaction_stale_days)
                ),
            }
        )
    stale = [item for item in accounts if item["stale"]]
    return {
        "newest_transaction_date": newest.isoformat() if newest else None,
        "days_since_newest": (today - newest).days if newest else None,
        "accounts": accounts,
        "stale_accounts": len(stale),
        "up_to_date": not stale,
    }
