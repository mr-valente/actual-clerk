"""What Clerk knows about its Plaid Items, read fresh and kept honest.

Every Item Clerk holds is asked two cheap questions -- ``/item/get`` for its
status and ``/accounts/get`` for cached balances -- and the answers are folded
into one shape the Connections page and the health check both consume. Item
status in Clerk's own table is updated as a side effect, so a broken login
seen here is the same broken login the next health check reports.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from actual_clerk.clients.plaid import PlaidClient, PlaidError, health_payload
from actual_clerk.config import Settings
from actual_clerk.db import Database

log = logging.getLogger(__name__)

# The Trial plan allows this many production Items for the life of the team;
# removing one does not give the slot back.
TRIAL_ITEM_LIMIT = 10
PLAID = "plaid"


def public_item(item: dict[str, Any]) -> dict[str, Any]:
    """An Item row without its access token, with timestamps as ISO strings."""
    result = {key: value for key, value in item.items() if key != "access_token"}
    for field in ("created_at", "updated_at", "last_refresh_at", "last_sync_at"):
        value = result.get(field)
        result[field] = (
            datetime.datetime.fromtimestamp(float(value), datetime.UTC).isoformat()
            if value
            else None
        )
    result["needs_repair"] = item.get("status") == "needs_repair"
    result["has_cursor"] = bool(item.get("cursor"))
    return result


async def read_items(
    database: Database, settings: Settings, *, client: PlaidClient | None = None
) -> list[dict[str, Any]]:
    """Ask Plaid about every live Item and record what it said.

    Returns one entry per Item: ``{"item", "info", "accounts", "error"}``.
    A failure on one Item never hides the others; it is recorded on that Item
    (as ``needs_repair`` when Link's update mode is the fix) and carried into
    the health check as a provider error.
    """

    items = database.list_plaid_items()
    if not items or not settings.plaid_configured:
        return []
    owned = client is None
    client = client or PlaidClient(settings)
    entries: list[dict[str, Any]] = []
    try:
        for item in items:
            entry: dict[str, Any] = {"item": item, "info": None, "accounts": [], "error": None}
            try:
                entry["info"] = await client.get_item(item["access_token"])
                entry["accounts"] = (await client.get_accounts(item["access_token"]))["accounts"]
            except PlaidError as exc:
                entry["error"] = exc
                log.warning("Plaid Item %s: %s", item["item_id"], exc)
            _record(database, entry)
            entries.append(entry)
    finally:
        if owned:
            await client.close()
    return entries


def _record(database: Database, entry: dict[str, Any]) -> None:
    item = entry["item"]
    info = entry.get("info") or {}
    error = entry.get("error")
    fields: dict[str, Any] = {}
    if info:
        if info.get("institution_name") and info["institution_name"] != item.get(
            "institution_name"
        ):
            fields["institution_name"] = info["institution_name"]
        if info.get("institution_id") and info["institution_id"] != item.get("institution_id"):
            fields["institution_id"] = info["institution_id"]
        if info.get("last_successful_update"):
            fields["last_successful_update"] = info["last_successful_update"].isoformat()
    if error is not None:
        fields["status"] = "needs_repair" if error.needs_repair else "error"
        fields["last_error"] = str(error)
    elif info.get("error"):
        fields["status"] = "needs_repair" if info["error"]["needs_repair"] else "error"
        fields["last_error"] = info["error"]["error_message"] or info["error"]["error_code"]
    else:
        fields["status"] = "ok"
        fields["last_error"] = ""
    changed = {key: value for key, value in fields.items() if item.get(key) != value}
    if changed:
        database.update_plaid_item(item["item_id"], **changed)
        item.update(changed)


def readings(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """The health check's view of the Items: readings and errors, provider-tagged."""
    return health_payload(entries)


def describe(
    entries: list[dict[str, Any]],
    links: list[dict[str, Any]],
    *,
    environment: str,
    items_ever: int,
) -> dict[str, Any]:
    """The Connections page's view: Items, their accounts, and which are mapped."""

    links_by_external = {
        (link["provider"], link["external_account_id"]): link
        for link in links
        if link["provider"] == PLAID
    }
    items = []
    for entry in entries:
        item = public_item(entry["item"])
        info = entry.get("info") or {}
        error = entry.get("error")
        item["institution_name"] = info.get("institution_name") or item.get("institution_name")
        item["error"] = (
            error.as_dict()
            if error is not None
            else (info.get("error") if info else None)
        )
        item["consent_expiration_time"] = (
            info["consent_expiration_time"].isoformat()
            if info and info.get("consent_expiration_time")
            else None
        )
        item["last_successful_update"] = (
            info["last_successful_update"].isoformat()
            if info and info.get("last_successful_update")
            else item.get("last_successful_update") or None
        )
        accounts = []
        for account in entry.get("accounts") or []:
            link = links_by_external.get((PLAID, account["id"]))
            accounts.append(
                {
                    **{
                        key: value
                        for key, value in account.items()
                        if key != "balance_updated"
                    },
                    "balance_updated": (
                        account["balance_updated"].isoformat()
                        if account.get("balance_updated")
                        else None
                    ),
                    "link": _public_link(link) if link else None,
                }
            )
        item["accounts"] = accounts
        items.append(item)
    return {
        "environment": environment,
        "items": items,
        "links": [_public_link(link) for link in links if link["provider"] == PLAID],
        "slots": {
            "used": items_ever,
            "limit": TRIAL_ITEM_LIMIT if environment == "production" else None,
        },
    }


def _public_link(link: dict[str, Any]) -> dict[str, Any]:
    result = dict(link)
    for field in ("created_at", "updated_at", "last_import_at"):
        value = result.get(field)
        result[field] = (
            datetime.datetime.fromtimestamp(float(value), datetime.UTC).isoformat()
            if value
            else None
        )
    return result
