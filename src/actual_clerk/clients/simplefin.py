"""A direct SimpleFIN client, used to verify what Actual cannot see.

Actual runs its own SimpleFIN sync and records the result, but it does not
surface a broken connection anywhere obvious. Reading SimpleFIN directly gives
Clerk the one thing the budget file lacks: the bank's own opinion, right now, of
whether each account is still reachable.

The protocol is small. A setup token is a base64 claim URL that is POSTed once
and exchanged for an access URL carrying HTTP basic-auth credentials; every
later request is `GET {access_url}/accounts`.
"""

from __future__ import annotations

import base64
import binascii
import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import unquote, urlparse, urlunparse

import httpx

from actual_clerk.config import Settings

CENTS = Decimal(100)


class SimpleFinError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False, status_code: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


def _to_cents(value: Any) -> int:
    """SimpleFIN sends amounts as decimal strings to avoid float drift."""
    if value is None:
        return 0
    try:
        return int((Decimal(str(value)) * CENTS).to_integral_value())
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise SimpleFinError(f"SimpleFIN returned an unreadable amount: {value!r}") from exc


def _to_datetime(value: Any) -> datetime.datetime | None:
    if value in (None, "", 0):
        return None
    try:
        return datetime.datetime.fromtimestamp(int(value), datetime.UTC)
    except (TypeError, ValueError, OSError):
        return None


def decode_setup_token(token: str) -> str:
    """Recover the one-time claim URL from a base64 setup token."""
    cleaned = "".join(str(token).split())
    if not cleaned:
        raise SimpleFinError("A SimpleFIN setup token is required")
    if cleaned.startswith(("http://", "https://")):
        claim_url = cleaned
    else:
        padded = cleaned + "=" * (-len(cleaned) % 4)
        try:
            claim_url = base64.b64decode(padded).decode("utf-8").strip()
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise SimpleFinError("The SimpleFIN setup token is not valid base64") from exc
    parsed = urlparse(claim_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise SimpleFinError("A SimpleFIN claim URL must be an https URL")
    return claim_url


def split_access_url(access_url: str) -> tuple[str, tuple[str, str] | None]:
    """Separate the request URL from the basic-auth credentials inside it.

    Credentials are pulled out rather than left in the URL so they never reach a
    log line, an error message, or a redirect target.
    """

    parsed = urlparse(access_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SimpleFinError("The SimpleFIN access URL must be an http(s) URL")
    auth: tuple[str, str] | None = None
    if parsed.username:
        auth = (unquote(parsed.username), unquote(parsed.password or ""))
    host = parsed.hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    sanitized = urlunparse((parsed.scheme, host, parsed.path.rstrip("/"), "", "", ""))
    return sanitized, auth


class SimpleFinClient:
    def __init__(self, settings: Settings):
        self.access_url = settings.secret_value("simplefin_access_url").strip()
        self.timeout = min(settings.request_timeout_seconds, 120)
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            headers={"Accept": "application/json"},
            follow_redirects=False,
        )

    @property
    def configured(self) -> bool:
        return bool(self.access_url)

    async def close(self) -> None:
        await self.client.aclose()

    @staticmethod
    async def claim(setup_token: str, *, request_timeout: float = 60.0) -> str:
        """Exchange a setup token for a durable access URL. Works exactly once."""
        claim_url = decode_setup_token(setup_token)
        async with httpx.AsyncClient(timeout=httpx.Timeout(request_timeout)) as client:
            try:
                response = await client.post(claim_url, content=b"")
            except httpx.RequestError as exc:
                raise SimpleFinError(f"SimpleFIN claim request failed: {exc}", retryable=True) from exc
        if response.status_code == 403:
            raise SimpleFinError(
                "SimpleFIN rejected this setup token. A token can only be claimed once, so "
                "generate a new one if the previous claim was not saved.",
                status_code=403,
            )
        if response.status_code >= 400:
            raise SimpleFinError(
                f"SimpleFIN claim returned {response.status_code}", status_code=response.status_code
            )
        access_url = response.text.strip()
        # Validate before storing so a bad response cannot be saved as a secret.
        split_access_url(access_url)
        return access_url

    async def _get_accounts(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            raise SimpleFinError("A SimpleFIN access URL is not configured")
        base, auth = split_access_url(self.access_url)
        try:
            response = await self.client.get(f"{base}/accounts", params=params, auth=auth)
        except httpx.RequestError as exc:
            raise SimpleFinError(f"SimpleFIN request failed: {exc}", retryable=True) from exc
        if response.status_code == 403:
            raise SimpleFinError(
                "SimpleFIN rejected Clerk's credentials. The access URL was revoked or is wrong.",
                status_code=403,
            )
        if response.status_code == 402:
            raise SimpleFinError(
                "SimpleFIN reports that payment is required for this bridge account.",
                status_code=402,
            )
        if response.status_code >= 400:
            retryable = response.status_code in {408, 425, 429, 500, 502, 503, 504}
            raise SimpleFinError(
                f"SimpleFIN returned {response.status_code}",
                retryable=retryable,
                status_code=response.status_code,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise SimpleFinError("SimpleFIN returned a response that was not JSON") from exc
        if not isinstance(body, dict):
            raise SimpleFinError("SimpleFIN returned an unexpected response shape")
        return body

    async def fetch(
        self,
        *,
        balances_only: bool = True,
        start_date: datetime.date | None = None,
        pending: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if balances_only:
            params["balances-only"] = 1
        if start_date is not None:
            params["start-date"] = int(
                datetime.datetime.combine(
                    start_date, datetime.time.min, tzinfo=datetime.UTC
                ).timestamp()
            )
        if pending:
            params["pending"] = 1
        return parse_account_set(await self._get_accounts(params))

    async def test_connection(self) -> dict[str, Any]:
        result = await self.fetch(balances_only=True)
        return {
            "ok": True,
            "message": (
                f"{len(result['accounts'])} account(s) visible"
                + (f", {len(result['errors'])} reported problem(s)" if result["errors"] else "")
            ),
            "accounts": len(result["accounts"]),
            "errors": result["errors"],
        }


def parse_account_set(body: dict[str, Any]) -> dict[str, Any]:
    """Normalize both protocol versions into one shape.

    Version 1 puts an `org` object on every account and a list of plain strings
    in `errors`. Version 2 replaces those with a flat `connections` list and
    structured `errlist` entries. Clerk reads whichever is present.
    """

    connections = {
        str(item.get("conn_id") or ""): item
        for item in body.get("connections") or []
        if isinstance(item, dict)
    }

    accounts: list[dict[str, Any]] = []
    for raw in body.get("accounts") or []:
        if not isinstance(raw, dict):
            continue
        org = raw.get("org") if isinstance(raw.get("org"), dict) else {}
        connection_id = str(raw.get("conn_id") or org.get("id") or "")
        connection = connections.get(connection_id, {})
        org_name = str(
            connection.get("name") or org.get("name") or org.get("domain") or ""
        ).strip()
        transactions = [item for item in raw.get("transactions") or [] if isinstance(item, dict)]
        posted_dates = [
            _to_datetime(item.get("posted") or item.get("transacted_at")) for item in transactions
        ]
        latest = max((item for item in posted_dates if item), default=None)
        available = raw.get("available-balance")
        accounts.append(
            {
                "id": str(raw.get("id") or ""),
                "name": str(raw.get("name") or "").strip(),
                "org_name": org_name,
                "connection_id": connection_id,
                "currency": str(raw.get("currency") or "USD"),
                "balance_cents": _to_cents(raw.get("balance")),
                "available_cents": _to_cents(available) if available is not None else None,
                "balance_date": _to_datetime(raw.get("balance-date")),
                "last_transaction_date": latest.date() if latest else None,
                "transaction_count": len(transactions),
            }
        )

    errors: list[dict[str, Any]] = []
    for item in body.get("errlist") or []:
        if isinstance(item, dict):
            errors.append(
                {
                    "code": str(item.get("code") or ""),
                    "message": str(item.get("msg") or item.get("message") or "").strip(),
                    "conn_id": str(item.get("conn_id") or ""),
                    "account_id": str(item.get("account_id") or ""),
                }
            )
    for item in body.get("errors") or []:
        if isinstance(item, str) and item.strip():
            errors.append({"code": "", "message": item.strip(), "conn_id": "", "account_id": ""})
        elif isinstance(item, dict):
            errors.append(
                {
                    "code": str(item.get("code") or ""),
                    "message": str(item.get("msg") or item.get("message") or "").strip(),
                    "conn_id": str(item.get("conn_id") or ""),
                    "account_id": str(item.get("account_id") or ""),
                }
            )
    return {"accounts": accounts, "errors": errors, "connections": list(connections.values())}
