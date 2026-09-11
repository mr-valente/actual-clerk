"""Deliver Plaid's change stream into Actual, one Item at a time.

For every Item with at least one enabled mapping:

1. **Refresh**, if allowed: ask Plaid to extract now, then wait -- bounded --
   for the Item to report a newer successful update. Clerk cannot receive
   Plaid's webhook, so this poll is the substitute.
2. **Read** ``/transactions/sync`` from the stored cursor to the end,
   restarting from the original cursor if Plaid reports a mutation during
   pagination.
3. **Plan** each mapped account's operations (``domain.plaid_import``), then
   apply them through the Actual worker: adoptions first, so a posted
   transaction settles its pending row before anything else is written, then
   deletions, then the import, which runs Actual's own reconciliation and
   rules.
4. **Persist the cursor** only after every mapped account on the Item was
   written. A failed run therefore replays the same window, and Actual's
   dedup by ``imported_id`` makes that replay idempotent.

One Item failing never stops the others; its failure is recorded on the Item
(as ``needs_repair`` when Link's update mode is the fix) and reported.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from typing import Any

from actual_clerk.clients.actual import ActualGateway, ActualGatewayError
from actual_clerk.clients.plaid import PlaidClient, PlaidError
from actual_clerk.config import Settings
from actual_clerk.db import Database
from actual_clerk.domain.plaid_import import (
    AccountPlan,
    PlaidTransactionError,
    plan_account,
    starting_balance_cents,
)

log = logging.getLogger(__name__)

MUTATION_ERROR = "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"
MAX_MUTATION_RESTARTS = 3
NOT_READY = "TRANSACTIONS_UPDATE_STATUS_NOT_READY"
REFRESH_POLL_SECONDS = 5.0
STARTING_BALANCE_PAYEE = "Starting Balance"
STARTING_BALANCE_CATEGORY = "Starting Balances"


class EventSink:
    """Where the engine reports progress; the job manager hands it its own."""

    def __call__(self, level: str, event_type: str, message: str, data: dict[str, Any] | None = None) -> None:
        log.log(logging.WARNING if level == "warning" else logging.INFO, "%s: %s", event_type, message)


async def read_stream(client: PlaidClient, access_token: str, cursor: str) -> dict[str, Any]:
    """Every page from ``cursor`` to the end, folded into one change set."""

    for attempt in range(MAX_MUTATION_RESTARTS + 1):
        added: list[dict[str, Any]] = []
        modified: list[dict[str, Any]] = []
        removed: list[dict[str, Any]] = []
        accounts: list[dict[str, Any]] = []
        update_status = ""
        page_cursor = cursor
        try:
            while True:
                page = await client.transactions_sync_page(access_token, cursor=page_cursor)
                added.extend(page["added"])
                modified.extend(page["modified"])
                removed.extend(page["removed"])
                if page["accounts"]:
                    accounts = page["accounts"]
                update_status = page["update_status"] or update_status
                page_cursor = page["next_cursor"] or page_cursor
                if not page["has_more"]:
                    break
        except PlaidError as exc:
            if exc.error_code != MUTATION_ERROR:
                raise
            if attempt < MAX_MUTATION_RESTARTS:
                continue
            raise PlaidError(
                "Plaid kept changing the transaction stream during pagination",
                error_code=MUTATION_ERROR,
                retryable=True,
            ) from exc
        return {
            "added": added,
            "modified": modified,
            "removed": removed,
            "accounts": accounts,
            "update_status": update_status,
            "next_cursor": page_cursor,
        }
    raise AssertionError("unreachable")  # pragma: no cover


class PlaidSyncEngine:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        gateway: ActualGateway,
        *,
        client: PlaidClient | None = None,
        events: EventSink | Any = None,
        today: datetime.date | None = None,
        clock: Any = time.time,
        sleep: Any = asyncio.sleep,
    ):
        self.database = database
        self.settings = settings
        self.gateway = gateway
        self.client = client
        self.events = events or EventSink()
        self.today = today or datetime.date.today()
        self.clock = clock
        self.sleep = sleep
        self._starting_balance_category: str | None = None

    # ------------------------------------------------------------------ run

    async def run(self) -> dict[str, Any]:
        """Sync every Item with an enabled mapping. Never raises for one Item."""
        totals = {
            "items": 0, "items_failed": 0, "imported": 0, "updated": 0,
            "adopted": 0, "deleted": 0, "kept": 0, "starting_balances": 0,
            "skipped_before_cutover": 0, "refreshed": 0, "not_ready": 0,
        }
        links = [
            link for link in self.database.list_bank_links(provider="plaid", enabled_only=True)
        ]
        by_item: dict[str, list[dict[str, Any]]] = {}
        for link in links:
            by_item.setdefault(link["item_id"], []).append(link)
        items = [item for item in self.database.list_plaid_items() if item["item_id"] in by_item]
        if not items:
            return totals
        owned = self.client is None
        client = self.client or PlaidClient(self.settings)
        try:
            for item in items:
                totals["items"] += 1
                try:
                    result = await self._sync_item(client, item, by_item[item["item_id"]])
                except (PlaidError, ActualGatewayError, PlaidTransactionError) as exc:
                    totals["items_failed"] += 1
                    needs_repair = getattr(exc, "needs_repair", False)
                    self.database.update_plaid_item(
                        item["item_id"],
                        status="needs_repair" if needs_repair else "error",
                        last_error=str(exc),
                    )
                    for link in by_item[item["item_id"]]:
                        self.database.update_bank_link(link["actual_account_id"], last_error=str(exc))
                    self.events(
                        "warning",
                        "plaid_item_failed",
                        f"{item.get('institution_name') or item['item_id']}: {exc}",
                        {"item_id": item["item_id"], "needs_repair": needs_repair},
                    )
                    continue
                for key, value in result.items():
                    if key in totals:
                        totals[key] += value
        finally:
            if owned:
                await client.close()
        return totals

    # ----------------------------------------------------------------- item

    async def _sync_item(
        self, client: PlaidClient, item: dict[str, Any], links: list[dict[str, Any]]
    ) -> dict[str, Any]:
        label = item.get("institution_name") or item["item_id"]
        access_token = item["access_token"]
        counts: dict[str, int] = {
            "imported": 0, "updated": 0, "adopted": 0, "deleted": 0, "kept": 0,
            "starting_balances": 0, "skipped_before_cutover": 0, "refreshed": 0, "not_ready": 0,
        }
        if await self._maybe_refresh(client, item):
            counts["refreshed"] = 1

        cursor = item.get("cursor") or ""
        # An Item's cursor is shared by all its accounts. A mapping added after
        # the Item has already been read would never see the history the
        # cursor has moved past, so a never-delivered mapping is first served
        # from the beginning of the stream, then joins the incremental read.
        fresh = [link for link in links if cursor and not link.get("last_import_at")]
        if fresh:
            history = await read_stream(client, access_token, "")
            history_balances = {account["id"]: account for account in history["accounts"]}
            for link in fresh:
                plan, opening = await self._apply_link(
                    link, history, history_balances.get(link["external_account_id"])
                )
                _tally(counts, plan, opening)
                self.events(
                    "info",
                    "plaid_backfilled",
                    f"{link.get('external_name') or link['actual_account_id']}: served from the start of "
                    f"{label}'s history ({plan.summary()['imports']} imported)",
                    {"item_id": item["item_id"], "actual_account_id": link["actual_account_id"]},
                )

        stream = await read_stream(client, access_token, cursor)
        changes = len(stream["added"]) + len(stream["modified"]) + len(stream["removed"])
        if not changes and stream["update_status"] == NOT_READY and not cursor:
            # Right after linking, Plaid is still preparing history. Keep the
            # empty cursor so the next run asks from the beginning again.
            counts["not_ready"] = 1
            self.events("info", "plaid_not_ready", f"{label}: Plaid is still preparing this connection's history")
            return counts

        balances = {account["id"]: account for account in stream["accounts"]}
        for link in links:
            plan, opening = await self._apply_link(link, stream, balances.get(link["external_account_id"]))
            _tally(counts, plan, opening)

        now = self.clock()
        self.database.update_plaid_item(
            item["item_id"],
            cursor=stream["next_cursor"] or item.get("cursor") or "",
            last_sync_at=now,
            status="ok",
            last_error="",
        )
        if changes or counts["starting_balances"]:
            self.events(
                "info",
                "plaid_item_synced",
                f"{label}: {counts['imported']} imported, {counts['updated']} updated, "
                f"{counts['adopted']} adopted, {counts['deleted']} removed",
                {"item_id": item["item_id"], **counts},
            )
        return counts

    async def _maybe_refresh(self, client: PlaidClient, item: dict[str, Any]) -> bool:
        settings = self.settings
        if not settings.plaid_refresh_enabled:
            return False
        last = item.get("last_refresh_at")
        if last and self.clock() - float(last) < settings.plaid_refresh_min_interval_minutes * 60:
            return False
        before = None
        try:
            before = await client.get_item(item["access_token"])
            await client.transactions_refresh(item["access_token"])
        except PlaidError as exc:
            if exc.needs_repair:
                raise
            # A refresh that Plaid declines (unsupported institution, rate
            # limit) is not a reason to skip reading what is already there.
            self.events("warning", "plaid_refresh_failed", f"{item.get('institution_name') or item['item_id']}: {exc}")
            return False
        self.database.update_plaid_item(item["item_id"], last_refresh_at=self.clock())
        await self._wait_for_update(client, item, before)
        return True

    async def _wait_for_update(
        self, client: PlaidClient, item: dict[str, Any], before: dict[str, Any] | None
    ) -> None:
        """Poll the Item until Plaid reports a newer update, or the wait runs out."""
        budget = float(self.settings.plaid_refresh_wait_seconds)
        if budget <= 0 or before is None:
            return
        previous_ok = before.get("last_successful_update")
        previous_failed = before.get("last_failed_update")
        waited = 0.0
        while waited < budget:
            step = min(REFRESH_POLL_SECONDS, budget - waited)
            await self.sleep(step)
            waited += step
            try:
                info = await client.get_item(item["access_token"])
            except PlaidError:
                return
            if info.get("error") and info["error"]["needs_repair"]:
                raise PlaidError(
                    f"Plaid {info['error']['error_type']}/{info['error']['error_code']}: "
                    f"{info['error']['error_message'] or 'the connection needs a fresh login'}",
                    error_type=info["error"]["error_type"],
                    error_code=info["error"]["error_code"],
                )
            newer_ok = info.get("last_successful_update") and info["last_successful_update"] != previous_ok
            newer_failed = info.get("last_failed_update") and info["last_failed_update"] != previous_failed
            if newer_ok or newer_failed:
                return

    # -------------------------------------------------------------- preview

    async def preview_link(self, link: dict[str, Any]) -> dict[str, Any]:
        """What the first delivery for this mapping would do, without writing.

        Reads the Item's whole stream from the beginning (the cursor is not
        touched), plans the account, and asks Actual for a dry-run import of
        the rows that would be new so its own matching is reflected too.
        """

        item = self.database.get_plaid_item(link["item_id"])
        if not item:
            raise PlaidError("Bank connection not found")
        owned = self.client is None
        client = self.client or PlaidClient(self.settings)
        try:
            stream = await read_stream(client, item["access_token"], "")
        finally:
            if owned:
                await client.close()
        account_id = link["actual_account_id"]
        cutover = _cutover(link, self.today)
        window = datetime.timedelta(days=self.settings.plaid_adopt_window_days)
        existing = await self.gateway.account_transactions(account_id, start=cutover - window)
        plan = plan_account(
            actual_account_id=account_id,
            external_account_id=link["external_account_id"],
            cutover=cutover,
            added=stream["added"],
            modified=stream["modified"],
            removed=stream["removed"],
            existing=existing,
            adopt_window_days=self.settings.plaid_adopt_window_days,
        )
        by_id = {row["id"]: row for row in existing}
        matched_by_actual = 0
        if plan.imports:
            preview = await self.gateway.import_transactions(account_id, plan.imports, dry_run=True)
            matched_by_actual = sum(
                1 for entry in preview.get("preview", []) if entry.get("existing")
            )
        empty = not existing and not await self.gateway.account_transactions(account_id)
        balance = next(
            (a for a in stream["accounts"] if a["id"] == link["external_account_id"]), None
        )
        opening = (
            starting_balance_cents(
                current_balance_cents=(balance or {}).get("balance_cents"), imports=plan.imports
            )
            if self.settings.plaid_starting_balance and empty and plan.imports
            else None
        )
        dates = sorted(row["date"] for row in plan.imports)
        return {
            "not_ready": stream["update_status"] == NOT_READY and not (
                stream["added"] or stream["modified"]
            ),
            "cutover_date": cutover.isoformat(),
            "counts": plan.summary(),
            "would_import": len(plan.imports) - matched_by_actual,
            "matched_by_actual": matched_by_actual,
            "import_range": [dates[0].isoformat(), dates[-1].isoformat()] if dates else None,
            "adoptions": [
                {
                    "date": by_id[a["transaction_id"]]["date"].isoformat(),
                    "amount_cents": by_id[a["transaction_id"]].get("amount_cents", 0),
                    "payee_name": by_id[a["transaction_id"]].get("payee_name", ""),
                    "previous_imported_id": a["previous_imported_id"],
                    "reason": a["reason"],
                }
                for a in plan.adoptions[:20]
                if a["transaction_id"] in by_id
            ],
            "deletions": [
                {"date": d["date"].isoformat() if d.get("date") else "", "amount_cents": d["amount_cents"]}
                for d in plan.deletions[:20]
            ],
            "opening_balance_cents": opening,
            "bank_balance_cents": (balance or {}).get("balance_cents"),
            "account_empty": empty,
        }

    # ----------------------------------------------------------------- link

    async def _apply_link(
        self, link: dict[str, Any], stream: dict[str, Any], balance: dict[str, Any] | None
    ) -> tuple[AccountPlan, int | None]:
        account_id = link["actual_account_id"]
        cutover = _cutover(link, self.today)
        window = datetime.timedelta(days=self.settings.plaid_adopt_window_days)
        existing = await self.gateway.account_transactions(account_id, start=cutover - window)
        plan = plan_account(
            actual_account_id=account_id,
            external_account_id=link["external_account_id"],
            cutover=cutover,
            added=stream["added"],
            modified=stream["modified"],
            removed=stream["removed"],
            existing=existing,
            adopt_window_days=self.settings.plaid_adopt_window_days,
        )
        if not self.settings.plaid_delete_removed_pending:
            plan.kept.extend(plan.deletions)
            plan.deletions = []

        opening: int | None = None
        if (
            self.settings.plaid_starting_balance
            and plan.imports
            and not existing
            and not await self.gateway.account_transactions(account_id)
        ):
            opening = starting_balance_cents(
                current_balance_cents=(balance or {}).get("balance_cents"),
                imports=plan.imports,
            )

        if plan.adoptions:
            result = await self.gateway.adopt_imported_ids(
                [
                    {key: value for key, value in adoption.items() if key not in ("previous_imported_id", "reason")}
                    for adoption in plan.adoptions
                ]
            )
            applied = {entry["id"] for entry in result.get("applied", [])}
            for adoption in plan.adoptions:
                if adoption["transaction_id"] in applied and adoption["reason"] != "settled":
                    self.database.record_adoption(
                        account_id=account_id,
                        transaction_id=adoption["transaction_id"],
                        previous_imported_id=adoption["previous_imported_id"],
                        imported_id=adoption.get("imported_id", adoption["previous_imported_id"]),
                        reason=adoption["reason"],
                    )
        if plan.deletions:
            await self.gateway.delete_transactions([item["transaction_id"] for item in plan.deletions])
        for item in plan.kept:
            self.events(
                "warning",
                "plaid_removed_cleared",
                f"{link.get('external_name') or account_id}: the bank withdrew a transaction Actual "
                f"already holds as cleared ({item['date']}, {item['amount_cents'] / 100:.2f}); left in place",
                {"actual_account_id": account_id, **{k: str(v) for k, v in item.items()}},
            )
        rows = list(plan.imports)
        if opening is not None:
            rows.insert(0, await self._opening_row(account_id, cutover, opening))
        if rows:
            result = await self.gateway.import_transactions(account_id, rows)
            if result.get("errors"):
                raise ActualGatewayError(
                    f"Actual refused {len(result['errors'])} imported row(s): {result['errors'][0]}"
                )
            if opening is not None:
                self.events(
                    "info",
                    "plaid_starting_balance",
                    f"{link.get('external_name') or account_id}: opening balance of "
                    f"{opening / 100:.2f} added so the account matches the bank",
                    {"actual_account_id": account_id, "amount_cents": opening},
                )
        self.database.update_bank_link(account_id, last_import_at=self.clock(), last_error="")
        return plan, opening

    async def _opening_row(self, account_id: str, cutover: datetime.date, amount: int) -> dict[str, Any]:
        row: dict[str, Any] = {
            "date": cutover - datetime.timedelta(days=1),
            "amount_cents": amount,
            "payee_name": STARTING_BALANCE_PAYEE,
            "imported_payee": STARTING_BALANCE_PAYEE,
            "imported_id": f"clerk-starting-balance-{account_id}",
            "cleared": True,
            "starting_balance_flag": True,
        }
        category = await self._starting_balance_category_id()
        if category:
            row["category_id"] = category
        return row

    async def _starting_balance_category_id(self) -> str:
        """Actual files its own opening balances under this income category."""
        if self._starting_balance_category is not None:
            return self._starting_balance_category
        self._starting_balance_category = ""
        try:
            snapshot = await self.gateway.snapshot(today=self.today)
        except ActualGatewayError:
            return ""
        for category in snapshot.get("categories") or []:
            if category.get("name") == STARTING_BALANCE_CATEGORY and category.get("is_income"):
                self._starting_balance_category = category["id"]
                break
        return self._starting_balance_category


def _tally(counts: dict[str, int], plan: AccountPlan, opening: int | None) -> None:
    summary = plan.summary()
    counts["imported"] += summary["imports"]
    counts["adopted"] += sum(1 for a in plan.adoptions if a["reason"] != "settled")
    counts["updated"] += sum(1 for a in plan.adoptions if a["reason"] == "settled")
    counts["deleted"] += summary["deletions"]
    counts["kept"] += summary["kept"]
    counts["skipped_before_cutover"] += summary["skipped_before_cutover"]
    counts["starting_balances"] += 1 if opening is not None else 0


def _cutover(link: dict[str, Any], today: datetime.date) -> datetime.date:
    try:
        return datetime.date.fromisoformat(str(link.get("cutover_date") or ""))
    except ValueError:
        return today
