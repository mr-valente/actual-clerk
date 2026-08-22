from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from actual_clerk import __version__
from actual_clerk.clients.actual import ActualGateway, ActualGatewayError
from actual_clerk.clients.ntfy import NotificationError, NtfyClient
from actual_clerk.clients.openai_compatible import ModelError, OpenAICompatibleClient
from actual_clerk.clients.simplefin import SimpleFinClient, SimpleFinError
from actual_clerk.config import SettingsManager, data_directory
from actual_clerk.db import Database
from actual_clerk.processing import OVERVIEW_SNAPSHOT, JobManager, ProcessingError
from actual_clerk.schemas import (
    ClaimSetupTokenRequest,
    CreateCategoryRequest,
    EnqueueRequest,
    MonitoringRequest,
    ResolveDecisionRequest,
    ResolveRuleRequest,
    SettingsPatch,
)

log = logging.getLogger(__name__)
STATIC_DIRECTORY = Path(__file__).parent / "static"


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
    for field in ("created_at", "resolved_at", "checked_at", "since", "last_seen"):
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
async def private_api_cache_control(request: Request, call_next: Any) -> Response:
    response = await call_next(request)
    if request.url.path.startswith("/api/") and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-store"
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


@app.get("/api/rules")
async def rule_suggestions(request: Request) -> list[dict[str, Any]]:
    return [
        _serialize_record(item) for item in _database(request).list_rule_suggestions(limit=100)
    ]


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
    params = {"full": True} if payload.full and payload.kind == "categorize" else None
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

    # Claim first: a second click must not produce a second write.
    claimed = database.resolve_decision(decision_id, "applied")
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
    return {"status": "applied", "category_id": category_id, "category_name": category_name}


def _category_name(database: Database, category_id: str) -> str:
    snapshot = database.get_snapshot(OVERVIEW_SNAPSHOT) or {}
    for category in snapshot.get("categories", []):
        if category["id"] == category_id:
            return category["name"]
    return ""


@app.post("/api/rules/{rule_id}/resolve")
async def resolve_rule(
    rule_id: str, payload: ResolveRuleRequest, request: Request
) -> dict[str, Any]:
    database = _database(request)
    if payload.action == "decline":
        database.resolve_rule_suggestion(rule_id, "declined")
        return {"status": "declined"}
    suggestion = database.claim_rule_suggestion(rule_id)
    if not suggestion:
        raise HTTPException(status_code=409, detail="This suggestion has already been resolved")
    try:
        created = await _gateway(request).create_category_rule(
            match_value=suggestion["match_value"],
            category_id=suggestion["category_id"],
            run_immediately=False,
        )
    except ActualGatewayError:
        database.resolve_rule_suggestion(rule_id, "suggested")
        raise
    database.resolve_rule_suggestion(rule_id, "created")
    return {"status": "created", "rule": created}


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


# ------------------------------------------------------------------- settings


@app.get("/api/settings")
async def get_settings(request: Request) -> dict[str, Any]:
    return _settings_manager(request).get().public_dict()


@app.patch("/api/settings")
async def update_settings(payload: SettingsPatch, request: Request) -> dict[str, Any]:
    before = _settings_manager(request).get()
    try:
        updated = _settings_manager(request).update(payload.values)
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
    except (ActualGatewayError, SimpleFinError, ModelError, NotificationError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


app.mount("/assets", StaticFiles(directory=STATIC_DIRECTORY), name="assets")


@app.get("/{path:path}", include_in_schema=False)
async def single_page_app(path: str) -> FileResponse:
    if path.startswith("api/"):
        raise HTTPException(status_code=404, detail="API endpoint not found")
    return FileResponse(STATIC_DIRECTORY / "index.html", headers={"Cache-Control": "no-cache"})


def run() -> None:
    uvicorn.run(
        "actual_clerk.main:app",
        host=os.environ.get("CLERK_HOST", "0.0.0.0"),
        port=int(os.environ.get("CLERK_PORT", "8080")),
        proxy_headers=True,
    )
