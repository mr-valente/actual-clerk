"""Durable jobs, the schedule that creates them, and the work they do.

There is one worker. The Actual budget file is a SQLite database that is not
safe for concurrent writers, and a single-user homelab budget has no work to
parallelise anyway. What the job machinery buys instead is durability: leases
so a job that dies mid-run is reclaimed after a restart, bounded retries with
backoff, one active job per kind, and a persisted timeline for every run.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import uuid
from typing import Any

from actual_clerk.categorize import (
    STATUS_APPLIED,
    Categorizer,
    rule_promotion_candidates,
)
from actual_clerk.clients.actual import ActualGateway, ActualGatewayError
from actual_clerk.clients.ntfy import NotificationError, NtfyClient
from actual_clerk.clients.simplefin import SimpleFinClient, SimpleFinError
from actual_clerk.config import Settings, SettingsManager
from actual_clerk.db import Database
from actual_clerk.digest import build_digest, health_alert
from actual_clerk.domain import tagging
from actual_clerk.domain.health import evaluate_accounts, summarize
from actual_clerk.reporting import (
    budget_report,
    freshness,
    to_actual_accounts,
    to_simplefin_accounts,
)

log = logging.getLogger(__name__)

SCHEDULER_TICK_SECONDS = 20
OVERVIEW_SNAPSHOT = "overview"
# How long after its scheduled time a morning digest is still a morning digest.
# A container that was down until the evening should wait for tomorrow rather
# than wish its owner good morning at eleven at night.
DIGEST_WINDOW_HOURS = 6


def digest_is_due(
    local_now: datetime.datetime,
    scheduled: datetime.time,
    *,
    window_hours: int = DIGEST_WINDOW_HOURS,
) -> bool:
    """Whether a morning digest should go out right now.

    True from the scheduled time until the window closes. Outside that, today's
    digest is simply missed: a container that was down all morning should wait
    for tomorrow rather than wish its owner good morning at eleven at night.
    """

    start = datetime.datetime.combine(local_now.date(), scheduled, tzinfo=local_now.tzinfo)
    return start <= local_now < start + datetime.timedelta(hours=window_hours)


class ProcessingError(RuntimeError):
    def __init__(self, message: str, *, code: str = "processing_error", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class JobManager:
    def __init__(
        self,
        database: Database,
        settings_manager: SettingsManager,
        gateway: ActualGateway,
    ):
        self.database = database
        self.settings_manager = settings_manager
        self.gateway = gateway
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._worker_id = f"worker-{uuid.uuid4().hex[:8]}"

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._stopping.clear()
        self._tasks = [
            asyncio.create_task(self._worker_loop(), name="clerk-worker"),
            asyncio.create_task(self._scheduler_loop(), name="clerk-scheduler"),
        ]
        self.wake()

    async def stop(self) -> None:
        self._stopping.set()
        self.wake()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []

    def wake(self) -> None:
        self._wake.set()

    def settings_changed(self) -> None:
        self.gateway.settings_changed()
        self.wake()

    async def enqueue(
        self, kind: str, *, trigger: str = "manual", params: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], bool]:
        settings = self.settings_manager.get()
        job, created = self.database.enqueue_job(
            kind, settings.job_max_attempts, trigger=trigger, params=params
        )
        if created:
            self.wake()
        return job, created

    # ----------------------------------------------------------------- loops

    async def _worker_loop(self) -> None:
        while not self._stopping.is_set():
            job = self.database.claim_job(self._worker_id, self.settings_manager.get().lease_seconds)
            if job is None:
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=SCHEDULER_TICK_SECONDS)
                continue
            await self._run_job(job)

    async def _scheduler_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                self._schedule_due_work()
            except Exception as exc:  # noqa: BLE001 - the scheduler must never die
                log.warning("Scheduler tick failed: %s", exc)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.shield(self._stopping.wait()), timeout=SCHEDULER_TICK_SECONDS
                )

    def _schedule_due_work(self) -> None:
        settings = self.settings_manager.get()
        # Nothing scheduled can succeed before Actual is connected, and a fresh
        # install should not greet its owner with a wall of failed runs. A
        # manual run is still allowed, so the real error is one click away.
        if not (settings.secret_value("actual_password") and settings.actual_budget_id):
            return
        now = datetime.datetime.now(datetime.UTC)
        if settings.sync_enabled and self._is_due(
            "sync", now, settings.sync_interval_minutes * 60
        ):
            self.enqueue_sync_nowait(trigger="schedule")
        if self._is_due("health", now, settings.health_interval_minutes * 60):
            self._enqueue_nowait("health", trigger="schedule")
        if settings.digest_enabled and settings.notifications_enabled:
            local_now = datetime.datetime.now(settings.zone)
            if digest_is_due(local_now, settings.digest_clock) and not self._digest_sent(
                local_now.date().isoformat()
            ):
                self._enqueue_nowait("digest", trigger="schedule")

    def enqueue_sync_nowait(self, *, trigger: str) -> None:
        self._enqueue_nowait("sync", trigger=trigger)

    def _enqueue_nowait(self, kind: str, *, trigger: str) -> None:
        settings = self.settings_manager.get()
        _, created = self.database.enqueue_job(kind, settings.job_max_attempts, trigger=trigger)
        if created:
            self.wake()

    def _is_due(self, kind: str, now: datetime.datetime, interval_seconds: float) -> bool:
        last = self.database.last_job(kind)
        if last is None:
            return True
        if last["status"] in ("queued", "running", "retry_wait"):
            return False
        reference = last.get("completed_at") or last.get("created_at") or 0
        return now.timestamp() - float(reference) >= interval_seconds

    def _digest_sent(self, local_date: str) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM digests WHERE local_date=? LIMIT 1", (local_date,)
            ).fetchone()
        return row is not None

    # ------------------------------------------------------------ dispatch

    async def _run_job(self, job: dict[str, Any]) -> None:
        settings = self.settings_manager.get()
        heartbeat = asyncio.create_task(self._heartbeat(job["id"], settings.lease_seconds))
        handlers = {
            "sync": self._run_sync,
            "categorize": self._run_categorize,
            "health": self._run_health,
            "digest": self._run_digest,
        }
        try:
            handler = handlers.get(job["kind"])
            if handler is None:
                raise ProcessingError(f"Unknown job kind: {job['kind']}", code="unknown_kind")
            result = await handler(job, settings)
            self.database.finish_job(job["id"], result=result)
            self.database.add_event(job["id"], "info", "completed", _describe(job["kind"], result))
        except asyncio.CancelledError:
            self.database.fail_or_retry(
                job["id"], "cancelled", "Clerk stopped while this job was running", True
            )
            raise
        except (ProcessingError, ActualGatewayError, SimpleFinError) as exc:
            retryable = getattr(exc, "retryable", False)
            code = getattr(exc, "code", type(exc).__name__)
            self.database.fail_or_retry(job["id"], str(code), str(exc), retryable)
            log.warning("%s job failed: %s", job["kind"], exc)
        except Exception as exc:  # noqa: BLE001 - a job failure must not stop the worker
            self.database.fail_or_retry(job["id"], "unexpected_error", str(exc), True)
            log.exception("%s job raised an unexpected error", job["kind"])
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    async def _heartbeat(self, job_id: str, lease_seconds: int) -> None:
        interval = max(15, lease_seconds // 3)
        while True:
            await asyncio.sleep(interval)
            self.database.heartbeat(job_id, lease_seconds)

    # ------------------------------------------------------------- handlers

    async def _run_sync(self, job: dict[str, Any], settings: Settings) -> dict[str, Any]:
        self.database.update_job(job["id"], phase="pulling")
        result: dict[str, Any] = {}
        if settings.bank_sync_enabled:
            self.database.update_job(job["id"], phase="bank_sync")
            try:
                bank = await self.gateway.bank_sync(run_rules=True)
                result["imported"] = bank["imported"]
                if bank["imported"]:
                    self.database.add_event(
                        job["id"],
                        "info",
                        "bank_sync",
                        f"Imported {bank['imported']} transaction(s) from bank sync",
                        bank,
                    )
            except ActualGatewayError as exc:
                # A failed bank sync must not stop the rest of the run: Clerk
                # still has a budget to report on, and the health check is what
                # explains the failure.
                result["bank_sync_error"] = str(exc)
                self.database.add_event(job["id"], "warning", "bank_sync_failed", str(exc))
        else:
            await self.gateway.pull()

        self.database.update_job(job["id"], phase="reading")
        today = datetime.datetime.now(settings.zone).date()
        snapshot = await self.gateway.snapshot(today=today)
        await self._refresh_overview(snapshot, settings, today)
        result["accounts"] = len(snapshot["accounts"])
        result["transactions"] = len(snapshot["transactions"])

        if settings.categorization_enabled:
            self._enqueue_nowait("categorize", trigger="sync")
        return result

    async def _run_categorize(self, job: dict[str, Any], settings: Settings) -> dict[str, Any]:
        if not settings.categorization_enabled:
            return {"skipped": "categorization is disabled"}
        today = datetime.datetime.now(settings.zone).date()
        self.database.update_job(job["id"], phase="reading")
        snapshot = await self.gateway.snapshot(today=today)

        # A first run against an existing budget has years of uncategorized
        # history to work through, which the recent-window default would skip.
        full = bool((job.get("params") or {}).get("full"))
        lookback = settings.history_lookback_days if full else settings.categorize_lookback_days

        stored = _stored_memory(self.database, snapshot)
        categorizer = Categorizer(settings)
        try:
            self.database.update_job(job["id"], phase="classifying")
            result = await categorizer.run(
                snapshot,
                today=today,
                lookback_days=lookback,
                stored_memory=[row for rows in stored.values() for row in rows],
                exclude_transaction_ids=self.database.open_review_transaction_ids(),
            )
        finally:
            await categorizer.close()

        if not result.proposals:
            await self._refresh_overview(snapshot, settings, today)
            return {"considered": 0, "lookback_days": lookback, "full_history": full}

        self.database.update_job(
            job["id"], phase="writing", total=len(result.updates), current=0
        )
        if settings.tagging_enabled:
            written_tags = {tag for proposal in result.applied for tag in proposal.tags}
            if settings.tag_provenance and settings.clerk_tag:
                written_tags.add(settings.clerk_tag)
            catalog = [
                entry
                for entry in tagging.tag_catalog(settings.clerk_tag)
                if entry["tag"] in written_tags
            ]
            with contextlib.suppress(ActualGatewayError):
                await self.gateway.ensure_tags(catalog)

        write_result = await self.gateway.apply_updates(result.updates)
        applied_ids = set(write_result["applied"])
        skipped = {item["id"]: item["reason"] for item in write_result["skipped"]}

        for proposal in result.proposals:
            if proposal.status == STATUS_APPLIED and proposal.transaction_id in skipped:
                proposal.status = "skipped"
                proposal.rationale["skipped_reason"] = skipped[proposal.transaction_id]
            self.database.add_decision(proposal.as_decision(job["id"]))
            if proposal.status == STATUS_APPLIED and proposal.transaction_id in applied_ids:
                self.database.record_memory(
                    proposal.merchant_key, proposal.category_id or "", proposal.category_name
                )

        self.database.update_job(job["id"], current=len(applied_ids))
        promotions = 0
        if settings.rule_promotion_enabled:
            promotions = self._suggest_rules(result.applied, stored, settings)

        refreshed = await self.gateway.snapshot(today=today)
        await self._refresh_overview(refreshed, settings, today)
        summary = result.summary()
        summary["written"] = len(applied_ids)
        summary["rule_suggestions"] = promotions
        summary["lookback_days"] = lookback
        summary["full_history"] = full
        return summary

    def _suggest_rules(
        self, applied: list[Any], stored: dict[str, list[dict[str, Any]]], settings: Settings
    ) -> int:
        created = 0
        for candidate in rule_promotion_candidates(
            applied, stored, promote_after=settings.rule_promote_after
        ):
            if self.database.suggest_rule(**candidate):
                created += 1
        return created

    async def _run_health(self, job: dict[str, Any], settings: Settings) -> dict[str, Any]:
        today = datetime.datetime.now(settings.zone).date()
        self.database.update_job(job["id"], phase="reading")
        snapshot = await self.gateway.snapshot(today=today)

        remote_payload: dict[str, Any] | None = None
        simplefin_error = ""
        client = SimpleFinClient(settings)
        try:
            if client.configured:
                self.database.update_job(job["id"], phase="simplefin")
                remote_payload = await client.fetch(balances_only=True)
        except SimpleFinError as exc:
            simplefin_error = str(exc)
            self.database.add_event(job["id"], "warning", "simplefin_failed", str(exc))
        finally:
            await client.close()

        results = evaluate_accounts(
            accounts=to_actual_accounts(snapshot),
            remote_accounts=to_simplefin_accounts(remote_payload),
            errors=(remote_payload or {}).get("errors", []),
            simplefin_configured=client.configured and not simplefin_error,
            balance_stale_hours=settings.balance_stale_hours,
            balance_tolerance_cents=settings.balance_tolerance_cents,
            transaction_stale_days=settings.transaction_stale_days,
            unmonitored_ids=self.database.unmonitored_account_ids(),
        )
        snapshots = [item.as_dict() for item in results]
        self.database.prune_health([item["account_id"] for item in snapshots])
        transitions = self.database.record_health(snapshots)

        if settings.health_alerts_enabled and transitions:
            await self._notify_health(transitions)

        await self._refresh_overview(snapshot, settings, today, health=snapshots)
        summary = summarize(results)
        summary["simplefin_error"] = simplefin_error
        summary["transitions"] = len(transitions)
        return summary

    async def _notify_health(self, transitions: list[dict[str, Any]]) -> None:
        settings = self.settings_manager.get()
        if not settings.notifications_enabled:
            return
        alert = health_alert(transitions)
        if alert is None:
            return
        client = NtfyClient(settings)
        try:
            await client.publish(**alert)
        except NotificationError as exc:
            log.warning("Could not deliver the connection alert: %s", exc)
        finally:
            await client.close()

    async def _run_digest(self, job: dict[str, Any], settings: Settings) -> dict[str, Any]:
        """Build the morning report and deliver it.

        A forced run is a rehearsal: it sends the same message but does not
        reserve today's date, so the real morning delivery still happens.
        """
        forced = bool((job.get("params") or {}).get("force"))
        local_now = datetime.datetime.now(settings.zone)
        local_date = local_now.date().isoformat()
        if not forced and not self.database.claim_digest(local_date):
            return {"skipped": "already sent today"}
        try:
            today = local_now.date()
            snapshot = await self.gateway.snapshot(today=today)
            overview = await self._refresh_overview(snapshot, settings, today)
            payload = build_digest(
                report=overview["budget"],
                health=self.database.health_snapshots(),
                review_count=self.database.counts()["needs_review"],
                currency=settings.budget_currency,
                today=today,
            )
            if not settings.notifications_enabled:
                if not forced:
                    self.database.complete_digest(
                        local_date, payload, delivered=False, error="notifications are disabled"
                    )
                return {
                    "delivered": False,
                    "forced": forced,
                    "reason": "notifications are disabled",
                }
            client = NtfyClient(settings)
            try:
                await client.publish(
                    title=payload["title"],
                    message=payload["message"],
                    priority=payload["priority"],
                    tags=payload["tags"],
                )
            finally:
                await client.close()
            if not forced:
                self.database.complete_digest(local_date, payload, delivered=True)
            return {"delivered": True, "forced": forced, "title": payload["title"]}
        except Exception:
            # Release the claim so a retry, or tomorrow's run, is not blocked
            # by a digest that never actually went out.
            if not forced:
                self.database.release_digest(local_date)
            raise

    # ------------------------------------------------------------- overview

    async def _refresh_overview(
        self,
        snapshot: dict[str, Any],
        settings: Settings,
        today: datetime.date,
        health: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        unmonitored = self.database.unmonitored_account_ids()
        overview = {
            "budget": budget_report(snapshot, settings, today=today),
            "freshness": freshness(
                snapshot, settings, today=today, unmonitored_ids=unmonitored
            ),
            "accounts": [
                {
                    "id": account["id"],
                    "name": account["name"],
                    "balance_cents": account["balance_cents"],
                    "off_budget": account["off_budget"],
                    "closed": account["closed"],
                    "sync_source": account["sync_source"],
                    "type": account["type"],
                    "monitored": account["id"] not in unmonitored,
                }
                for account in snapshot["accounts"]
                if not account["closed"]
            ],
            "categories": [
                {
                    "id": category["id"],
                    "name": category["name"],
                    "group_name": category["group_name"],
                    "is_income": category["is_income"],
                }
                for category in snapshot["categories"]
                if not category["hidden"]
            ],
            "groups": sorted(
                {
                    category["group_name"]
                    for category in snapshot["categories"]
                    if category["group_name"] and not category["is_income"]
                }
            ),
            "currency": settings.budget_currency,
            "today": today.isoformat(),
        }
        if health is not None:
            overview["health"] = health
        else:
            overview["health"] = self.database.health_snapshots()
        self.database.set_snapshot(OVERVIEW_SNAPSHOT, overview)
        return overview

    async def refresh_now(self) -> dict[str, Any]:
        """Read the budget and rebuild the overview outside the job queue."""
        settings = self.settings_manager.get()
        today = datetime.datetime.now(settings.zone).date()
        snapshot = await self.gateway.snapshot(today=today)
        return await self._refresh_overview(snapshot, settings, today)


def _stored_memory(database: Database, snapshot: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Clerk's own learned rows for the merchants about to be considered."""
    keys = {
        item["merchant_key"]
        for item in snapshot["transactions"]
        if item.get("merchant_key") and not item.get("category_id")
    }
    stored: dict[str, list[dict[str, Any]]] = {}
    for key in keys:
        rows = database.memory_for(key)
        if rows:
            stored[key] = [
                {
                    **row,
                    "last_seen_date": datetime.datetime.fromtimestamp(
                        row["last_seen"], datetime.UTC
                    ).date(),
                }
                for row in rows
            ]
    return stored


def _describe(kind: str, result: dict[str, Any]) -> str:
    if kind == "sync":
        parts = [f"{result.get('transactions', 0)} transaction(s) read"]
        if result.get("imported"):
            parts.insert(0, f"{result['imported']} imported")
        return ", ".join(parts)
    if kind == "categorize":
        return (
            f"{result.get('applied', 0)} applied, {result.get('needs_review', 0)} for review, "
            f"{result.get('model_calls', 0)} model call(s)"
        )
    if kind == "health":
        return f"{result.get('linked', 0)} linked account(s), {result.get('degraded', 0)} degraded"
    if kind == "digest":
        return result.get("title") or str(result.get("reason") or "digest complete")
    return "complete"
