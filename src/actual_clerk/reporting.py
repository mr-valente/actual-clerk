"""Turn one budget snapshot into everything the dashboard and digest show.

Kept separate from the job machinery so the numbers can be tested against a
plain dictionary, with no Actual server, no SimpleFIN, and no model anywhere in
the picture.
"""

from __future__ import annotations

import datetime
from typing import Any

from actual_clerk.config import Settings
from actual_clerk.domain.budget import (
    CategoryInfo,
    TransactionInfo,
    build_budget_report,
    month_bounds,
)
from actual_clerk.domain.health import (
    ActualAccountInfo,
    SimpleFinAccountInfo,
    UnconfirmedTransferInfo,
)


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


def to_actual_accounts(snapshot: dict[str, Any]) -> list[ActualAccountInfo]:
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
            last_sync=account["last_sync"],
            last_transaction_date=account.get("last_transaction_date"),
            off_budget=account["off_budget"],
            closed=account["closed"],
        )
        for account in snapshot["accounts"]
    ]


def to_simplefin_accounts(payload: dict[str, Any] | None) -> list[SimpleFinAccountInfo]:
    if not payload:
        return []
    return [
        SimpleFinAccountInfo(
            id=account["id"],
            name=account["name"],
            org_name=account.get("org_name", ""),
            connection_id=account.get("connection_id", ""),
            balance_cents=account.get("balance_cents", 0),
            balance_date=account.get("balance_date"),
            available_cents=account.get("available_cents"),
            last_transaction_date=account.get("last_transaction_date"),
            currency=account.get("currency", "USD"),
        )
        for account in payload.get("accounts", [])
    ]


def budget_report(
    snapshot: dict[str, Any], settings: Settings, *, today: datetime.date
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
