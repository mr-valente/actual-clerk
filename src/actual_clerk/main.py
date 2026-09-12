from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import mimetypes
import os
import secrets
import sqlite3
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError

from actual_clerk import __version__, anticipated
from actual_clerk import intelligence as takeover
from actual_clerk.clients.actual import ActualGateway, ActualGatewayError
from actual_clerk.clients.ntfy import NotificationError, NtfyClient
from actual_clerk.clients.openai_compatible import ModelError, OpenAICompatibleClient
from actual_clerk.clients.plaid import PlaidClient, PlaidError
from actual_clerk.clients.simplefin import SimpleFinClient, SimpleFinError
from actual_clerk.config import TIMEZONE_CHOSEN_KEY, SettingsManager, data_directory
from actual_clerk.db import Database
from actual_clerk.diagnostics import build_report
from actual_clerk.domain.merchants import merchant_label, normalize_merchant
from actual_clerk.plaid_links import describe, public_item, read_items
from actual_clerk.plaid_sync import PlaidSyncEngine
from actual_clerk.processing import OVERVIEW_SNAPSHOT, JobManager, ProcessingError
from actual_clerk.schemas import (
    ActualRulesRequest,
    BulkResolveRequest,
    ClaimSetupTokenRequest,
    CreateAliasRequest,
    CreateCategoryRequest,
    CreateLinkRequest,
    CreateRuleRequest,
    DeclineProposalsRequest,
    EnqueueRequest,
    ExchangeRequest,
    ForwardNotificationRequest,
    LinkTokenRequest,
    MigrateToPlaidRequest,
    MigrateToSimpleFinRequest,
    MonitoringRequest,
    RegisterSourceRequest,
    ResolveDecisionRequest,
    ResolveProposalRequest,
    SandboxItemRequest,
    ServerTokenRequest,
    SettingsPatch,
    TeachAliasRequest,
    TeachCategoryRequest,
    UpdateLinkRequest,
    UpdateRuleRequest,
    UpdateSourceRequest,
)

log = logging.getLogger(__name__)
STATIC_DIRECTORY = Path(__file__).parent / "static"


def _static_assets(directory: Path) -> dict[str, bytes]:
    """Read the immutable assets; the revalidated HTML shell is not part of its own hash."""
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path != directory / "index.html"
    }


def _fingerprint_static_assets(assets: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for asset_name in sorted(assets):
        content = assets[asset_name]
        name = asset_name.encode()
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()[:16]


def _static_asset_version(directory: Path) -> str:
    """Fingerprint every browser asset by relative name and content."""
    return _fingerprint_static_assets(_static_assets(directory))


STATIC_ASSETS = _static_assets(STATIC_DIRECTORY)
STATIC_ASSET_VERSION = _fingerprint_static_assets(STATIC_ASSETS)
INDEX_HTML = (
    (STATIC_DIRECTORY / "index.html")
    .read_text(encoding="utf-8")
    .replace("__STATIC_ASSET_VERSION__", STATIC_ASSET_VERSION)
)


def _configure_application_logging(
    level_name: str, *, stream: TextIO | None = None
) -> logging.Logger:
    """Give Clerk its own Docker-visible handler instead of relying on Uvicorn's."""
    package_logger = logging.getLogger("actual_clerk")
    package_logger.handlers.clear()
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    package_logger.addHandler(handler)
    package_logger.setLevel(getattr(logging, level_name))
    package_logger.propagate = False
    package_logger.disabled = False
    return package_logger


def _timestamp(value: float | None) -> str | None:
    return datetime.fromtimestamp(value, UTC).isoformat() if value else None


def _field_errors(exc: ValidationError) -> str:
    """One readable line per rejected field, for a toast rather than a log."""
    parts = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", ())) or "settings"
        message = str(error.get("msg", "")).removeprefix("Value error, ")
        parts.append(f"{location.replace('_', ' ')}: {message}")
    return "; ".join(parts) or str(exc)


def _serialize_job(job: dict[str, Any]) -> dict[str, Any]:
    result = dict(job)
    result.pop("worker_id", None)
    for field in ("created_at", "updated_at", "started_at", "completed_at", "next_run_at",
                  "lease_until"):
        result[field] = _timestamp(result.get(field))
    for event in result.get("events", []):
        event["created_at"] = _timestamp(event.get("created_at"))
    return result


def _serialize_record(record: dict[str, Any]) -> dict[str, Any]:
    result = dict(record)
    for field in (
        "created_at",
        "resolved_at",
        "checked_at",
        "since",
        "last_seen",
        "updated_at",
        "last_applied_at",
        "observed_at",
        "last_import_at",
    ):
        if field in result:
            result[field] = _timestamp(result.get(field))
    return result


@asynccontextmanager
async def lifespan(app: FastAPI):
    directory = data_directory()
    database = Database(directory / "clerk.db")
    database.initialize()
    settings_manager = SettingsManager(database)
    settings = settings_manager.get()
    _configure_application_logging(settings.log_level)
    await _claim_pending_setup_token(settings_manager)
    gateway = ActualGateway(settings_manager, directory)
    manager = JobManager(database, settings_manager, gateway)
    app.state.database = database
    app.state.settings_manager = settings_manager
    app.state.gateway = gateway
    app.state.job_manager = manager
    await manager.start()
    log.info(
        "Actual Clerk %s started with data directory %s and log level %s",
        __version__,
        directory,
        settings.log_level,
    )
    try:
        yield
    finally:
        await manager.stop()
        await gateway.close()
        log.info("Actual Clerk stopped")


async def _claim_pending_setup_token(settings_manager: SettingsManager) -> None:
    """Redeem a setup token supplied through the environment, exactly once.

    A SimpleFIN setup token can only be claimed once, so the token is cleared
    whichever way this goes: a second startup must not burn a fresh one, and a
    token that was already claimed elsewhere should stop being retried.
    """

    settings = settings_manager.get()
    token = settings.secret_value("simplefin_setup_token")
    if not token or settings.secret_value("simplefin_access_url"):
        return
    try:
        access_url = await SimpleFinClient.claim(token)
    except SimpleFinError as exc:
        log.warning("Could not claim the SimpleFIN setup token: %s", exc)
        access_url = ""
    try:
        settings_manager.update(
            {"simplefin_setup_token": "", **({"simplefin_access_url": access_url} if access_url else {})}
        )
    except ValueError as exc:
        log.warning("Could not store the claimed SimpleFIN access URL: %s", exc)
    else:
        if access_url:
            log.info("Claimed the SimpleFIN setup token and stored its access URL")


app = FastAPI(
    title="Actual Clerk",
    description="A budgeting assistant sidecar for Actual Budget",
    version=__version__,
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)


@app.middleware("http")
async def response_cache_control(request: Request, call_next: Any) -> Response:
    response = await call_next(request)
    if request.url.path.startswith("/api/") and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-store"
    elif request.url.path.startswith("/assets/"):
        if (
            response.status_code in (200, 304)
            and request.query_params.get("v") == STATIC_ASSET_VERSION
        ):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
    return response


def _database(request: Request) -> Database:
    return request.app.state.database


def _settings_manager(request: Request) -> SettingsManager:
    return request.app.state.settings_manager


def _gateway(request: Request) -> ActualGateway:
    return request.app.state.gateway


def _jobs(request: Request) -> JobManager:
    return request.app.state.job_manager


@app.exception_handler(ProcessingError)
async def processing_error_handler(_: Request, exc: ProcessingError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"error": exc.code, "detail": str(exc)},
    )


@app.exception_handler(ActualGatewayError)
async def gateway_error_handler(_: Request, exc: ActualGatewayError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={"error": "actual_unavailable", "detail": str(exc)},
    )


@app.exception_handler(PlaidError)
async def plaid_error_handler(_: Request, exc: PlaidError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={"error": "plaid_error", "detail": str(exc), **exc.as_dict()},
    )


# ------------------------------------------------------------------ read-only


@app.get("/api/health")
async def health(request: Request) -> dict[str, Any]:
    settings = _settings_manager(request).get()
    return {
        "status": "ok",
        "version": __version__,
        "configured": {
            "actual": bool(
                settings.secret_value("actual_password") and settings.actual_budget_id
            ),
            "simplefin": bool(settings.secret_value("simplefin_access_url")),
            "plaid": settings.plaid_configured,
            "model": bool(settings.openai_base_url and settings.model and settings.ai_enabled),
            "notifications": bool(settings.notifications_enabled and settings.ntfy_topic),
        },
    }


@app.get("/api/overview")
async def overview(request: Request) -> dict[str, Any]:
    database = _database(request)
    settings = _settings_manager(request).get()
    snapshot = database.get_snapshot(OVERVIEW_SNAPSHOT) or {}
    last_sync = database.last_job("sync", "completed")
    return {
        "counts": database.counts(),
        "overview": snapshot,
        "jobs": [_serialize_job(job) for job in database.list_jobs(limit=6)],
        "last_sync": _serialize_job(last_sync) if last_sync else None,
        "gateway": _gateway(request).status(),
        "digest": database.latest_digest(),
        "actual_url": settings.actual_url,
        "currency": settings.budget_currency,
        "apply_mode": settings.apply_mode,
        "sync_enabled": settings.sync_enabled,
        "stale": _snapshot_age(snapshot),
    }


def _snapshot_age(snapshot: dict[str, Any]) -> float | None:
    updated = snapshot.get("snapshot_updated_at")
    return round(datetime.now(UTC).timestamp() - float(updated), 1) if updated else None


@app.post("/api/refresh", status_code=status.HTTP_202_ACCEPTED)
async def refresh(request: Request) -> dict[str, Any]:
    """Read the budget immediately rather than waiting for the next tick."""
    return await _jobs(request).refresh_now()


@app.get("/api/diagnostics")
async def diagnostics(request: Request, redact: bool = Query(False)) -> dict[str, Any]:
    """One plain-text report explaining every figure the Overview shows.

    Reads only. The live read is attempted first so the report can compare what
    Actual holds right now against the snapshot the dashboard is drawn from --
    which is the difference most disagreements turn out to be.
    """
    settings = _settings_manager(request).get()
    database = _database(request)
    gateway = _gateway(request)
    today = datetime.now(settings.zone).date()

    snapshot: dict[str, Any] | None = None
    probe: dict[str, Any] | None = None
    snapshot_error = ""
    try:
        snapshot = await _jobs(request).snapshot(today=today)
        probe = await gateway.diagnostics()
    except ActualGatewayError as exc:
        snapshot_error = str(exc)
    except Exception as exc:  # noqa: BLE001 - a diagnostic must never fail to render
        snapshot_error = f"{type(exc).__name__}: {exc}"

    report = build_report(
        settings=settings,
        today=today,
        snapshot=snapshot,
        stored_overview=database.get_snapshot(OVERVIEW_SNAPSHOT),
        probe=probe,
        gateway_status=gateway.status(),
        jobs=[_serialize_job(job) for job in database.list_jobs(limit=10)],
        last_sync=database.last_job("sync", "completed"),
        health=[_serialize_record(item) for item in database.health_snapshots()],
        unmonitored=database.unmonitored_account_ids(),
        counts=database.counts(),
        digests=database.list_digests(limit=10),
        digest_jobs=[
            _serialize_job(job) for job in database.list_jobs(kind="digest", limit=10)
        ],
        timezone_chosen=database.get_setting(TIMEZONE_CHOSEN_KEY) == "1",
        snapshot_error=snapshot_error,
        redact=redact,
    )
    return {"report": report, "generated_at": datetime.now(UTC).isoformat()}


@app.get("/api/accounts")
async def accounts(request: Request) -> dict[str, Any]:
    database = _database(request)
    return {
        "health": [_serialize_record(item) for item in database.health_snapshots()],
        "events": [_serialize_record(item) for item in database.list_health_events(limit=40)],
    }


@app.post("/api/accounts/{account_id}/monitoring")
async def set_monitoring(
    account_id: str, payload: MonitoringRequest, request: Request
) -> dict[str, Any]:
    """Turn Clerk's connection watching on or off for one account.

    Nothing in Actual changes: the account stays linked and keeps importing.
    Clerk simply stops scoring it, alerting on it, and counting it as stale,
    which is what a dormant legacy account needs.
    """

    database = _database(request)
    database.set_monitoring(account_id, payload.monitored, payload.account_name)
    await _jobs(request).enqueue("health", trigger="manual")
    return {"account_id": account_id, "monitored": payload.monitored}


@app.get("/api/reviews")
async def reviews(request: Request) -> list[dict[str, Any]]:
    return [
        _serialize_record(item)
        for item in _database(request).list_decisions(status="needs_review", limit=250)
    ]


@app.get("/api/decisions")
async def decisions(
    request: Request,
    decision_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return [
        _serialize_record(item)
        for item in _database(request).list_decisions(status=decision_status, limit=limit)
    ]


@app.get("/api/decisions/{decision_id}")
async def decision(decision_id: str, request: Request) -> dict[str, Any]:
    item = _database(request).get_decision(decision_id)
    if not item:
        raise HTTPException(status_code=404, detail="Decision not found")
    return _serialize_record(item)


@app.get("/api/jobs")
async def list_jobs(
    request: Request,
    job_status: str | None = Query(default=None, alias="status"),
    kind: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    return [
        _serialize_job(job)
        for job in _database(request).list_jobs(status=job_status, kind=kind, limit=limit)
    ]


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str, request: Request) -> dict[str, Any]:
    job = _database(request).get_job(job_id, include_events=True)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _serialize_job(job)


# --------------------------------------------------------------------- writes


@app.post("/api/jobs", status_code=status.HTTP_202_ACCEPTED)
async def enqueue_job(payload: EnqueueRequest, request: Request) -> dict[str, Any]:
    params: dict[str, Any] | None = None
    if payload.kind == "categorize":
        if payload.full:
            params = {"full": True}
        elif payload.reviews:
            params = {"reviews": True}
        if payload.propose_rules:
            params = {**(params or {}), "propose_rules": True}
    elif payload.force and payload.kind == "digest":
        params = {"force": True}
    job, created = await _jobs(request).enqueue(payload.kind, trigger="manual", params=params)
    return {"created": created, "job": _serialize_job(job)}


@app.post("/api/jobs/{job_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_job(job_id: str, request: Request) -> dict[str, Any]:
    job = _database(request).retry_job(job_id)
    if not job:
        raise HTTPException(
            status_code=409, detail="This job is not retryable, or another run of it is active"
        )
    _jobs(request).wake()
    return _serialize_job(job)


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request) -> dict[str, bool]:
    if not _database(request).cancel_job(job_id):
        raise HTTPException(status_code=409, detail="Only queued jobs can be cancelled")
    return {"cancelled": True}


@app.post("/api/reviews/resolve")
async def resolve_reviews(payload: BulkResolveRequest, request: Request) -> dict[str, Any]:
    """Resolve many reviews in one write.

    A backlog is cleared a merchant at a time, not a transaction at a time, so
    the categories go to Actual in a single batch and every decision is claimed
    before that batch is sent -- a failed write hands them all back.
    """
    database = _database(request)
    settings = _settings_manager(request).get()

    if payload.action == "dismiss":
        dismissed = [
            decision_id
            for decision_id in payload.ids
            if database.resolve_decision(decision_id, "dismissed")
        ]
        return {"status": "dismissed", "resolved": len(dismissed)}

    updates: list[dict[str, Any]] = []
    claimed: list[tuple[str, dict[str, Any], str]] = []
    for decision_id in payload.ids:
        pending = database.get_decision(decision_id)
        if not pending or pending["status"] != "needs_review":
            continue
        category_id = (
            payload.category_id if payload.action == "recategorize" else pending["category_id"]
        )
        if not category_id:
            continue
        recategorized = payload.action == "recategorize"
        if not database.resolve_decision(
            decision_id,
            "applied",
            category_id=category_id if recategorized else None,
            category_name=(
                (_category_name(database, category_id) or pending["category_name"])
                if recategorized
                else None
            ),
        ):
            continue
        add_tags = list(pending.get("tags") or [])
        if settings.tag_provenance and settings.clerk_tag:
            add_tags.append(settings.clerk_tag)
        claimed.append((decision_id, pending, category_id))
        updates.append(
            {
                "transaction_id": pending["transaction_id"],
                "category_id": category_id,
                "add_tags": add_tags,
            }
        )

    if not updates:
        return {"status": "skipped", "resolved": 0}

    try:
        result = await _gateway(request).apply_updates(updates, overwrite=True)
    except ActualGatewayError:
        for decision_id, _, _ in claimed:
            database.reopen_decision(decision_id)
        raise

    applied = set(result["applied"])
    declared: set[str] = set()
    for _decision_id, pending, category_id in claimed:
        if pending["transaction_id"] not in applied:
            continue
        category_name = _category_name(database, category_id) or pending["category_name"]
        database.record_memory(
            pending["merchant_key"],
            category_id,
            category_name,
            correction=payload.action == "recategorize",
        )
        if payload.always and pending["merchant_key"] and pending["merchant_key"] not in declared:
            declared.add(pending["merchant_key"])
            _declare_rule(
                database,
                merchant_key=pending["merchant_key"],
                merchant_label=pending.get("payee_name") or "",
                category_id=category_id,
                category_name=category_name,
            )
    return {
        "status": "applied",
        "resolved": len(applied),
        "skipped": len(result["skipped"]),
        "rules": len(declared),
    }


@app.post("/api/reviews/{decision_id}/resolve")
async def resolve_review(
    decision_id: str, payload: ResolveDecisionRequest, request: Request
) -> dict[str, Any]:
    database = _database(request)
    settings = _settings_manager(request).get()
    pending = database.get_decision(decision_id)
    if not pending:
        raise HTTPException(status_code=404, detail="Review not found")
    if pending["status"] != "needs_review":
        raise HTTPException(status_code=409, detail="This review has already been resolved")

    if payload.action == "dismiss":
        if not database.resolve_decision(decision_id, "dismissed"):
            raise HTTPException(status_code=409, detail="This review has already been resolved")
        return {"status": "dismissed"}

    category_id = payload.category_id if payload.action == "recategorize" else pending["category_id"]
    if not category_id:
        raise HTTPException(
            status_code=422, detail="This review has no proposed category; choose one to apply"
        )
    category_name = pending["category_name"]
    if payload.action == "recategorize":
        category_name = _category_name(database, category_id) or category_name

    # Claim first: a second click must not produce a second write. The chosen
    # category is written on the decision too, so the ledger says what went
    # to Actual rather than what Clerk proposed.
    claimed = database.resolve_decision(
        decision_id,
        "applied",
        category_id=category_id if payload.action == "recategorize" else None,
        category_name=category_name if payload.action == "recategorize" else None,
    )
    if not claimed:
        raise HTTPException(status_code=409, detail="This review has already been resolved")

    add_tags = list(pending.get("tags") or [])
    if settings.tag_provenance and settings.clerk_tag:
        add_tags.append(settings.clerk_tag)
    try:
        result = await _gateway(request).apply_updates(
            [
                {
                    "transaction_id": pending["transaction_id"],
                    "category_id": category_id,
                    "add_tags": add_tags,
                }
            ],
            overwrite=True,
        )
    except ActualGatewayError:
        # The claim was taken before the write, so hand it back rather than
        # leaving a review recorded as applied that Actual never received.
        database.reopen_decision(decision_id)
        raise

    if not result["applied"]:
        reason = next(
            (item["reason"] for item in result["skipped"]), "the transaction could not be updated"
        )
        if reason == "deleted":
            return {"status": "skipped", "reason": reason}
    database.record_memory(
        pending["merchant_key"],
        category_id,
        category_name,
        correction=payload.action == "recategorize",
    )
    rule = None
    if payload.always and pending["merchant_key"]:
        rule = _declare_rule(
            database,
            merchant_key=pending["merchant_key"],
            merchant_label=pending.get("payee_name") or "",
            category_id=category_id,
            category_name=category_name,
        )
    return {
        "status": "applied",
        "category_id": category_id,
        "category_name": category_name,
        "rule": _serialize_record(rule) if rule else None,
    }


def _category_name(database: Database, category_id: str) -> str:
    snapshot = database.get_snapshot(OVERVIEW_SNAPSHOT) or {}
    for category in snapshot.get("categories", []):
        if category["id"] == category_id:
            return category["name"]
    return ""


def _declare_rule(
    database: Database,
    *,
    merchant_key: str,
    category_id: str,
    category_name: str,
    merchant_label: str = "",
    account_id: str = "",
    match: str = "exact",
    source: str = "user",
) -> dict[str, Any] | None:
    """Write the user's word and withdraw any open question it answers."""
    rule = database.upsert_rule(
        merchant_key=merchant_key,
        category_id=category_id,
        category_name=category_name,
        account_id=account_id,
        match=match,
        merchant_label=merchant_label,
        source=source,
    )
    if rule:
        database.close_proposals_for(merchant_key, kind="rule")
    return rule


# --------------------------------------------------------------- intelligence
#
# Everything Clerk knows about what a transaction means, and everything it
# wants to know: rules the user declared, questions Clerk is asking, and the
# merchants and aliases it has learned. Nothing here touches Actual.


@app.get("/api/intelligence")
async def intelligence(
    request: Request, include_retired: bool = Query(False)
) -> dict[str, Any]:
    database = _database(request)
    snapshot = database.get_snapshot(OVERVIEW_SNAPSHOT) or {}
    statuses = ("active", "paused", "retired") if include_retired else ("active", "paused")
    return {
        "rules": [_serialize_record(rule) for rule in database.list_rules(statuses=statuses)],
        "proposals": [
            _serialize_record(proposal) for proposal in database.list_proposals(limit=200)
        ],
        "aliases": [_serialize_record(alias) for alias in database.list_aliases()],
        "merchants": [_serialize_record(item) for item in database.list_memory(limit=500)],
        "counts": {
            **database.counts(),
            "memory_merchants": database.memory_size(),
            "aliases": len(database.list_aliases()),
        },
        "categories": snapshot.get("categories", []),
        "accounts": [
            {"id": account["id"], "name": account["name"]}
            for account in snapshot.get("accounts", [])
            if not account.get("closed")
        ],
    }


@app.get("/api/intelligence/merchant")
async def intelligence_merchant(text: str = Query(default="", max_length=200)) -> dict[str, Any]:
    """Show what a merchant name becomes, so a rule is made on the key it will match."""
    return {"merchant_key": normalize_merchant(text), "label": merchant_label(text)}


@app.post("/api/intelligence/rules", status_code=status.HTTP_201_CREATED)
async def create_rule(payload: CreateRuleRequest, request: Request) -> dict[str, Any]:
    database = _database(request)
    key = payload.merchant_key or normalize_merchant(payload.merchant)
    if not key:
        raise HTTPException(
            status_code=422, detail="That name is all decoration; nothing is left to match on"
        )
    name = _category_name(database, payload.category_id)
    if not name:
        raise HTTPException(status_code=404, detail="That category is not in the last snapshot")
    rule = _declare_rule(
        database,
        merchant_key=key,
        merchant_label=merchant_label(payload.merchant) or key,
        category_id=payload.category_id,
        category_name=name,
        account_id=payload.account_id,
        match=payload.match,
    )
    return {"rule": _serialize_record(rule)}


@app.post("/api/intelligence/aliases", status_code=status.HTTP_201_CREATED)
async def create_alias(payload: CreateAliasRequest, request: Request) -> dict[str, Any]:
    """Teach that one name for a shop is another: the alias resolves to the merchant."""
    alias_key = normalize_merchant(payload.alias)
    merchant_key = normalize_merchant(payload.merchant)
    if not alias_key or not merchant_key:
        raise HTTPException(status_code=422, detail="One of those names is all decoration")
    if alias_key == merchant_key:
        raise HTTPException(status_code=422, detail="Those two names are already the same merchant")
    database = _database(request)
    existing = database.alias_map()
    if merchant_key in existing or alias_key in set(existing.values()):
        raise HTTPException(
            status_code=422,
            detail="That would chain aliases; point both names at the same merchant instead",
        )
    alias = database.upsert_alias(
        alias_key,
        merchant_key,
        alias_label=merchant_label(payload.alias),
        merchant_label=merchant_label(payload.merchant),
        source="taught",
    )
    return {"alias": _serialize_record(alias)}


@app.delete("/api/intelligence/aliases/{alias_key}")
async def delete_alias(alias_key: str, request: Request) -> dict[str, Any]:
    if not _database(request).delete_alias(alias_key):
        raise HTTPException(status_code=404, detail="Alias not found")
    return {"deleted": True}


def _rule_or_404(request: Request, rule_id: str) -> dict[str, Any]:
    rule = _database(request).get_rule(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    return rule


@app.patch("/api/intelligence/rules/{rule_id}")
async def update_rule(rule_id: str, payload: UpdateRuleRequest, request: Request) -> dict[str, Any]:
    database = _database(request)
    _rule_or_404(request, rule_id)
    changes: dict[str, Any] = {}
    if payload.category_id is not None:
        name = _category_name(database, payload.category_id)
        if not name:
            raise HTTPException(status_code=404, detail="That category is not in the last snapshot")
        changes["category_id"] = payload.category_id
        changes["category_name"] = name
    if payload.status is not None:
        changes["status"] = payload.status
    if payload.match is not None:
        changes["match"] = payload.match
    return {"rule": _serialize_record(database.update_rule(rule_id, **changes))}


@app.delete("/api/intelligence/rules/{rule_id}")
async def retire_rule(rule_id: str, request: Request) -> dict[str, Any]:
    """Retire a rule. The row stays, so a mistake can be undone from the page."""
    _rule_or_404(request, rule_id)
    rule = _database(request).update_rule(rule_id, status="retired")
    return {"rule": _serialize_record(rule)}


async def _live_snapshot(request: Request) -> tuple[dict[str, Any], Any]:
    settings = _settings_manager(request).get()
    today = datetime.now(settings.zone).date()
    return await _jobs(request).snapshot(today=today), today


@app.get("/api/intelligence/actual")
async def intelligence_actual(request: Request) -> dict[str, Any]:
    """What Actual's rule table holds: what can move, what stays, and how each replays."""
    snapshot, today = await _live_snapshot(request)
    return await takeover.read_actual(
        _gateway(request), _database(request), today=today, snapshot=snapshot
    )


@app.post("/api/intelligence/actual/import")
async def intelligence_import(payload: ActualRulesRequest, request: Request) -> dict[str, Any]:
    snapshot, today = await _live_snapshot(request)
    return await takeover.import_rules(
        _gateway(request),
        _database(request),
        snapshot=snapshot,
        today=today,
        rule_ids=payload.rule_ids,
        dry_run=payload.dry_run,
    )


@app.post("/api/intelligence/actual/retire")
async def intelligence_retire(payload: ActualRulesRequest, request: Request) -> dict[str, Any]:
    return await takeover.retire_rules(
        _gateway(request), _database(request), rule_ids=payload.rule_ids, dry_run=payload.dry_run
    )


@app.post("/api/intelligence/actual/restore")
async def intelligence_restore(payload: ActualRulesRequest, request: Request) -> dict[str, Any]:
    return await takeover.restore_rules(
        _gateway(request), _database(request), rule_ids=payload.rule_ids, dry_run=payload.dry_run
    )


@app.post("/api/intelligence/proposals/decline")
async def decline_proposals(payload: DeclineProposalsRequest, request: Request) -> dict[str, Any]:
    """Wave away every open proposal of a kind, for a bulk run that asked too much."""
    declined = _database(request).decline_open_proposals(kind=payload.kind or None)
    return {"declined": declined}


@app.post("/api/intelligence/proposals/{proposal_id}/resolve")
async def resolve_proposal(
    proposal_id: str, payload: ResolveProposalRequest, request: Request
) -> dict[str, Any]:
    database = _database(request)
    proposal = database.claim_proposal(proposal_id)
    if not proposal:
        raise HTTPException(status_code=409, detail="This proposal has already been resolved")
    if payload.action == "decline":
        database.resolve_proposal(proposal_id, "declined")
        return {"status": "declined"}
    body = proposal["payload"]
    kind = proposal["kind"]

    def hand_back(detail: str) -> HTTPException:
        database.resolve_proposal(proposal_id, "open")
        return HTTPException(status_code=422, detail=detail)

    if kind == "rule":
        category_id = payload.category_id or body.get("category_id", "")
        category_name = _category_name(database, category_id) or body.get("category_name", "")
        rule = _declare_rule(
            database,
            merchant_key=body.get("merchant_key") or proposal["merchant_key"],
            merchant_label=body.get("merchant_label", ""),
            category_id=category_id,
            category_name=category_name,
            account_id=body.get("account_id", ""),
            source="proposal",
        )
        if rule is None:
            raise hand_back("The proposal no longer names a category")
        database.resolve_proposal(proposal_id, "accepted")
        return {"status": "accepted", "rule": _serialize_record(rule)}

    if kind == "alias":
        alias_key = str(body.get("alias_key") or proposal["merchant_key"])
        merchant_key = str(body.get("merchant_key") or "")
        existing = database.alias_map()
        if not alias_key or not merchant_key or alias_key == merchant_key:
            raise hand_back("The proposal no longer names two merchants")
        if merchant_key in existing or alias_key in set(existing.values()):
            raise hand_back("That alias would chain; declare it by hand instead")
        alias = database.upsert_alias(
            alias_key,
            merchant_key,
            alias_label=str(body.get("alias_label") or ""),
            source="taught",
        )
        database.resolve_proposal(proposal_id, "accepted")
        return {"status": "accepted", "alias": _serialize_record(alias) if alias else None}

    rule_id = str(body.get("rule_id") or "")
    rule = database.get_rule(rule_id) if rule_id else None
    if rule is None:
        database.resolve_proposal(proposal_id, "withdrawn")
        raise HTTPException(status_code=409, detail="The rule this proposal is about is gone")
    if kind in ("rule_change", "repair"):
        category_id = payload.category_id or body.get("category_id", "")
        category_name = _category_name(database, category_id)
        if not category_id or not category_name:
            raise hand_back("Choose a category that is in Actual")
        updated = database.update_rule(
            rule_id, category_id=category_id, category_name=category_name, status="active"
        )
        database.reset_rule_disputes(rule_id)
        database.resolve_proposal(proposal_id, "accepted")
        return {"status": "accepted", "rule": _serialize_record(updated)}
    if kind == "rule_retire":
        updated = database.update_rule(rule_id, status="retired")
        database.resolve_proposal(proposal_id, "accepted")
        return {"status": "accepted", "rule": _serialize_record(updated)}
    raise hand_back(f"Clerk cannot act on a {kind} proposal")


@app.post("/api/categories", status_code=status.HTTP_201_CREATED)
async def create_category(payload: CreateCategoryRequest, request: Request) -> dict[str, Any]:
    created = await _gateway(request).create_category(payload.name, payload.group_name)
    await _jobs(request).refresh_now()
    return created


@app.delete("/api/memory/{merchant_key}")
async def forget_merchant(merchant_key: str, request: Request) -> dict[str, bool]:
    """Drop what Clerk learned about one merchant so it is classified afresh."""
    _database(request).forget_merchant(merchant_key)
    return {"forgotten": True}


# ---------------------------------------------------------------------- Plaid
#
# Plaid Items are Clerk's own: Actual never sees them. Access tokens are stored
# in Clerk's database and never returned by any of these endpoints.


def _plaid_client(request: Request) -> PlaidClient:
    settings = _settings_manager(request).get()
    if not settings.plaid_configured:
        raise HTTPException(
            status_code=409, detail="Plaid is not configured: add a client id and secret in Settings"
        )
    return PlaidClient(settings)


def _plaid_item_or_404(request: Request, item_id: str) -> dict[str, Any]:
    item = _database(request).get_plaid_item(item_id)
    if not item or item.get("status") == "removed":
        raise HTTPException(status_code=404, detail="Bank connection not found")
    return item


def _refuse_taken_plaid_account(
    database: Database, external_account_id: str, *, actual_account_id: str = ""
) -> None:
    """Refuse a Plaid account that another Actual account's mapping still owns.

    The unique index on (provider, external id) covers paused mappings too,
    so this has to run before anything in Actual is created or unlinked: the
    insert would fail afterwards and leave that work half done.
    """
    owner = next(
        (
            link
            for link in database.list_bank_links(provider="plaid")
            if link["external_account_id"] == external_account_id
            and link["actual_account_id"] != actual_account_id
        ),
        None,
    )
    if owner is None:
        return
    detail = "That bank account is already mapped to another Actual account"
    if not owner["enabled"]:
        detail += " (a paused mapping; forget that mapping first)"
    raise HTTPException(status_code=409, detail=detail)


def _actual_accounts_for_mapping(request: Request) -> list[dict[str, Any]]:
    """The Actual accounts a Plaid account can be mapped onto, from the last snapshot."""
    snapshot = _database(request).get_snapshot(OVERVIEW_SNAPSHOT) or {}
    # Only a live mapping makes an account unavailable. A paused one (its
    # connection was removed, say) is replaced by mapping the account again.
    links = {
        link["actual_account_id"]: link
        for link in _database(request).list_bank_links(enabled_only=True)
    }
    accounts = []
    for account in snapshot.get("accounts") or []:
        if account.get("closed"):
            continue
        link = links.get(account["id"])
        accounts.append(
            {
                "id": account["id"],
                "name": account["name"],
                "off_budget": bool(account.get("off_budget")),
                "actual_sync_source": account.get("actual_sync_source", account.get("sync_source", "")),
                "linked_to": (
                    {"provider": link["provider"], "external_account_id": link["external_account_id"]}
                    if link
                    else None
                ),
            }
        )
    return accounts


@app.get("/api/plaid/items")
async def plaid_items(request: Request) -> dict[str, Any]:
    """Every Plaid Item Clerk holds, its accounts and balances, and the mappings."""
    settings = _settings_manager(request).get()
    database = _database(request)
    entries = await read_items(database, settings) if settings.plaid_configured else []
    result = describe(
        entries,
        database.list_bank_links(),
        environment=settings.plaid_env,
        items_ever=len(database.list_plaid_items(include_removed=True)),
    )
    result["configured"] = settings.plaid_configured
    result["redirect_uri"] = settings.plaid_redirect_uri
    result["actual_accounts"] = _actual_accounts_for_mapping(request)
    return result


@app.post("/api/plaid/link-token")
async def plaid_link_token(payload: LinkTokenRequest, request: Request) -> dict[str, Any]:
    """Mint a Link token: a new connection, or update mode for a named Item."""
    access_token = None
    if payload.item_id:
        access_token = _plaid_item_or_404(request, payload.item_id)["access_token"]
    client = _plaid_client(request)
    try:
        token = await client.create_link_token(
            access_token=access_token, account_selection=payload.account_selection
        )
    finally:
        await client.close()
    return {**token, "item_id": payload.item_id}


async def _store_item(
    request: Request,
    client: PlaidClient,
    *,
    access_token: str,
    item_id: str,
    institution_id: str = "",
    institution_name: str = "",
) -> dict[str, Any]:
    settings = _settings_manager(request).get()
    database = _database(request)
    info: dict[str, Any] = {}
    try:
        info = await client.get_item(access_token)
    except PlaidError as exc:
        log.warning("Stored Plaid Item %s before it could be described: %s", item_id, exc)
    stored = database.upsert_plaid_item(
        {
            "item_id": item_id,
            "access_token": access_token,
            "environment": settings.plaid_env,
            "institution_id": info.get("institution_id") or institution_id,
            "institution_name": info.get("institution_name") or institution_name,
            "status": "ok",
            "last_error": "",
        }
    )
    await _jobs(request).enqueue("health", trigger="manual")
    return public_item(stored)


@app.post("/api/plaid/exchange", status_code=status.HTTP_201_CREATED)
async def plaid_exchange(payload: ExchangeRequest, request: Request) -> dict[str, Any]:
    """Exchange Link's one-time public token and keep the resulting Item."""
    client = _plaid_client(request)
    try:
        exchanged = await client.exchange_public_token(payload.public_token)
        item = await _store_item(
            request,
            client,
            access_token=exchanged["access_token"],
            item_id=exchanged["item_id"],
            institution_id=payload.institution_id,
            institution_name=payload.institution_name,
        )
    finally:
        await client.close()
    return {"item": item, "accounts": [account.model_dump() for account in payload.accounts]}


@app.post("/api/plaid/sandbox/items", status_code=status.HTTP_201_CREATED)
async def plaid_sandbox_item(payload: SandboxItemRequest, request: Request) -> dict[str, Any]:
    """Create a sandbox Item without the Link UI. Refused outside the sandbox."""
    client = _plaid_client(request)
    try:
        created = await client.sandbox_create_item(
            institution_id=payload.institution_id, username=payload.username
        )
        item = await _store_item(
            request,
            client,
            access_token=created["access_token"],
            item_id=created["item_id"],
            institution_id=created["institution_id"],
        )
    finally:
        await client.close()
    return {"item": item}


@app.post("/api/plaid/items/{item_id}/repaired")
async def plaid_item_repaired(item_id: str, request: Request) -> dict[str, Any]:
    """After Link's update mode: re-read the Item and clear its repair flag if it is healthy."""
    item = _plaid_item_or_404(request, item_id)
    client = _plaid_client(request)
    try:
        info = await client.get_item(item["access_token"])
    finally:
        await client.close()
    database = _database(request)
    if info.get("error"):
        database.update_plaid_item(
            item_id,
            status="needs_repair" if info["error"]["needs_repair"] else "error",
            last_error=info["error"]["error_message"] or info["error"]["error_code"],
        )
        return {"repaired": False, "error": info["error"]}
    database.update_plaid_item(item_id, status="ok", last_error="")
    await _jobs(request).enqueue("health", trigger="manual")
    return {"repaired": True, "item": public_item(database.get_plaid_item(item_id) or item)}


@app.post("/api/plaid/items/{item_id}/remove")
async def plaid_item_remove(item_id: str, request: Request) -> dict[str, Any]:
    """Disconnect an Item at Plaid and disable every mapping that used it.

    On the Trial plan this does not give the Item slot back. A Plaid-side
    failure (the Item may already be dead) still removes it locally.
    """
    item = _plaid_item_or_404(request, item_id)
    client = _plaid_client(request)
    plaid_error = ""
    try:
        await client.remove_item(item["access_token"])
    except PlaidError as exc:
        plaid_error = str(exc)
        log.warning("Plaid refused to remove Item %s; removing locally anyway: %s", item_id, exc)
    finally:
        await client.close()
    disabled = _database(request).remove_plaid_item(item_id)
    await _jobs(request).enqueue("health", trigger="manual")
    return {"removed": True, "links_disabled": disabled, "plaid_error": plaid_error}


@app.post("/api/plaid/sandbox/items/{item_id}/reset-login")
async def plaid_sandbox_reset_login(item_id: str, request: Request) -> dict[str, Any]:
    """Break a sandbox Item's login to rehearse the repair flow."""
    item = _plaid_item_or_404(request, item_id)
    client = _plaid_client(request)
    try:
        await client.sandbox_reset_login(item["access_token"])
    finally:
        await client.close()
    await _jobs(request).enqueue("health", trigger="manual")
    return {"reset": True}


@app.post("/api/plaid/links", status_code=status.HTTP_201_CREATED)
async def plaid_create_link(payload: CreateLinkRequest, request: Request) -> dict[str, Any]:
    """Map one Plaid account onto an Actual account.

    Nothing is imported here; the mapping only tells later syncs where the
    feed goes and from which date. Mapping an account Actual still links to
    SimpleFIN is allowed (that is what a migration looks like mid-way), and
    the Connections page says so.
    """
    item = _plaid_item_or_404(request, payload.item_id)
    client = _plaid_client(request)
    try:
        accounts = (await client.get_accounts(item["access_token"]))["accounts"]
    finally:
        await client.close()
    external = next((a for a in accounts if a["id"] == payload.external_account_id), None)
    if external is None:
        raise HTTPException(
            status_code=404, detail="That account is not on this bank connection"
        )
    database = _database(request)
    actual_account_id = payload.actual_account_id
    _refuse_taken_plaid_account(database, external["id"], actual_account_id=actual_account_id)
    existing = database.get_bank_link(actual_account_id) if actual_account_id else None
    if (
        existing
        and existing["enabled"]
        and existing["external_account_id"] != payload.external_account_id
    ):
        raise HTTPException(
            status_code=409,
            detail="That Actual account is already mapped to a different bank account",
        )
    created_account: dict[str, Any] | None = None
    if payload.new_account is not None:
        created_account = await _gateway(request).create_account(
            payload.new_account.name, off_budget=payload.new_account.off_budget
        )
        actual_account_id = created_account["id"]
    try:
        link = database.upsert_bank_link(
            {
                "actual_account_id": actual_account_id,
                "provider": "plaid",
                "item_id": item["item_id"],
                "external_account_id": external["id"],
                "external_name": external["name"],
                "mask": external["mask"],
                "account_type": external["type"],
                "account_subtype": external["subtype"],
                "institution": item.get("institution_name") or "",
                "enabled": True,
                "cutover_date": payload.cutover_date
                or datetime.now(_settings_manager(request).get().zone).date().isoformat(),
                "last_error": "",
            }
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="That bank account is already mapped to another Actual account"
        ) from exc
    if created_account is not None:
        await _jobs(request).refresh_now()
    await _jobs(request).enqueue("health", trigger="manual")
    return {"link": _serialize_record(link), "created_account": created_account}


@app.patch("/api/plaid/links/{actual_account_id}")
async def plaid_update_link(
    actual_account_id: str, payload: UpdateLinkRequest, request: Request
) -> dict[str, Any]:
    database = _database(request)
    if not database.get_bank_link(actual_account_id):
        raise HTTPException(status_code=404, detail="Mapping not found")
    fields: dict[str, Any] = {}
    if payload.enabled is not None:
        fields["enabled"] = payload.enabled
    if payload.cutover_date is not None:
        fields["cutover_date"] = payload.cutover_date
    link = database.update_bank_link(actual_account_id, **fields)
    await _jobs(request).enqueue("health", trigger="manual")
    return {"link": _serialize_record(link or {})}


@app.delete("/api/plaid/links/{actual_account_id}")
async def plaid_delete_link(actual_account_id: str, request: Request) -> dict[str, Any]:
    """Forget a mapping. The Actual account and everything imported into it stay."""
    if not _database(request).delete_bank_link(actual_account_id):
        raise HTTPException(status_code=404, detail="Mapping not found")
    await _jobs(request).enqueue("health", trigger="manual")
    return {"deleted": True}


# ------------------------------------------------------------------ migration
#
# Moving one account's feed between providers, in either direction, with a
# dry run for every write. The state of an account is derived, never stored:
# what Actual links it to, and whether Clerk holds an enabled mapping.


def _feed_state(actual_source: str, link: dict[str, Any] | None) -> str:
    clerk = bool(link and link.get("enabled"))
    if clerk and actual_source:
        return "both"
    if clerk:
        return "plaid"
    if actual_source:
        return "simplefin" if actual_source == "simpleFin" else "actual"
    if link:
        return "plaid_paused"
    return "manual"


async def _simplefin_server(request: Request, *, with_accounts: bool) -> dict[str, Any]:
    gateway = _gateway(request)
    try:
        status_payload = await gateway.simplefin_server_status()
    except ActualGatewayError as exc:
        return {"configured": False, "error": str(exc), "accounts": []}
    result: dict[str, Any] = {
        "configured": bool(status_payload.get("configured")),
        "error": status_payload.get("error") or "",
        "accounts": [],
    }
    if with_accounts and result["configured"]:
        try:
            listing = await gateway.simplefin_server_accounts()
        except ActualGatewayError as exc:
            result["error"] = str(exc)
        else:
            result["accounts"] = listing.get("accounts") or []
            if listing.get("error"):
                result["error"] = f"{listing['error']} {listing.get('reason') or ''}".strip()
    return result


@app.get("/api/simplefin/server")
async def simplefin_server(request: Request, accounts: bool = Query(False)) -> dict[str, Any]:
    """Whether the Actual *server* holds a SimpleFIN token, and what it can see."""
    return await _simplefin_server(request, with_accounts=accounts)


@app.post("/api/simplefin/server-token")
async def set_simplefin_server_token(payload: ServerTokenRequest, request: Request) -> dict[str, Any]:
    """Store a SimpleFIN setup token on the Actual server, as Actual's own UI does."""
    token = "".join(payload.setup_token.split())
    await _gateway(request).set_server_secret("simplefin_token", token)
    return await _simplefin_server(request, with_accounts=False)


@app.delete("/api/simplefin/server-token")
async def clear_simplefin_server_token(request: Request) -> dict[str, Any]:
    """Remove the SimpleFIN token and derived access key from the Actual server.

    Accounts Actual still links to SimpleFIN stay linked; their next sync
    simply fails until a token is stored again.
    """
    gateway = _gateway(request)
    await gateway.set_server_secret("simplefin_token", None)
    with contextlib.suppress(ActualGatewayError):
        await gateway.set_server_secret("simplefin_accessKey", None)
    return await _simplefin_server(request, with_accounts=False)


@app.get("/api/migration")
async def migration_overview(request: Request) -> dict[str, Any]:
    """Every open Actual account, what feeds it, and what it could move to."""
    settings = _settings_manager(request).get()
    database = _database(request)
    gateway = _gateway(request)
    accounts = await gateway.list_accounts_detailed()
    links = {link["actual_account_id"]: link for link in database.list_bank_links()}
    health = {item["account_id"]: item for item in database.health_snapshots()}
    server = await _simplefin_server(request, with_accounts=True)
    server_accounts = {account["account_id"]: account for account in server["accounts"]}
    plaid: dict[str, Any] = {"configured": settings.plaid_configured, "items": []}
    if settings.plaid_configured:
        entries = await read_items(database, settings)
        plaid = {
            **describe(entries, list(links.values()), environment=settings.plaid_env,
                       items_ever=len(database.list_plaid_items(include_removed=True))),
            "configured": True,
        }
    rows = []
    for account in accounts:
        if account.get("closed"):
            continue
        link = links.get(account["id"])
        actual_source = account.get("sync_source") or ""
        remembered = (link or {}).get("previous_external_id") or ""
        simplefin_id = account.get("external_id") if actual_source == "simpleFin" else remembered
        rows.append(
            {
                "id": account["id"],
                "name": account["name"],
                "off_budget": account.get("off_budget", False),
                "actual_sync_source": actual_source,
                "actual_external_id": account.get("external_id") or "",
                "bank_name": account.get("bank_name") or "",
                "state": _feed_state(actual_source, link),
                "link": _serialize_record(link) if link else None,
                "health_status": (health.get(account["id"]) or {}).get("status", "unknown"),
                "simplefin_account": server_accounts.get(simplefin_id),
            }
        )
    return {
        "accounts": rows,
        "plaid": plaid,
        "simplefin_server": {k: v for k, v in server.items() if k != "accounts"},
        "simplefin_accounts": server["accounts"],
        "clerk_simplefin_configured": bool(settings.secret_value("simplefin_access_url")),
    }


@app.post("/api/migration/to-plaid")
async def migrate_to_plaid(payload: MigrateToPlaidRequest, request: Request) -> dict[str, Any]:
    """Feed an Actual account from Plaid; a dry run shows what the first delivery would do."""
    settings = _settings_manager(request).get()
    database = _database(request)
    gateway = _gateway(request)
    item = _plaid_item_or_404(request, payload.item_id)
    account = next(
        (a for a in await gateway.list_accounts_detailed() if a["id"] == payload.actual_account_id),
        None,
    )
    if account is None or account.get("closed"):
        raise HTTPException(status_code=404, detail="Actual account not found")
    client = _plaid_client(request)
    try:
        externals = (await client.get_accounts(item["access_token"]))["accounts"]
    finally:
        await client.close()
    external = next((a for a in externals if a["id"] == payload.external_account_id), None)
    if external is None:
        raise HTTPException(status_code=404, detail="That account is not on this bank connection")
    _refuse_taken_plaid_account(database, external["id"], actual_account_id=account["id"])
    existing = database.get_bank_link(account["id"])
    actual_source = account.get("sync_source") or ""
    cutover = payload.cutover_date or datetime.now(settings.zone).date().isoformat()
    link = {
        "actual_account_id": account["id"],
        "provider": "plaid",
        "item_id": item["item_id"],
        "external_account_id": external["id"],
        "external_name": external["name"],
        "mask": external["mask"],
        "account_type": external["type"],
        "account_subtype": external["subtype"],
        "institution": item.get("institution_name") or "",
        "enabled": True,
        "cutover_date": cutover,
        "last_error": "",
        "previous_provider": actual_source or (existing or {}).get("previous_provider") or "",
        "previous_external_id": (account.get("external_id") if actual_source else "")
        or (existing or {}).get("previous_external_id") or "",
    }
    will_unlink = bool(payload.unlink_actual and actual_source)
    if payload.dry_run:
        engine = PlaidSyncEngine(database, settings, gateway, today=datetime.now(settings.zone).date())
        preview = await engine.preview_link({**link, "last_import_at": None})
        return {
            "dry_run": True,
            "account": {"id": account["id"], "name": account["name"], "actual_sync_source": actual_source},
            "will_unlink_actual": will_unlink,
            "keeps_actual_link": bool(actual_source and not payload.unlink_actual),
            "preview": preview,
        }
    # The mapping is stored before Actual's own link is dropped: if the
    # second step fails, the account is fed twice (a state the Connections
    # page allows mid-migration) rather than by nobody.
    try:
        stored = database.upsert_bank_link(link)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="That bank account is already mapped to another Actual account") from exc
    if will_unlink:
        await gateway.unlink_account(account["id"])
    await _jobs(request).enqueue("sync", trigger="manual")
    return {
        "dry_run": False,
        "unlinked_actual": will_unlink,
        "link": _serialize_record(stored),
        "sync_queued": True,
    }


@app.post("/api/migration/to-simplefin")
async def migrate_to_simplefin(payload: MigrateToSimpleFinRequest, request: Request) -> dict[str, Any]:
    """Hand an account back to Actual's own SimpleFIN link, from a starting date."""
    settings = _settings_manager(request).get()
    database = _database(request)
    gateway = _gateway(request)
    account = next(
        (a for a in await gateway.list_accounts_detailed() if a["id"] == payload.actual_account_id),
        None,
    )
    if account is None or account.get("closed"):
        raise HTTPException(status_code=404, detail="Actual account not found")
    link = database.get_bank_link(account["id"])
    server = await _simplefin_server(request, with_accounts=True)
    warnings: list[str] = []
    if not server["configured"]:
        warnings.append("The Actual server holds no SimpleFIN token. Store one in Settings first.")
    if server.get("error"):
        warnings.append(f"The Actual server reported: {server['error']}")
    wanted = payload.simplefin_account_id or (link or {}).get("previous_external_id") or ""
    remote = next((a for a in server["accounts"] if a["account_id"] == wanted), None)
    if not wanted:
        warnings.append("Choose the SimpleFIN account this Actual account should follow.")
    elif remote is None and server["configured"]:
        warnings.append("SimpleFIN did not return that account. Check the SimpleFIN Bridge.")
    if account.get("sync_source") == "simpleFin" and account.get("external_id") == wanted:
        warnings.append("Actual already links this account to that SimpleFIN account.")
    starting = payload.starting_date or (link or {}).get("cutover_date") or datetime.now(settings.zone).date().isoformat()
    summary = {
        "account": {"id": account["id"], "name": account["name"], "actual_sync_source": account.get("sync_source") or ""},
        "simplefin_account": remote,
        "starting_date": starting,
        "will_pause_plaid_link": bool(link and link.get("enabled")),
        "warnings": warnings,
        "note": (
            "Actual imports from SimpleFIN from the starting date onward and matches rows "
            "Clerk delivered from Plaid by amount and date, so the overlap is adopted rather "
            "than duplicated."
        ),
    }
    if payload.dry_run:
        return {"dry_run": True, **summary}
    if warnings and (remote is None or not server["configured"]):
        raise HTTPException(status_code=409, detail=" ".join(warnings))
    if link and link.get("enabled"):
        database.update_bank_link(account["id"], enabled=False)
    linked = await gateway.link_simplefin_account(
        remote, account_id=account["id"], starting_date=datetime.fromisoformat(starting).date()
    )
    await _jobs(request).enqueue("sync", trigger="manual")
    return {"dry_run": False, **summary, "linked": linked, "sync_queued": True}


# ---------------------------------------------------- anticipated charges
#
# The phone companion forwards card-app notifications here. Everything under
# /api/anticipated/device/ is what the phone calls and is the only part of
# Clerk an outside device writes to, so it can be gated by a device token.
# The rest is the web UI's view of the same ledger. Nothing here writes to
# Actual: an anticipated charge lives in Clerk until the bank's own row
# arrives through the ordinary import and settles it.


# How long the phone's forward call waits for the budget to be re-read. The
# phone gives up reading a reply after 30 seconds and would retry a charge it
# has already delivered.
FORWARD_REFRESH_SECONDS = 20.0


def _log_refresh_outcome(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        log.warning("Budget re-read after an anticipated charge failed: %s", error)


def _require_device(request: Request) -> None:
    settings = _settings_manager(request).get()
    if not settings.anticipated_enabled:
        raise HTTPException(status_code=409, detail="Anticipated charges are turned off in Settings")
    expected = settings.secret_value("anticipated_device_token")
    if not expected:
        return
    header = request.headers.get("authorization", "")
    presented = header[7:].strip() if header.lower().startswith("bearer ") else ""
    presented = presented or request.headers.get("x-clerk-device-token", "").strip()
    # Compared as bytes: the str form of compare_digest refuses non-ASCII
    # text, which would turn a token with an accent into a 500 instead of a 401.
    if not presented or not secrets.compare_digest(presented.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="The device token is missing or wrong")


def _anticipated_accounts(request: Request) -> list[dict[str, Any]]:
    """Open Actual accounts a phone source can point at, from the last snapshot."""
    snapshot = _database(request).get_snapshot(OVERVIEW_SNAPSHOT) or {}
    return [
        {
            "id": account["id"],
            "name": account["name"],
            "off_budget": bool(account.get("off_budget")),
            "sync_source": account.get("sync_source") or "",
        }
        for account in snapshot.get("accounts") or []
        if not account.get("closed")
    ]


def _account_name(request: Request, account_id: str) -> str:
    return next((a["name"] for a in _anticipated_accounts(request) if a["id"] == account_id), "")


def _serialize_source(source: dict[str, Any]) -> dict[str, Any]:
    return _serialize_record({**source, "last_seen": source.get("last_seen_at")})


def _budget_summary(request: Request) -> dict[str, Any]:
    settings = _settings_manager(request).get()
    report = (_database(request).get_snapshot(OVERVIEW_SNAPSHOT) or {}).get("budget") or {}
    return {
        "configured": bool(report.get("configured")),
        "remaining_cents": int(report.get("remaining_cents", 0)),
        "available_cents": int(report.get("available_cents", 0)),
        "spent_cents": int(report.get("spent_cents", 0)),
        "anticipated_cents": int(report.get("anticipated_cents", 0)),
        "anticipated_count": int(report.get("anticipated_count", 0)),
        "daily_safe_to_spend_cents": int(report.get("daily_safe_to_spend_cents", 0)),
        "currency": settings.budget_currency,
        "month": report.get("month") or "",
    }


@app.get("/api/anticipated/device/hello")
async def anticipated_hello(request: Request, device_id: str = Query(default="")) -> dict[str, Any]:
    """The phone's one bootstrap call: proves the token, lists accounts and its sources."""
    _require_device(request)
    database = _database(request)
    sources = database.list_notification_sources(device_id=device_id) if device_id else []
    return {
        "ok": True,
        "version": __version__,
        "accounts": _anticipated_accounts(request),
        "sources": [_serialize_source(source) for source in sources],
        "budget": _budget_summary(request),
    }


@app.post("/api/anticipated/device/sources", status_code=status.HTTP_201_CREATED)
async def anticipated_register_source(
    payload: RegisterSourceRequest, request: Request
) -> dict[str, Any]:
    """Point one app's notifications on one phone at an Actual account."""
    _require_device(request)
    name = _account_name(request, payload.actual_account_id)
    if not name:
        raise HTTPException(status_code=404, detail="That Actual account is not in the last snapshot")
    database = _database(request)
    fields = payload.model_dump(exclude={"sample_posted_at_ms"})
    source = database.upsert_notification_source({**fields, "account_name": name, "enabled": True})
    # The notification picked at registration is almost always the purchase
    # that prompted it, and the listener only sees notifications posted from
    # now on, so the sample is forwarded on the phone's behalf.
    charge = None
    if payload.sample_title or payload.sample_text:
        row, created = anticipated.record_notification(
            database,
            _settings_manager(request).get(),
            source=source,
            posted_at_ms=payload.sample_posted_at_ms,
            title=payload.sample_title,
            text=payload.sample_text,
        )
        if created and row["status"] == anticipated.OPEN:
            with contextlib.suppress(Exception):
                await _jobs(request).refresh_now()
        charge = anticipated.public_charge(database.get_anticipated_charge(row["id"]) or row)
    return {"source": _serialize_source(source), "charge": charge}


@app.delete("/api/anticipated/device/sources/{source_id}")
async def anticipated_unregister_source(
    source_id: str, request: Request, device_id: str = Query(default="")
) -> dict[str, Any]:
    _require_device(request)
    database = _database(request)
    source = database.get_notification_source(source_id)
    if not source or (device_id and source["device_id"] != device_id):
        raise HTTPException(status_code=404, detail="Source not found")
    database.delete_notification_source(source_id)
    return {"deleted": True}


@app.post("/api/anticipated/device/notifications", status_code=status.HTTP_202_ACCEPTED)
async def anticipated_forward(payload: ForwardNotificationRequest, request: Request) -> dict[str, Any]:
    """One notification from the phone becomes at most one anticipated charge.

    Clerk reads the amount, the direction, and the merchant out of the text
    itself, so the phone never needs to know what a bank's wording looks like
    and the reading can improve without a new app build. The reply carries the
    budget as it now stands, which is what the phone shows in its own toast.
    """
    _require_device(request)
    database = _database(request)
    settings = _settings_manager(request).get()
    source = database.find_notification_source(payload.device_id, payload.package_name)
    if source is None:
        raise HTTPException(
            status_code=404,
            detail="This app is not registered as a source on this device; register it first",
        )
    if not source["enabled"]:
        return {"accepted": False, "reason": "source_disabled", "source_id": source["id"]}
    row, created = anticipated.record_notification(
        database,
        settings,
        source=source,
        posted_at_ms=payload.posted_at_ms,
        title=payload.title,
        text=payload.text,
        key=payload.notification_key,
    )
    refreshed = False
    refresh_error = ""
    if created and row["status"] == anticipated.OPEN:
        # The bank may already hold this charge (the phone was offline, or the
        # feed was quick). Reading the budget now settles it immediately and
        # gives the phone a figure that already reflects the charge. The read
        # is bounded so the phone never times out and retries a charge that
        # is already stored; a slow read carries on in the background and the
        # next hello shows its result.
        refresh = asyncio.ensure_future(_jobs(request).refresh_now())
        try:
            await asyncio.wait_for(asyncio.shield(refresh), timeout=FORWARD_REFRESH_SECONDS)
            refreshed = True
        except TimeoutError:
            refresh_error = "the budget is still being re-read"
            refresh.add_done_callback(_log_refresh_outcome)
        except ActualGatewayError as exc:
            refresh_error = str(exc)
        except Exception as exc:  # noqa: BLE001 - the charge is stored; the read is best effort
            refresh_error = f"{type(exc).__name__}: {exc}"
            log.warning("Anticipated charge stored but the budget could not be re-read: %s", exc)
    current = database.get_anticipated_charge(row["id"]) or row
    return {
        "accepted": True,
        "created": created,
        "charge": anticipated.public_charge(current),
        "refreshed": refreshed,
        "refresh_error": refresh_error,
        "budget": _budget_summary(request),
    }


@app.get("/api/anticipated/device/charges")
async def anticipated_device_charges(
    request: Request,
    device_id: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    _require_device(request)
    database = _database(request)
    sources = database.list_notification_sources(device_id=device_id) if device_id else []
    charges = database.list_anticipated_charges(
        source_ids=[source["id"] for source in sources], limit=limit
    )
    return {
        "charges": [anticipated.public_charge(charge) for charge in charges],
        "budget": _budget_summary(request),
    }


@app.get("/api/anticipated")
async def anticipated_overview(request: Request) -> dict[str, Any]:
    """Sources, what is still anticipated, and what was settled recently."""
    settings = _settings_manager(request).get()
    database = _database(request)
    return {
        "enabled": settings.anticipated_enabled,
        "token_configured": bool(settings.secret_value("anticipated_device_token")),
        "match_window_days": settings.anticipated_match_window_days,
        "expire_days": settings.anticipated_expire_days,
        "sources": [_serialize_source(s) for s in database.list_notification_sources()],
        "open": [
            anticipated.public_charge(c)
            for c in database.list_anticipated_charges(status=anticipated.OPEN, limit=200)
        ],
        "recent": [
            anticipated.public_charge(c)
            for c in database.list_anticipated_charges(limit=60)
            if c["status"] != anticipated.OPEN
        ],
        "accounts": _anticipated_accounts(request),
        "categories": [
            category
            for category in (database.get_snapshot(OVERVIEW_SNAPSHOT) or {}).get("categories") or []
            if not category.get("is_income")
        ],
        "aliases": [_serialize_record(alias) for alias in database.list_aliases()],
        "budget": _budget_summary(request),
    }


def _anticipated_charge_or_404(request: Request, charge_id: str) -> dict[str, Any]:
    row = _database(request).get_anticipated_charge(charge_id)
    if not row:
        raise HTTPException(status_code=404, detail="Anticipated charge not found")
    return row


@app.post("/api/anticipated/charges/{charge_id}/category")
async def anticipated_teach_category(
    charge_id: str, payload: TeachCategoryRequest, request: Request
) -> dict[str, Any]:
    """Name where this charge belongs; Clerk remembers it for the merchant."""
    database = _database(request)
    row = _anticipated_charge_or_404(request, charge_id)
    name = ""
    if payload.category_id:
        name = _category_name(database, payload.category_id)
        if not name:
            raise HTTPException(status_code=404, detail="That category is not in the last snapshot")
    rule = anticipated.teach_category(
        database, row, category_id=payload.category_id, category_name=name
    )
    with contextlib.suppress(Exception):
        await _jobs(request).refresh_now()
    return {
        "charge": anticipated.public_charge(database.get_anticipated_charge(charge_id) or row),
        "rule": _serialize_record(rule) if rule else None,
    }


@app.post("/api/anticipated/charges/{charge_id}/alias")
async def anticipated_teach_alias(
    charge_id: str, payload: TeachAliasRequest, request: Request
) -> dict[str, Any]:
    """Name the payee the bank posts this merchant as, so history and memory apply."""
    database = _database(request)
    row = _anticipated_charge_or_404(request, charge_id)
    alias = anticipated.teach_alias(database, row, payee=payload.payee)
    if alias is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "That pair cannot be an alias: one name is all decoration, they are already "
                "the same merchant, or other names already resolve to this one"
            ),
        )
    with contextlib.suppress(Exception):
        await _jobs(request).refresh_now()
    return {
        "alias": _serialize_record(alias),
        "charge": anticipated.public_charge(database.get_anticipated_charge(charge_id) or row),
    }


@app.delete("/api/anticipated/aliases/{alias_key}")
async def anticipated_delete_alias(alias_key: str, request: Request) -> dict[str, Any]:
    if not _database(request).delete_alias(alias_key):
        raise HTTPException(status_code=404, detail="Alias not found")
    with contextlib.suppress(Exception):
        await _jobs(request).refresh_now()
    return {"deleted": True}


@app.patch("/api/anticipated/sources/{source_id}")
async def anticipated_update_source(
    source_id: str, payload: UpdateSourceRequest, request: Request
) -> dict[str, Any]:
    database = _database(request)
    if not database.get_notification_source(source_id):
        raise HTTPException(status_code=404, detail="Source not found")
    fields: dict[str, Any] = {}
    if payload.actual_account_id is not None:
        name = _account_name(request, payload.actual_account_id)
        if not name:
            raise HTTPException(status_code=404, detail="That Actual account is not in the last snapshot")
        fields["actual_account_id"] = payload.actual_account_id
        fields["account_name"] = name
    if payload.enabled is not None:
        fields["enabled"] = payload.enabled
    source = database.update_notification_source(source_id, **fields)
    with contextlib.suppress(Exception):
        await _jobs(request).refresh_now()
    return {"source": _serialize_source(source or {})}


@app.delete("/api/anticipated/sources/{source_id}")
async def anticipated_delete_source(source_id: str, request: Request) -> dict[str, Any]:
    if not _database(request).delete_notification_source(source_id):
        raise HTTPException(status_code=404, detail="Source not found")
    with contextlib.suppress(Exception):
        await _jobs(request).refresh_now()
    return {"deleted": True}


@app.post("/api/anticipated/charges/{charge_id}/dismiss")
async def anticipated_dismiss(charge_id: str, request: Request) -> dict[str, Any]:
    """Stop counting one anticipation without waiting for it to expire."""
    database = _database(request)
    if not database.get_anticipated_charge(charge_id):
        raise HTTPException(status_code=404, detail="Anticipated charge not found")
    if not database.resolve_anticipated_charge(
        charge_id, anticipated.DISMISSED, match_reason="dismissed by hand"
    ):
        raise HTTPException(status_code=409, detail="This charge is no longer open")
    with contextlib.suppress(Exception):
        await _jobs(request).refresh_now()
    return {"status": anticipated.DISMISSED}


@app.post("/api/anticipated/charges/{charge_id}/reopen")
async def anticipated_reopen(charge_id: str, request: Request) -> dict[str, Any]:
    """Count a settled or dismissed anticipation again, for a match that was wrong."""
    database = _database(request)
    if not database.get_anticipated_charge(charge_id):
        raise HTTPException(status_code=404, detail="Anticipated charge not found")
    if not database.reopen_anticipated_charge(charge_id):
        raise HTTPException(status_code=409, detail="This charge is already open, or was never read")
    with contextlib.suppress(Exception):
        await _jobs(request).refresh_now()
    return {"status": anticipated.OPEN}


# ------------------------------------------------------------------- settings


@app.get("/api/settings")
async def get_settings(request: Request) -> dict[str, Any]:
    return _settings_manager(request).get().public_dict()


@app.patch("/api/settings")
async def update_settings(payload: SettingsPatch, request: Request) -> dict[str, Any]:
    before = _settings_manager(request).get()
    try:
        updated = _settings_manager(request).update(payload.values)
    except ValidationError as exc:
        # The whole form is rejected when any one field is, so the message has
        # to name the field: a pydantic dump in a toast tells the reader
        # nothing about which box to go back and fix.
        raise HTTPException(status_code=422, detail=_field_errors(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    restart_fields = {"log_level"}
    restart_required = sorted(
        field
        for field in restart_fields & payload.values.keys()
        if getattr(before, field) != getattr(updated, field)
    )
    _jobs(request).settings_changed()
    return {"settings": updated.public_dict(), "restart_required": restart_required}


@app.post("/api/settings/simplefin/claim")
async def claim_simplefin(payload: ClaimSetupTokenRequest, request: Request) -> dict[str, Any]:
    """Exchange a SimpleFIN setup token for an access URL and store it."""
    try:
        access_url = await SimpleFinClient.claim(payload.setup_token)
    except SimpleFinError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    manager = _settings_manager(request)
    try:
        manager.update({"simplefin_access_url": access_url, "simplefin_setup_token": ""})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await _jobs(request).enqueue("health", trigger="manual")
    return {"claimed": True, "settings": manager.get().public_dict()}


@app.post("/api/settings/test/{target}")
async def test_settings(target: str, request: Request) -> dict[str, Any]:
    settings = _settings_manager(request).get()
    try:
        if target == "actual":
            return await _gateway(request).test_connection()
        if target == "simplefin":
            client: Any = SimpleFinClient(settings)
        elif target == "plaid":
            client = PlaidClient(settings)
        elif target == "model":
            client = OpenAICompatibleClient(settings)
        elif target == "notifications":
            client = NtfyClient(settings)
        else:
            raise HTTPException(status_code=404, detail="Unknown connection target")
        try:
            return await client.test_connection()
        finally:
            await client.close()
    except (ActualGatewayError, SimpleFinError, PlaidError, ModelError, NotificationError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/assets/{asset_path:path}", include_in_schema=False)
async def browser_asset(asset_path: str, request: Request) -> Response:
    content = STATIC_ASSETS.get(asset_path)
    if content is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    etag = f'"{hashlib.sha256(content).hexdigest()}"'
    requested_etags = request.headers.get("if-none-match", "").split(",")
    headers = {"Cache-Control": "no-cache", "ETag": etag}
    if any(tag.strip().removeprefix("W/") == etag for tag in requested_etags):
        return Response(status_code=304, headers=headers)
    media_type = mimetypes.guess_type(asset_path)[0] or "application/octet-stream"
    return Response(content=content, media_type=media_type, headers=headers)


@app.get("/{path:path}", include_in_schema=False)
async def single_page_app(path: str) -> Response:
    if path.startswith("api/"):
        raise HTTPException(status_code=404, detail="API endpoint not found")
    return HTMLResponse(INDEX_HTML, headers={"Cache-Control": "no-cache"})


def run() -> None:
    uvicorn.run(
        "actual_clerk.main:app",
        host=os.environ.get("CLERK_HOST", "0.0.0.0"),
        port=int(os.environ.get("CLERK_PORT", "8080")),
        proxy_headers=True,
    )
