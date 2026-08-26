"""Builders for the plain-dict budget snapshot the gateway produces.

Everything downstream of `ActualGateway.snapshot` consumes dictionaries, so the
whole application can be exercised without an Actual server.
"""

from __future__ import annotations

import datetime
import itertools
from typing import Any

from actual_clerk.domain.merchants import merchant_label, normalize_merchant

_counter = itertools.count(1)


def category(
    name: str,
    group_name: str = "Everyday",
    *,
    category_id: str | None = None,
    is_income: bool = False,
    hidden: bool = False,
) -> dict[str, Any]:
    return {
        "id": category_id or f"cat-{name.lower().replace(' ', '-')}",
        "name": name,
        "group_id": f"grp-{group_name.lower().replace(' ', '-')}",
        "group_name": group_name,
        "is_income": is_income,
        "hidden": hidden,
    }


def account(
    name: str,
    *,
    account_id: str | None = None,
    sync_source: str = "simpleFin",
    external_id: str | None = None,
    balance_cents: int = 0,
    last_sync: datetime.datetime | None = None,
    off_budget: bool = False,
    closed: bool = False,
    bank_name: str = "",
    account_type: str = "checking",
) -> dict[str, Any]:
    identifier = account_id or f"acct-{name.lower().replace(' ', '-')}"
    return {
        "id": identifier,
        "name": name,
        "sync_source": sync_source,
        "external_id": external_id if external_id is not None else (f"sf-{identifier}" if sync_source else ""),
        "bank_name": bank_name,
        "balance_cents": balance_cents,
        "cleared_balance_cents": balance_cents,
        "unconfirmed_transfers": [],
        "last_sync": last_sync,
        "off_budget": off_budget,
        "closed": closed,
        "type": account_type,
        "last_transaction_date": None,
    }


def transaction(
    date: datetime.date,
    amount_cents: int,
    *,
    payee: str = "",
    description: str = "",
    category_id: str | None = None,
    category_name: str = "",
    account_id: str = "acct-checking",
    account_name: str = "Checking",
    notes: str = "",
    transaction_id: str | None = None,
    off_budget: bool = False,
    is_transfer: bool = False,
    is_starting_balance: bool = False,
    closed_account: bool = False,
) -> dict[str, Any]:
    payee = payee or description
    return {
        "id": transaction_id or f"txn-{next(_counter)}",
        "date": date,
        "amount_cents": amount_cents,
        "category_id": category_id,
        "category_name": category_name,
        "payee_name": payee,
        "imported_description": description or payee,
        "merchant_key": normalize_merchant(payee, description),
        "merchant_label": merchant_label(payee, description),
        "notes": notes,
        "account_id": account_id,
        "account_name": account_name,
        "off_budget": off_budget,
        "closed_account": closed_account,
        "is_transfer": is_transfer,
        "is_child": False,
        "is_starting_balance": is_starting_balance,
        "cleared": True,
        "pending": False,
        "imported_id": "",
        "schedule_id": None,
    }


def snapshot(
    *,
    categories: list[dict[str, Any]] | None = None,
    accounts: list[dict[str, Any]] | None = None,
    transactions: list[dict[str, Any]] | None = None,
    budgeted: dict[str, int] | None = None,
    budgeted_history: dict[str, dict[str, int]] | None = None,
    income_history: list[tuple[datetime.date, int]] | None = None,
    tags: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    categories = categories if categories is not None else default_categories()
    accounts = accounts if accounts is not None else [account("Checking", balance_cents=250000)]
    transactions = transactions or []

    last_transaction: dict[str, datetime.date] = {}
    for item in transactions:
        current = last_transaction.get(item["account_id"])
        if current is None or item["date"] > current:
            last_transaction[item["account_id"]] = item["date"]
    for entry in accounts:
        entry.setdefault("last_transaction_date", None)
        if entry["id"] in last_transaction:
            entry["last_transaction_date"] = last_transaction[entry["id"]]

    if income_history is None:
        income_ids = {item["id"] for item in categories if item["is_income"]}
        totals: dict[tuple[int, int], int] = {}
        for item in transactions:
            if item["category_id"] in income_ids and not item["off_budget"] and not item["is_transfer"]:
                key = (item["date"].year, item["date"].month)
                totals[key] = totals.get(key, 0) + max(0, item["amount_cents"])
        income_history = sorted(
            (datetime.date(year, month, 1), cents) for (year, month), cents in totals.items()
        )

    return {
        "accounts": accounts,
        "categories": categories,
        "groups": [],
        "budgeted": budgeted or {},
        "budgeted_history": budgeted_history or {},
        "transactions": transactions,
        "income_history": income_history,
        "tags": tags or [],
        "collected_at": datetime.datetime(2026, 8, 21, 12, tzinfo=datetime.UTC),
        "history_start": datetime.date(2024, 8, 21),
    }


def default_categories() -> list[dict[str, Any]]:
    return [
        category("Paycheck", "Income", is_income=True),
        category("Rent", "Bills"),
        category("Electric", "Bills"),
        category("Groceries", "Everyday"),
        category("Dining", "Everyday"),
        category("Coffee", "Everyday"),
        category("Subscriptions", "Bills"),
    ]


class FakeModel:
    """A stand-in for the local model that records every prompt it is given."""

    def __init__(self, responses: list[dict[str, Any]] | None = None, error: Exception | None = None):
        self.responses = list(responses or [])
        self.error = error
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def structured(self, *, name: str, schema: dict, system: str, user: str) -> dict[str, Any]:
        self.calls.append({"name": name, "system": system, "user": user})
        if self.error is not None:
            raise self.error
        if not self.responses:
            return {"category_number": 0, "confidence": 0.0, "reason": "no answer", "suggested_new_category": ""}
        return self.responses.pop(0)

    async def close(self) -> None:
        self.closed = True
