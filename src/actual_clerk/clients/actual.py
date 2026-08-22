"""The single owner of Clerk's connection to the Actual budget file.

`actualpy` works the way Actual itself does: it downloads the budget, keeps a
local SQLite copy, applies changes to that copy, and syncs the resulting
messages back. That file is not safe for concurrent writers, so every read and
every write in Clerk goes through one gateway, on one thread, in order.

Everything the gateway hands back is a plain dict. Nothing outside this module
touches a SQLAlchemy object, which keeps the budget logic, the health checks,
and the categorizer testable without a running Actual server.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from actual import Actual, ActualError
from actual.database import Transactions
from actual.queries import (
    create_category,
    create_rule,
    create_tag,
    get_account,
    get_accounts,
    get_budgets,
    get_categories,
    get_category_groups,
    get_tags,
    get_transactions,
)
from actual.rules import Action, Condition, ConditionType, Rule
from sqlalchemy import case, func
from sqlmodel import select

from actual_clerk.config import Settings
from actual_clerk.domain.merchants import merchant_label, normalize_merchant
from actual_clerk.domain.tagging import apply_tags

log = logging.getLogger(__name__)

# Fields whose change invalidates an open budget session.
CONNECTION_FIELDS = (
    "actual_url",
    "actual_password",
    "actual_budget_id",
    "actual_encryption_password",
    "actual_verify_ssl",
)


class ActualGatewayError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def _parse_timestamp(value: Any) -> datetime.datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.UTC)


def _payee_name(transaction: Transactions) -> str:
    payee = getattr(transaction, "payee", None)
    return str(getattr(payee, "name", "") or "")


def transaction_dict(transaction: Transactions) -> dict[str, Any] | None:
    """Flatten one Actual transaction into the shape the rest of Clerk uses.

    Returns None for a row Clerk cannot place in time. Every number Clerk
    reports is anchored to a date, so one malformed row is skipped rather than
    allowed to fail the whole read.
    """

    if transaction.date is None:
        return None
    account = getattr(transaction, "account", None)
    category = getattr(transaction, "category", None)
    payee = _payee_name(transaction)
    description = transaction.imported_description or ""
    return {
        "id": transaction.id,
        "date": transaction.get_date(),
        "amount_cents": int(transaction.amount or 0),
        "category_id": transaction.category_id,
        "category_name": str(getattr(category, "name", "") or ""),
        "payee_name": payee,
        "imported_description": description,
        "merchant_key": normalize_merchant(payee, description),
        "merchant_label": merchant_label(payee, description),
        "notes": transaction.notes or "",
        "account_id": transaction.acct,
        "account_name": str(getattr(account, "name", "") or ""),
        "off_budget": bool(getattr(account, "offbudget", 0)),
        "closed_account": bool(getattr(account, "closed", 0)),
        "is_transfer": bool(transaction.transferred_id),
        "is_child": bool(transaction.is_child),
        "is_starting_balance": bool(transaction.starting_balance_flag),
        "cleared": bool(transaction.cleared),
        "pending": bool(transaction.pending),
        "imported_id": transaction.financial_id or "",
        "schedule_id": transaction.schedule_id,
    }


def account_balances(session: Any) -> dict[str, tuple[int, int]]:
    """Total and cleared balance per account, in cents.

    Actual's own balance counts every transaction, cleared or not, which is the
    figure the app shows and the one free money should be measured against. A
    bank, by contrast, reports only what it has actually posted. Comparing the
    two directly would report a mismatch every time a transaction is waiting to
    clear, so the cleared total is computed alongside it.
    """

    rows = session.exec(
        select(
            Transactions.acct,
            func.coalesce(func.sum(Transactions.amount), 0),
            func.coalesce(
                func.sum(case((Transactions.cleared == 1, Transactions.amount), else_=0)), 0
            ),
        )
        .where(Transactions.is_parent == 0, Transactions.tombstone == 0)
        .group_by(Transactions.acct)
    ).all()
    return {row[0]: (int(row[1] or 0), int(row[2] or 0)) for row in rows}


def collect_snapshot(actual: Actual, *, settings: Settings, today: datetime.date) -> dict[str, Any]:
    """Read everything Clerk needs from the budget in one pass."""
    session = actual.session
    history_start = today - datetime.timedelta(days=settings.history_lookback_days)
    # get_transactions treats end_date as exclusive.
    history_end = today + datetime.timedelta(days=1)

    groups_by_id: dict[str, dict[str, Any]] = {}
    for group in get_category_groups(session):
        groups_by_id[group.id] = {
            "id": group.id,
            "name": group.name or "",
            "is_income": bool(group.is_income),
            "hidden": bool(getattr(group, "hidden", 0)),
        }

    categories = []
    for category in get_categories(session):
        group = groups_by_id.get(category.cat_group or "", {})
        categories.append(
            {
                "id": category.id,
                "name": category.name or "",
                "group_id": category.cat_group or "",
                "group_name": group.get("name", ""),
                "is_income": bool(category.is_income) or bool(group.get("is_income")),
                "hidden": bool(category.hidden),
            }
        )

    balances = account_balances(session)
    accounts = []
    for account in get_accounts(session):
        bank = getattr(account, "bank", None)
        total, cleared = balances.get(account.id, (0, 0))
        accounts.append(
            {
                "id": account.id,
                "name": account.name or "",
                "sync_source": account.account_sync_source or "",
                "external_id": account.account_id or "",
                "bank_name": str(getattr(bank, "name", "") or ""),
                "balance_cents": total,
                "cleared_balance_cents": cleared,
                "uncleared_balance_cents": total - cleared,
                "last_sync": _parse_timestamp(account.last_sync),
                "off_budget": bool(account.offbudget),
                "closed": bool(account.closed),
                "type": account.type or "",
            }
        )

    transactions = [
        flattened
        for flattened in (
            transaction_dict(item)
            for item in get_transactions(session, start_date=history_start, end_date=history_end)
        )
        if flattened is not None
    ]

    # Every month's budget, not just this one: a bill accrued a twelfth at a
    # time is only understood by looking at what earlier months set aside.
    budgeted_history: dict[str, dict[str, int]] = {}
    for budget in get_budgets(session):
        if not budget.category_id or budget.month is None:
            continue
        key = budget.get_date().strftime("%Y-%m")
        budgeted_history.setdefault(key, {})[budget.category_id] = int(budget.amount or 0)
    budgeted = budgeted_history.get(today.strftime("%Y-%m"), {})

    last_transaction: dict[str, datetime.date] = {}
    for item in transactions:
        account_id = item["account_id"]
        current = last_transaction.get(account_id)
        if current is None or item["date"] > current:
            last_transaction[account_id] = item["date"]
    for account in accounts:
        account["last_transaction_date"] = last_transaction.get(account["id"])

    income_ids = {category["id"] for category in categories if category["is_income"]}
    income_history: dict[tuple[int, int], int] = {}
    for item in transactions:
        if item["category_id"] in income_ids and not item["off_budget"] and not item["is_transfer"]:
            key = (item["date"].year, item["date"].month)
            income_history[key] = income_history.get(key, 0) + max(0, item["amount_cents"])

    return {
        "accounts": accounts,
        "categories": categories,
        "groups": list(groups_by_id.values()),
        "budgeted": budgeted,
        "budgeted_history": budgeted_history,
        "transactions": transactions,
        "income_history": sorted(
            (datetime.date(year, month, 1), cents)
            for (year, month), cents in income_history.items()
        ),
        "tags": [
            {"tag": tag.tag or "", "color": tag.color or "", "description": tag.description or ""}
            for tag in get_tags(session)
        ],
        "collected_at": datetime.datetime.now(datetime.UTC),
        "history_start": history_start,
    }


class ActualGateway:
    """Serializes every interaction with the budget file onto one thread."""

    def __init__(self, settings_manager: Any, data_dir: Path):
        self._settings_manager = settings_manager
        self._data_dir = Path(data_dir)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="actual")
        self._lock = asyncio.Lock()
        self._actual: Actual | None = None
        self._fingerprint: tuple[Any, ...] | None = None
        self._state_lock = threading.Lock()
        self._last_error: str = ""
        self._last_connected_at: datetime.datetime | None = None
        self._closed = False

    # ------------------------------------------------------------------ status

    @property
    def connected(self) -> bool:
        with self._state_lock:
            return self._actual is not None

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "connected": self._actual is not None,
                "last_error": self._last_error,
                "last_connected_at": (
                    self._last_connected_at.isoformat() if self._last_connected_at else None
                ),
            }

    def settings_changed(self) -> None:
        """Drop the session when the connection settings themselves changed."""
        settings = self._settings_manager.get()
        if self._fingerprint is not None and self._fingerprint != self._connection_fingerprint(
            settings
        ):
            self._executor.submit(self._close_session)

    @staticmethod
    def _connection_fingerprint(settings: Settings) -> tuple[Any, ...]:
        return tuple(
            settings.secret_value(field) if field.endswith("password") else getattr(settings, field)
            for field in CONNECTION_FIELDS
        )

    # ------------------------------------------------------------- lifecycle

    def _close_session(self) -> None:
        with self._state_lock:
            actual, self._actual = self._actual, None
        if actual is None:
            return
        try:
            actual.__exit__(None, None, None)
        except Exception as exc:  # noqa: BLE001 - teardown must not raise
            log.debug("Ignoring error while closing the Actual session: %s", exc)

    def _open_session(self, settings: Settings) -> Actual:
        if not settings.secret_value("actual_password"):
            raise ActualGatewayError("An Actual server password is not configured")
        if not settings.actual_budget_id:
            raise ActualGatewayError("An Actual budget (sync ID) is not configured")
        data_dir = self._data_dir / "budget"
        data_dir.mkdir(parents=True, exist_ok=True)
        encryption = settings.secret_value("actual_encryption_password") or None
        actual = Actual(
            base_url=settings.actual_url,
            password=settings.secret_value("actual_password"),
            file=settings.actual_budget_id,
            encryption_password=encryption,
            data_dir=data_dir,
            cert=bool(settings.actual_verify_ssl),
            timeout=float(settings.request_timeout_seconds),
        )
        actual.__enter__()
        return actual

    def _ensure(self, *, refresh: bool) -> Actual:
        settings = self._settings_manager.get()
        fingerprint = self._connection_fingerprint(settings)
        with self._state_lock:
            actual = self._actual
            stale = actual is not None and self._fingerprint != fingerprint
        if stale:
            self._close_session()
            actual = None
        if actual is None:
            actual = self._open_session(settings)
            with self._state_lock:
                self._actual = actual
                self._fingerprint = fingerprint
                self._last_connected_at = datetime.datetime.now(datetime.UTC)
                self._last_error = ""
        elif refresh:
            actual.sync()
        return actual

    def _invoke(self, fn: Callable[[Actual], Any], refresh: bool) -> Any:
        try:
            actual = self._ensure(refresh=refresh)
            return fn(actual)
        except ActualGatewayError as exc:
            with self._state_lock:
                self._last_error = str(exc)
            raise
        except ActualError as exc:
            self._close_session()
            with self._state_lock:
                self._last_error = str(exc)
            raise ActualGatewayError(f"Actual rejected the request: {exc}", retryable=True) from exc
        except Exception as exc:  # noqa: BLE001 - any failure invalidates the session
            self._close_session()
            with self._state_lock:
                self._last_error = str(exc)
            raise ActualGatewayError(f"Actual request failed: {exc}", retryable=True) from exc

    async def run(self, fn: Callable[[Actual], Any], *, refresh: bool = True) -> Any:
        """Run `fn(actual)` on the budget thread with the session guaranteed open."""
        if self._closed:
            raise ActualGatewayError("The Actual gateway is shutting down")
        async with self._lock:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._executor, self._invoke, fn, refresh)

    async def close(self) -> None:
        self._closed = True
        async with self._lock:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self._executor, self._close_session)
        self._executor.shutdown(wait=True)

    # ---------------------------------------------------------------- actions

    async def snapshot(self, *, today: datetime.date | None = None) -> dict[str, Any]:
        settings = self._settings_manager.get()
        day = today or datetime.datetime.now(settings.zone).date()
        return await self.run(lambda actual: collect_snapshot(actual, settings=settings, today=day))

    async def test_connection(self) -> dict[str, Any]:
        def _test(actual: Actual) -> dict[str, Any]:
            accounts = get_accounts(actual.session)
            info = actual.get_metadata()
            return {
                "ok": True,
                "message": f"{len(accounts)} account(s) in {info.get('budgetName') or 'the budget'}",
                "accounts": len(accounts),
                "budget_name": info.get("budgetName") or "",
            }

        return await self.run(_test)

    async def bank_sync(self, *, run_rules: bool = True) -> dict[str, Any]:
        """Ask Actual's server to pull fresh transactions from the bank."""

        def _sync(actual: Actual) -> dict[str, Any]:
            imported = actual.run_bank_sync(run_rules=run_rules)
            if imported:
                actual.commit()
            return {
                "imported": len(imported),
                "accounts": sorted(
                    {str(getattr(getattr(item, "account", None), "name", "") or "") for item in imported}
                    - {""}
                ),
            }

        return await self.run(_sync)

    async def pull(self) -> dict[str, Any]:
        """Apply the server's pending changes without importing from the bank."""

        def _pull(actual: Actual) -> dict[str, Any]:
            return {"changes": len(actual.sync())}

        return await self.run(_pull, refresh=False)

    async def apply_updates(
        self, updates: Sequence[dict[str, Any]], *, overwrite: bool = False
    ) -> dict[str, Any]:
        """Write categories and tags back to Actual in one committed batch.

        Every update re-reads the live transaction on the budget thread and
        derives the new note from whatever is in it right now, so a note the
        user edited between the proposal and the write is extended rather than
        replaced. A category the user set by hand also wins unless the caller
        is acting on an explicit human instruction: Clerk proposes, the person
        decides.
        """

        def _apply(actual: Actual) -> dict[str, Any]:
            session = actual.session
            applied: list[str] = []
            skipped: list[dict[str, str]] = []
            for update in updates:
                transaction = session.get(Transactions, update["transaction_id"])
                if transaction is None or transaction.tombstone:
                    skipped.append({"id": update["transaction_id"], "reason": "deleted"})
                    continue
                category_id = update.get("category_id")
                occupied = bool(transaction.category_id) and transaction.category_id != category_id
                if category_id and occupied and not overwrite:
                    skipped.append({"id": transaction.id, "reason": "already_categorized"})
                    continue
                changed = False
                if category_id and transaction.category_id != category_id:
                    transaction.category_id = category_id
                    changed = True
                add_tags = update.get("add_tags") or []
                if add_tags:
                    notes = apply_tags(transaction.notes, add_tags)
                    if notes != (transaction.notes or ""):
                        transaction.notes = notes
                        changed = True
                if changed:
                    applied.append(transaction.id)
                else:
                    skipped.append({"id": transaction.id, "reason": "no_change"})
            if applied:
                actual.commit()
            return {"applied": applied, "skipped": skipped}

        if not updates:
            return {"applied": [], "skipped": []}
        return await self.run(_apply, refresh=False)

    async def ensure_tags(self, catalog: Sequence[dict[str, Any]]) -> list[str]:
        """Register Clerk's tags in Actual so they carry a colour and meaning."""

        def _ensure(actual: Actual) -> list[str]:
            existing = {
                (tag.tag or "").casefold() for tag in get_tags(actual.session, include_deleted=False)
            }
            created: list[str] = []
            for entry in catalog:
                name = str(entry.get("tag") or "").strip()
                if not name or name.casefold() in existing:
                    continue
                create_tag(
                    actual.session,
                    name,
                    description=str(entry.get("description") or ""),
                    color=str(entry.get("color") or "#690cb0"),
                )
                created.append(name)
            if created:
                actual.commit()
            return created

        if not catalog:
            return []
        return await self.run(_ensure, refresh=False)

    async def create_category(self, name: str, group_name: str) -> dict[str, Any]:
        def _create(actual: Actual) -> dict[str, Any]:
            category = create_category(actual.session, name, group_name)
            actual.commit()
            return {"id": category.id, "name": category.name, "group_name": group_name}

        return await self.run(_create, refresh=False)

    async def create_category_rule(
        self, *, match_value: str, category_id: str, run_immediately: bool = False
    ) -> dict[str, Any]:
        """Promote a repeatedly confirmed merchant into a native Actual rule.

        Once the rule exists Actual applies it on import, before Clerk ever sees
        the transaction, so the same decision stops costing anything at all.
        """

        def _create(actual: Actual) -> dict[str, Any]:
            rule = Rule(
                conditions=[
                    Condition(
                        field="imported_description", op=ConditionType.CONTAINS, value=match_value
                    )
                ],
                operation="and",
                actions=[Action(field="category", value=category_id)],
                stage=None,
            )
            created = create_rule(actual.session, rule, run_immediately=run_immediately)
            actual.commit()
            return {"id": created.id, "match_value": match_value, "category_id": category_id}

        return await self.run(_create, refresh=False)

    async def find_account(self, name: str) -> dict[str, Any] | None:
        def _find(actual: Actual) -> dict[str, Any] | None:
            account = get_account(actual.session, name)
            return {"id": account.id, "name": account.name} if account else None

        return await self.run(_find, refresh=False)
