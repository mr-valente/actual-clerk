"""The single owner of Clerk's connection to the official Actual API.

Actual's supported API is a Node library, not an HTTP service. Clerk keeps one
long-lived Node worker beside the Python application and speaks a deliberately
small, line-delimited JSON protocol to it. The worker owns one private budget
cache and every operation is serialized through this gateway, so neither the
SQLite file nor Actual's in-process state ever has two writers.

Everything returned from here is a plain dictionary. The rest of Clerk does
not know which runtime Actual uses and remains straightforward to unit test.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import fcntl
import hashlib
import json
import logging
import os
from collections import deque
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from actual_clerk.config import Settings
from actual_clerk.domain.merchants import merchant_label, normalize_merchant

log = logging.getLogger(__name__)

CONNECTION_FIELDS = (
    "actual_url",
    "actual_password",
    "actual_budget_id",
    "actual_encryption_password",
    "actual_verify_ssl",
)
RPC_PREFIX = "@@actual-clerk-rpc@@"
# A snapshot is one framed JSON response and can easily exceed asyncio's
# 64 KiB subprocess stream default. Keep a finite ceiling, but size it for a
# large retained transaction history rather than a terminal-sized log line.
RPC_STREAM_LIMIT_BYTES = 64 * 1024 * 1024


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


def _parse_date(value: Any) -> datetime.date | None:
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _normalize_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """Restore Python date types and Clerk's derived merchant fields."""
    snapshot = dict(payload)
    accounts = []
    for raw in payload.get("accounts") or []:
        account = dict(raw)
        account["last_sync"] = _parse_timestamp(account.get("last_sync"))
        account["last_transaction_date"] = _parse_date(account.get("last_transaction_date"))
        account["unconfirmed_transfers"] = [
            {**item, "date": date}
            for item in account.get("unconfirmed_transfers") or []
            if (date := _parse_date(item.get("date"))) is not None
        ]
        accounts.append(account)
    snapshot["accounts"] = accounts

    transactions = []
    for raw in payload.get("transactions") or []:
        transaction = dict(raw)
        date = _parse_date(transaction.get("date"))
        if date is None:
            continue
        transaction["date"] = date
        payee = str(transaction.get("payee_name") or "")
        description = str(transaction.get("imported_description") or "")
        transaction["merchant_key"] = normalize_merchant(payee, description)
        transaction["merchant_label"] = merchant_label(payee, description)
        transactions.append(transaction)
    snapshot["transactions"] = transactions

    income_history = []
    for row in payload.get("income_history") or []:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            continue
        date = _parse_date(row[0])
        if date is not None:
            income_history.append((date, int(row[1] or 0)))
    snapshot["income_history"] = income_history
    snapshot["collected_at"] = _parse_timestamp(
        payload.get("collected_at")
    ) or datetime.datetime.now(datetime.UTC)
    snapshot["history_start"] = _parse_date(payload.get("history_start")) or datetime.date.min
    return snapshot


class ActualGateway:
    """Serialize access to one long-lived ``@actual-app/api`` worker."""

    def __init__(
        self,
        settings_manager: Any,
        data_dir: Path,
        *,
        node_executable: str | None = None,
        worker_path: Path | None = None,
    ):
        self._settings_manager = settings_manager
        self._data_dir = Path(data_dir)
        self._node_executable = node_executable or os.environ.get("ACTUAL_CLERK_NODE") or "node"
        self._worker_path = worker_path or Path(__file__).parents[1] / "actual_worker.mjs"
        self._lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._cache_lock_fd: int | None = None
        self._fingerprint: tuple[Any, ...] | None = None
        self._restart_requested = False
        self._request_id = 0
        self._last_error = ""
        self._last_connected_at: datetime.datetime | None = None
        self._worker_info: dict[str, Any] = {}
        self._closed = False

    @staticmethod
    def _connection_fingerprint(settings: Settings) -> tuple[Any, ...]:
        return tuple(
            settings.secret_value(field) if field.endswith("password") else getattr(settings, field)
            for field in CONNECTION_FIELDS
        )

    @property
    def connected(self) -> bool:
        return bool(
            self._process is not None
            and self._process.returncode is None
            and self._fingerprint is not None
            and not self._restart_requested
        )

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "last_error": self._last_error,
            "last_connected_at": (
                self._last_connected_at.isoformat() if self._last_connected_at else None
            ),
            "api_version": self._worker_info.get("apiVersion", ""),
            "server_version": self._worker_info.get("serverVersion", ""),
        }

    def settings_changed(self) -> None:
        settings = self._settings_manager.get()
        if self._fingerprint is not None and self._fingerprint != self._connection_fingerprint(
            settings
        ):
            self._restart_requested = True

    def _acquire_cache_lock(self) -> None:
        if self._cache_lock_fd is not None:
            return
        self._data_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self._data_dir / "actual-api.lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise ActualGatewayError(
                "Another Actual Clerk process already owns this Actual API cache"
            ) from exc
        self._cache_lock_fd = descriptor

    def _release_cache_lock(self) -> None:
        if self._cache_lock_fd is None:
            return
        fcntl.flock(self._cache_lock_fd, fcntl.LOCK_UN)
        os.close(self._cache_lock_fd)
        self._cache_lock_fd = None

    def _cache_directory(self, settings: Settings) -> Path:
        identity = f"{settings.actual_url.rstrip('/')}\0{settings.actual_budget_id}".encode()
        suffix = hashlib.sha256(identity).hexdigest()[:16]
        return self._data_dir / "actual-api" / suffix

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        while line := await stream.readline():
            message = line.decode(errors="replace").rstrip()
            if message:
                self._stderr_tail.append(message)
                log.debug("Actual API worker: %s", message)

    async def _start_worker(self, settings: Settings) -> None:
        if self._closed:
            raise ActualGatewayError("The Actual gateway is shutting down")
        password = settings.secret_value("actual_password")
        if not password:
            raise ActualGatewayError("An Actual server password is not configured")
        if not settings.actual_budget_id:
            raise ActualGatewayError("An Actual budget (sync ID) is not configured")

        self._acquire_cache_lock()
        cache = self._cache_directory(settings)
        cache.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        if settings.actual_verify_ssl:
            environment.pop("NODE_TLS_REJECT_UNAUTHORIZED", None)
        else:
            # The worker talks only to Actual. Node has no supported per-fetch
            # CA bypass, so the existing setting is scoped to this child.
            environment["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
        try:
            self._process = await asyncio.create_subprocess_exec(
                self._node_executable,
                str(self._worker_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
                limit=RPC_STREAM_LIMIT_BYTES,
            )
        except (FileNotFoundError, OSError) as exc:
            raise ActualGatewayError(
                f"Could not start the official Actual API worker: {exc}"
            ) from exc
        assert self._process.stderr is not None
        self._stderr_task = asyncio.create_task(self._drain_stderr(self._process.stderr))
        try:
            self._worker_info = await self._rpc_unlocked(
                "initialize",
                {
                    "serverUrl": settings.actual_url.rstrip("/"),
                    "password": password,
                    "syncId": settings.actual_budget_id,
                    "encryptionPassword": settings.secret_value("actual_encryption_password"),
                    "dataDir": str(cache),
                },
                request_timeout=float(settings.request_timeout_seconds),
            )
        except Exception:
            await self._stop_worker(graceful=False)
            raise
        self._fingerprint = self._connection_fingerprint(settings)
        self._restart_requested = False
        self._last_connected_at = datetime.datetime.now(datetime.UTC)
        self._last_error = ""

    async def _stop_worker(self, *, graceful: bool) -> None:
        process, self._process = self._process, None
        self._fingerprint = None
        self._worker_info = {}
        if process is not None and process.returncode is None:
            if graceful:
                with contextlib.suppress(Exception):
                    await self._rpc_to_process(
                        process, "shutdown", {}, request_timeout=10.0
                    )
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                process.kill()
                await process.wait()
        if self._stderr_task is not None:
            await asyncio.gather(self._stderr_task, return_exceptions=True)
            self._stderr_task = None

    async def _rpc_to_process(
        self,
        process: asyncio.subprocess.Process,
        method: str,
        params: dict[str, Any],
        *,
        request_timeout: float,
    ) -> Any:
        if process.returncode is not None or process.stdin is None or process.stdout is None:
            detail = self._stderr_tail[-1] if self._stderr_tail else "the worker exited"
            raise ActualGatewayError(f"Official Actual API worker stopped: {detail}", retryable=True)
        self._request_id += 1
        request_id = self._request_id
        message = json.dumps(
            {"id": request_id, "method": method, "params": params}, separators=(",", ":")
        )
        process.stdin.write(f"{message}\n".encode())
        await process.stdin.drain()

        async def _read_response() -> dict[str, Any]:
            while line := await process.stdout.readline():
                decoded = line.decode(errors="replace").rstrip()
                if not decoded.startswith(RPC_PREFIX):
                    if decoded:
                        log.debug("Ignoring unframed Actual API output: %s", decoded)
                    continue
                response = json.loads(decoded.removeprefix(RPC_PREFIX))
                if response.get("id") == request_id:
                    return response
            detail = self._stderr_tail[-1] if self._stderr_tail else "no error output"
            raise ActualGatewayError(
                f"Official Actual API worker exited before replying: {detail}", retryable=True
            )

        try:
            response = await asyncio.wait_for(_read_response(), timeout=request_timeout)
        except TimeoutError as exc:
            raise ActualGatewayError(
                f"Official Actual API request timed out after {request_timeout:g} seconds",
                retryable=True,
            ) from exc
        if not response.get("ok"):
            error = response.get("error") or {}
            message = str(error.get("message") or "Unknown Actual API failure")
            code = str(error.get("code") or "")
            suffix = f" ({code})" if code and code not in message else ""
            raise ActualGatewayError(f"Actual rejected the request: {message}{suffix}", retryable=True)
        return response.get("result")

    async def _rpc_unlocked(
        self, method: str, params: dict[str, Any], *, request_timeout: float
    ) -> Any:
        if self._process is None:
            raise ActualGatewayError("Official Actual API worker is not running", retryable=True)
        return await self._rpc_to_process(
            self._process, method, params, request_timeout=request_timeout
        )

    async def _call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if self._closed:
            raise ActualGatewayError("The Actual gateway is shutting down")
        async with self._lock:
            settings = self._settings_manager.get()
            fingerprint = self._connection_fingerprint(settings)
            if self._restart_requested or (
                self._fingerprint is not None and self._fingerprint != fingerprint
            ):
                await self._stop_worker(graceful=True)
            try:
                if self._process is None or self._process.returncode is not None:
                    await self._start_worker(settings)
                return await self._rpc_unlocked(
                    method,
                    params or {},
                    request_timeout=float(settings.request_timeout_seconds),
                )
            except ActualGatewayError as exc:
                self._last_error = str(exc)
                await self._stop_worker(graceful=False)
                raise
            except Exception as exc:  # noqa: BLE001 - protocol failures invalidate the worker
                self._last_error = str(exc)
                await self._stop_worker(graceful=False)
                raise ActualGatewayError(
                    f"Official Actual API request failed: {exc}", retryable=True
                ) from exc

    async def close(self) -> None:
        self._closed = True
        async with self._lock:
            await self._stop_worker(graceful=True)
            self._release_cache_lock()

    async def snapshot(self, *, today: datetime.date | None = None) -> dict[str, Any]:
        settings = self._settings_manager.get()
        day = today or datetime.datetime.now(settings.zone).date()
        payload = await self._call(
            "snapshot",
            {"today": day.isoformat(), "historyLookbackDays": settings.history_lookback_days},
        )
        return _normalize_snapshot(payload)

    async def test_connection(self) -> dict[str, Any]:
        return await self._call("testConnection")

    async def bank_sync(self, *, run_rules: bool = True) -> dict[str, Any]:
        # Native bank sync always runs Actual's own reconciliation and rules.
        # The argument remains for compatibility with the processing contract.
        del run_rules
        return await self._call("bankSync")

    async def pull(self) -> dict[str, Any]:
        return await self._call("pull")

    async def apply_updates(
        self, updates: Sequence[dict[str, Any]], *, overwrite: bool = False
    ) -> dict[str, Any]:
        if not updates:
            return {"applied": [], "skipped": []}
        return await self._call(
            "applyUpdates", {"updates": list(updates), "overwrite": overwrite}
        )

    async def ensure_tags(self, catalog: Sequence[dict[str, Any]]) -> list[str]:
        if not catalog:
            return []
        return await self._call("ensureTags", {"catalog": list(catalog)})

    async def create_category(self, name: str, group_name: str) -> dict[str, Any]:
        return await self._call("createCategory", {"name": name, "groupName": group_name})

    async def create_category_rule(
        self, *, match_value: str, category_id: str, run_immediately: bool = False
    ) -> dict[str, Any]:
        return await self._call(
            "createCategoryRule",
            {
                "matchValue": match_value,
                "categoryId": category_id,
                "runImmediately": run_immediately,
            },
        )

    async def find_account(self, name: str) -> dict[str, Any] | None:
        return await self._call("findAccount", {"name": name})

    async def diagnostics(self) -> dict[str, Any]:
        return await self._call("diagnostics")
