"""A direct Plaid client, the bank feed Actual itself cannot read.

Plaid is the one provider Actual has no server-side support for, so Clerk
talks to it directly and delivers what arrives through Actual's own import.
This module is deliberately small and dependency-free: every call is one POST
of JSON with the client id and secret in headers, against a pinned API
version, and every answer is reduced to plain dictionaries in Clerk's shape.

Amounts follow Actual's conventions on the way out. Plaid reports a purchase
as a positive amount and a credit card balance as a positive amount owed;
Actual wants both negative.
"""

from __future__ import annotations

import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import httpx

from actual_clerk.config import Settings

PLAID_VERSION = "2020-09-14"
PLAID_HOSTS = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}
# Shown in the Link UI, at most 30 characters; also the stable, non-personal
# user id Link asks for. One Clerk instance is one user.
CLIENT_USER_ID = "actual-clerk"
# The sandbox institution that works without OAuth, and the test user whose
# transaction history keeps moving so a sync loop has something to do.
SANDBOX_INSTITUTION = "ins_109508"
SANDBOX_USERNAME = "user_transactions_dynamic"
SANDBOX_PASSWORD = "pass_good"

# Item errors that Link's update mode repairs. Anything else is either
# transient (retry) or a real fault (surface it).
REPAIR_ERROR_CODES = frozenset(
    {
        "ITEM_LOGIN_REQUIRED",
        "ACCESS_NOT_GRANTED",
        "INVALID_CREDENTIALS",
        "INSUFFICIENT_CREDENTIALS",
        "PENDING_EXPIRATION",
        "PENDING_DISCONNECT",
        "USER_SETUP_REQUIRED",
        "MFA_NOT_SUPPORTED",
        "INVALID_MFA",
    }
)
RETRYABLE_ERROR_TYPES = frozenset({"API_ERROR", "RATE_LIMIT_EXCEEDED", "INSTITUTION_ERROR"})
# Plaid reports what is owed on these as a positive number.
LIABILITY_TYPES = frozenset({"credit", "loan"})

CENTS = Decimal(100)


class PlaidError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_type: str = "",
        error_code: str = "",
        display_message: str = "",
        request_id: str = "",
        status_code: int | None = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.error_type = error_type
        self.error_code = error_code
        self.display_message = display_message
        self.request_id = request_id
        self.status_code = status_code
        self.retryable = retryable

    @property
    def needs_repair(self) -> bool:
        return self.error_code in REPAIR_ERROR_CODES

    def as_dict(self) -> dict[str, Any]:
        return {
            "message": str(self),
            "error_type": self.error_type,
            "error_code": self.error_code,
            "display_message": self.display_message,
            "request_id": self.request_id,
            "needs_repair": self.needs_repair,
        }


def to_cents(value: Any) -> int | None:
    """Plaid sends JSON numbers; go through Decimal so 12.34 never becomes 1233."""
    if value is None or value == "":
        return None
    try:
        return int((Decimal(str(value)) * CENTS).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise PlaidError(f"Plaid returned an unreadable amount: {value!r}") from exc


def parse_timestamp(value: Any) -> datetime.datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.UTC)


def normalize_account(raw: dict[str, Any], *, item_id: str = "") -> dict[str, Any]:
    """One Plaid account in Clerk's shape, balances in Actual's sign convention."""

    balances = raw.get("balances") if isinstance(raw.get("balances"), dict) else {}
    account_type = str(raw.get("type") or "")
    sign = -1 if account_type in LIABILITY_TYPES else 1
    current = to_cents(balances.get("current"))
    available = to_cents(balances.get("available"))
    limit = to_cents(balances.get("limit"))
    return {
        "id": str(raw.get("account_id") or ""),
        "item_id": item_id,
        "name": str(raw.get("name") or "").strip(),
        "official_name": str(raw.get("official_name") or "").strip(),
        "mask": str(raw.get("mask") or ""),
        "type": account_type,
        "subtype": str(raw.get("subtype") or ""),
        "currency": str(
            balances.get("iso_currency_code") or balances.get("unofficial_currency_code") or "USD"
        ),
        "balance_cents": None if current is None else sign * current,
        "available_cents": None if available is None else sign * available,
        "limit_cents": limit,
        "balance_updated": parse_timestamp(balances.get("last_updated_datetime")),
    }


def normalize_item(raw: dict[str, Any], status: dict[str, Any] | None = None) -> dict[str, Any]:
    """The `item` object of /item/get (or /accounts/get), plus its update status."""

    error = raw.get("error") if isinstance(raw.get("error"), dict) else None
    transactions_status = (
        (status or {}).get("transactions") if isinstance((status or {}).get("transactions"), dict)
        else {}
    )
    return {
        "item_id": str(raw.get("item_id") or ""),
        "institution_id": str(raw.get("institution_id") or ""),
        "institution_name": str(raw.get("institution_name") or ""),
        "error": (
            {
                "error_type": str(error.get("error_type") or ""),
                "error_code": str(error.get("error_code") or ""),
                "error_message": str(error.get("error_message") or ""),
                "display_message": str(error.get("display_message") or ""),
                "needs_repair": str(error.get("error_code") or "") in REPAIR_ERROR_CODES,
            }
            if error and error.get("error_code")
            else None
        ),
        "consent_expiration_time": parse_timestamp(raw.get("consent_expiration_time")),
        "products": [str(item) for item in raw.get("products") or []],
        "billed_products": [str(item) for item in raw.get("billed_products") or []],
        "last_successful_update": parse_timestamp(
            transactions_status.get("last_successful_update")
        ),
        "last_failed_update": parse_timestamp(transactions_status.get("last_failed_update")),
    }


class PlaidClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.client_id = settings.plaid_client_id.strip()
        self.secret = settings.secret_value("plaid_secret").strip()
        self.environment = settings.plaid_env
        self.days_requested = settings.plaid_days_requested
        self.redirect_uri = settings.plaid_redirect_uri.strip()
        self.client_name = settings.plaid_client_name
        self.timeout = min(settings.request_timeout_seconds, 120)
        self.client = httpx.AsyncClient(
            base_url=PLAID_HOSTS[self.environment],
            timeout=httpx.Timeout(self.timeout),
            headers={
                "Content-Type": "application/json",
                "Plaid-Version": PLAID_VERSION,
                "PLAID-CLIENT-ID": self.client_id,
                "PLAID-SECRET": self.secret,
            },
            transport=transport,
        )

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.secret)

    @property
    def sandbox(self) -> bool:
        return self.environment == "sandbox"

    async def close(self) -> None:
        await self.client.aclose()

    async def _post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.configured:
            raise PlaidError("Plaid is not configured: a client id and secret are required")
        try:
            response = await self.client.post(path, json=body or {})
        except httpx.RequestError as exc:
            raise PlaidError(f"Plaid request failed: {exc}", retryable=True) from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if response.status_code >= 400 or payload.get("error_code"):
            error_type = str(payload.get("error_type") or "")
            error_code = str(payload.get("error_code") or "")
            message = str(payload.get("error_message") or "").strip()
            if not message:
                message = f"Plaid {path} returned HTTP {response.status_code}"
            raise PlaidError(
                f"Plaid {error_type or 'error'}/{error_code or response.status_code}: {message}"
                if error_type or error_code
                else message,
                error_type=error_type,
                error_code=error_code,
                display_message=str(payload.get("display_message") or ""),
                request_id=str(payload.get("request_id") or ""),
                status_code=response.status_code,
                retryable=error_type in RETRYABLE_ERROR_TYPES
                or response.status_code in {408, 425, 429, 500, 502, 503, 504},
            )
        return payload

    # ------------------------------------------------------------------ Link

    async def create_link_token(
        self,
        *,
        access_token: str | None = None,
        account_selection: bool = False,
    ) -> dict[str, Any]:
        """Mint a Link token: a new connection, or update mode for a broken Item.

        Update mode reuses the Item's access token, so it repairs a login
        without spending one of the Trial plan's lifetime Item slots, and the
        public token Link hands back afterwards must not be exchanged.
        """

        body: dict[str, Any] = {
            "client_name": self.client_name[:30],
            "language": "en",
            "country_codes": ["US"],
            "user": {"client_user_id": CLIENT_USER_ID},
        }
        if self.redirect_uri:
            body["redirect_uri"] = self.redirect_uri
        if access_token:
            body["access_token"] = access_token
            if account_selection:
                body["update"] = {"account_selection_enabled": True}
        else:
            body["products"] = ["transactions"]
            body["transactions"] = {"days_requested": self.days_requested}
        payload = await self._post("/link/token/create", body)
        return {
            "link_token": str(payload.get("link_token") or ""),
            "expiration": str(payload.get("expiration") or ""),
            "update_mode": bool(access_token),
            "environment": self.environment,
        }

    async def exchange_public_token(self, public_token: str) -> dict[str, str]:
        payload = await self._post("/item/public_token/exchange", {"public_token": public_token})
        return {
            "access_token": str(payload.get("access_token") or ""),
            "item_id": str(payload.get("item_id") or ""),
        }

    # ----------------------------------------------------------------- Items

    async def get_item(self, access_token: str) -> dict[str, Any]:
        payload = await self._post("/item/get", {"access_token": access_token})
        return normalize_item(payload.get("item") or {}, payload.get("status") or {})

    async def get_accounts(self, access_token: str) -> dict[str, Any]:
        """Cached balances for every account on an Item. Free, refreshed by Plaid daily."""
        payload = await self._post("/accounts/get", {"access_token": access_token})
        item = normalize_item(payload.get("item") or {})
        return {
            "item": item,
            "accounts": [
                normalize_account(raw, item_id=item["item_id"])
                for raw in payload.get("accounts") or []
                if isinstance(raw, dict)
            ],
        }

    async def remove_item(self, access_token: str) -> bool:
        """Permanently disconnect an Item. On the Trial plan this frees no slot."""
        await self._post("/item/remove", {"access_token": access_token})
        return True

    # ---------------------------------------------------------- transactions

    async def transactions_sync_page(
        self, access_token: str, *, cursor: str = "", count: int = 500
    ) -> dict[str, Any]:
        """One page of the per-Item change stream. The caller pages and keeps the cursor."""
        body: dict[str, Any] = {
            "access_token": access_token,
            "count": max(1, min(500, count)),
            "options": {"include_original_description": True},
        }
        if cursor:
            body["cursor"] = cursor
        payload = await self._post("/transactions/sync", body)
        return {
            "added": [item for item in payload.get("added") or [] if isinstance(item, dict)],
            "modified": [item for item in payload.get("modified") or [] if isinstance(item, dict)],
            "removed": [item for item in payload.get("removed") or [] if isinstance(item, dict)],
            "next_cursor": str(payload.get("next_cursor") or ""),
            "has_more": bool(payload.get("has_more")),
            "update_status": str(payload.get("transactions_update_status") or ""),
            "accounts": [
                normalize_account(raw)
                for raw in payload.get("accounts") or []
                if isinstance(raw, dict)
            ],
        }

    async def transactions_refresh(self, access_token: str) -> dict[str, Any]:
        """Ask Plaid to extract now rather than on its own schedule. Asynchronous."""
        payload = await self._post("/transactions/refresh", {"access_token": access_token})
        return {"request_id": str(payload.get("request_id") or "")}

    # --------------------------------------------------------------- sandbox

    async def sandbox_create_item(
        self,
        *,
        institution_id: str = SANDBOX_INSTITUTION,
        username: str = SANDBOX_USERNAME,
        password: str = SANDBOX_PASSWORD,
    ) -> dict[str, str]:
        """Create a sandbox Item without the Link UI and exchange it straight away."""
        if not self.sandbox:
            raise PlaidError("Sandbox Items can only be created in the sandbox environment")
        payload = await self._post(
            "/sandbox/public_token/create",
            {
                "institution_id": institution_id,
                "initial_products": ["transactions"],
                "options": {
                    "override_username": username,
                    "override_password": password,
                    "transactions": {
                        "start_date": (
                            datetime.date.today() - datetime.timedelta(days=self.days_requested)
                        ).isoformat(),
                        "end_date": datetime.date.today().isoformat(),
                    },
                },
            },
        )
        exchanged = await self.exchange_public_token(str(payload.get("public_token") or ""))
        return {**exchanged, "institution_id": institution_id}

    async def sandbox_reset_login(self, access_token: str) -> bool:
        """Force ITEM_LOGIN_REQUIRED on a sandbox Item to rehearse the repair flow."""
        if not self.sandbox:
            raise PlaidError("Logins can only be reset in the sandbox environment")
        await self._post("/sandbox/item/reset_login", {"access_token": access_token})
        return True

    # ------------------------------------------------------------------ test

    async def test_connection(self) -> dict[str, Any]:
        """Creating a Link token is free and proves the credentials and environment."""
        token = await self.create_link_token()
        return {
            "ok": True,
            "message": f"Plaid {self.environment} credentials accepted",
            "environment": self.environment,
            "link_token_expires": token["expiration"],
        }


def health_payload(
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Fold per-Item readings into the shape the health check consumes.

    Each entry is ``{"item": stored row, "info": get_item result or None,
    "accounts": get_accounts accounts or [], "error": PlaidError or None}``.
    Plaid has no balance timestamp for most institutions, so the Item's last
    successful transactions update stands in for it: it is the moment Plaid
    last got fresh data from the bank, which is what staleness is about.
    """

    accounts: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for entry in items:
        stored = entry.get("item") or {}
        item_id = str(stored.get("item_id") or "")
        info = entry.get("info") or {}
        institution = str(
            info.get("institution_name") or stored.get("institution_name") or ""
        )
        failure = entry.get("error")
        if failure is not None:
            errors.append(
                {
                    "provider": "plaid",
                    "conn_id": item_id,
                    "code": getattr(failure, "error_code", "") or "",
                    "message": getattr(failure, "display_message", "") or str(failure),
                }
            )
        elif info.get("error"):
            errors.append(
                {
                    "provider": "plaid",
                    "conn_id": item_id,
                    "code": info["error"]["error_code"],
                    "message": info["error"]["display_message"]
                    or info["error"]["error_message"]
                    or info["error"]["error_code"],
                }
            )
        balance_date = info.get("last_successful_update")
        for account in entry.get("accounts") or []:
            accounts.append(
                {
                    "provider": "plaid",
                    "id": account["id"],
                    "name": account["name"],
                    "org_name": institution,
                    "connection_id": item_id,
                    "balance_cents": account.get("balance_cents") or 0,
                    "available_cents": account.get("available_cents"),
                    "balance_date": account.get("balance_updated") or balance_date,
                    "last_transaction_date": None,
                    "currency": account.get("currency", "USD"),
                }
            )
    return {"accounts": accounts, "errors": errors}
