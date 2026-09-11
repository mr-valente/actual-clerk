"""The direct Plaid client and the shapes it hands the rest of Clerk."""

from __future__ import annotations

import datetime
import json

import httpx
import pytest

from actual_clerk.clients.plaid import (
    PlaidClient,
    PlaidError,
    health_payload,
    normalize_account,
    normalize_item,
    to_cents,
)
from actual_clerk.config import Settings


def plaid_settings(**overrides) -> Settings:
    values = {
        "actual_password": "secret",
        "actual_budget_id": "budget-1",
        "plaid_client_id": "client-1",
        "plaid_secret": "secret-1",
    }
    values.update(overrides)
    return Settings(**values)


def transport(handler):
    return httpx.MockTransport(handler)


def json_response(payload, status_code=200):
    return httpx.Response(status_code, json=payload)


# ------------------------------------------------------------- conversions


def test_amounts_go_through_decimal_and_liabilities_flip_sign():
    assert to_cents(12.34) == 1234
    assert to_cents("0.1") == 10
    assert to_cents(None) is None
    card = normalize_account(
        {
            "account_id": "acc-card",
            "name": "Credit card",
            "type": "credit",
            "subtype": "credit card",
            "mask": "3637",
            "balances": {"current": 500.0, "available": 4026.06, "limit": 4600, "iso_currency_code": "USD"},
        }
    )
    assert card["balance_cents"] == -50000
    assert card["available_cents"] == -402606
    assert card["limit_cents"] == 460000
    checking = normalize_account(
        {"account_id": "acc-chk", "name": "Checking", "type": "depository", "balances": {"current": 500}}
    )
    assert checking["balance_cents"] == 50000
    assert checking["available_cents"] is None
    assert checking["currency"] == "USD"
    with pytest.raises(PlaidError):
        to_cents("twelve")


def test_item_status_and_errors_are_normalized():
    item = normalize_item(
        {
            "item_id": "item-1",
            "institution_id": "ins_1",
            "institution_name": "Platypus",
            "error": {"error_type": "ITEM_ERROR", "error_code": "ITEM_LOGIN_REQUIRED", "error_message": "log in again"},
            "consent_expiration_time": "2027-01-01T00:00:00Z",
            "products": ["transactions"],
        },
        {"transactions": {"last_successful_update": "2026-09-10T08:00:00Z", "last_failed_update": None}},
    )
    assert item["error"]["needs_repair"] is True
    assert item["consent_expiration_time"].year == 2027
    assert item["last_successful_update"] == datetime.datetime(2026, 9, 10, 8, tzinfo=datetime.UTC)
    assert item["last_failed_update"] is None
    assert normalize_item({"item_id": "x", "error": None})["error"] is None


# ------------------------------------------------------------------ client


async def test_requests_carry_credentials_and_the_pinned_version():
    seen = {}

    def handler(request: httpx.Request):
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        seen["url"] = str(request.url)
        return json_response({"link_token": "link-1", "expiration": "2026-09-12T00:00:00Z"})

    client = PlaidClient(plaid_settings(plaid_days_requested=400), transport=transport(handler))
    try:
        token = await client.create_link_token()
    finally:
        await client.close()
    assert token == {"link_token": "link-1", "expiration": "2026-09-12T00:00:00Z", "update_mode": False, "environment": "sandbox"}
    assert seen["url"] == "https://sandbox.plaid.com/link/token/create"
    assert seen["headers"]["plaid-client-id"] == "client-1"
    assert seen["headers"]["plaid-secret"] == "secret-1"
    assert seen["headers"]["plaid-version"] == "2020-09-14"
    assert seen["body"]["products"] == ["transactions"]
    assert seen["body"]["transactions"] == {"days_requested": 400}
    assert "redirect_uri" not in seen["body"]


async def test_update_mode_reuses_the_access_token_and_asks_for_no_products():
    seen = {}

    def handler(request: httpx.Request):
        seen["body"] = json.loads(request.content)
        return json_response({"link_token": "link-2", "expiration": ""})

    client = PlaidClient(
        plaid_settings(plaid_redirect_uri="https://clerk.example/plaid-oauth"),
        transport=transport(handler),
    )
    try:
        token = await client.create_link_token(access_token="access-1", account_selection=True)
    finally:
        await client.close()
    assert token["update_mode"] is True
    assert seen["body"]["access_token"] == "access-1"
    assert seen["body"]["update"] == {"account_selection_enabled": True}
    assert seen["body"]["redirect_uri"] == "https://clerk.example/plaid-oauth"
    assert "products" not in seen["body"]


async def test_plaid_errors_are_typed_and_repairable_ones_say_so():
    def handler(request: httpx.Request):
        return json_response(
            {
                "error_type": "ITEM_ERROR",
                "error_code": "ITEM_LOGIN_REQUIRED",
                "error_message": "the login details changed",
                "display_message": None,
                "request_id": "req-1",
            },
            status_code=400,
        )

    client = PlaidClient(plaid_settings(), transport=transport(handler))
    try:
        with pytest.raises(PlaidError) as caught:
            await client.get_accounts("access-1")
    finally:
        await client.close()
    error = caught.value
    assert error.error_code == "ITEM_LOGIN_REQUIRED"
    assert error.needs_repair is True
    assert error.retryable is False
    assert error.request_id == "req-1"
    assert "ITEM_LOGIN_REQUIRED" in str(error)


async def test_server_side_failures_are_retryable():
    def handler(request: httpx.Request):
        return json_response({"error_type": "API_ERROR", "error_code": "INTERNAL_SERVER_ERROR", "error_message": "oops"}, 500)

    client = PlaidClient(plaid_settings(), transport=transport(handler))
    try:
        with pytest.raises(PlaidError) as caught:
            await client.get_item("access-1")
    finally:
        await client.close()
    assert caught.value.retryable is True
    assert caught.value.needs_repair is False


async def test_an_unconfigured_client_refuses_before_reaching_the_network():
    client = PlaidClient(plaid_settings(plaid_client_id=""), transport=transport(lambda r: json_response({})))
    try:
        assert client.configured is False
        with pytest.raises(PlaidError, match="not configured"):
            await client.get_item("x")
    finally:
        await client.close()


async def test_accounts_and_items_are_read_in_clerks_shape():
    def handler(request: httpx.Request):
        if request.url.path == "/accounts/get":
            return json_response(
                {
                    "accounts": [
                        {"account_id": "acc-1", "name": "Checking", "type": "depository", "subtype": "checking", "mask": "8193", "balances": {"current": 500, "available": 442}},
                    ],
                    "item": {"item_id": "item-1", "institution_id": "ins_1", "institution_name": "Platypus"},
                }
            )
        return json_response({"item": {"item_id": "item-1", "institution_name": "Platypus", "error": None}, "status": {"transactions": {"last_successful_update": "2026-09-10T08:00:00Z"}}})

    client = PlaidClient(plaid_settings(), transport=transport(handler))
    try:
        accounts = await client.get_accounts("access-1")
        item = await client.get_item("access-1")
    finally:
        await client.close()
    assert accounts["item"]["institution_name"] == "Platypus"
    assert accounts["accounts"][0]["item_id"] == "item-1"
    assert accounts["accounts"][0]["balance_cents"] == 50000
    assert item["last_successful_update"].hour == 8


async def test_a_sync_page_keeps_the_cursor_and_the_update_status():
    seen = {}

    def handler(request: httpx.Request):
        seen["body"] = json.loads(request.content)
        return json_response(
            {
                "added": [{"transaction_id": "t1"}],
                "modified": [],
                "removed": [{"transaction_id": "t0", "account_id": "acc-1"}],
                "next_cursor": "cursor-2",
                "has_more": True,
                "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
                "accounts": [{"account_id": "acc-1", "name": "Checking", "type": "depository", "balances": {"current": 1}}],
            }
        )

    client = PlaidClient(plaid_settings(), transport=transport(handler))
    try:
        page = await client.transactions_sync_page("access-1", cursor="cursor-1", count=9999)
    finally:
        await client.close()
    assert seen["body"]["cursor"] == "cursor-1"
    assert seen["body"]["count"] == 500
    assert seen["body"]["options"] == {"include_original_description": True}
    assert page["next_cursor"] == "cursor-2"
    assert page["has_more"] is True
    assert page["update_status"] == "HISTORICAL_UPDATE_COMPLETE"
    assert page["removed"] == [{"transaction_id": "t0", "account_id": "acc-1"}]
    assert page["accounts"][0]["balance_cents"] == 100


async def test_sandbox_helpers_are_refused_in_production():
    client = PlaidClient(plaid_settings(plaid_env="production"), transport=transport(lambda r: json_response({})))
    try:
        assert client.sandbox is False
        with pytest.raises(PlaidError, match="sandbox"):
            await client.sandbox_create_item()
        with pytest.raises(PlaidError, match="sandbox"):
            await client.sandbox_reset_login("access-1")
    finally:
        await client.close()


async def test_a_sandbox_item_is_created_and_exchanged_in_one_step():
    calls = []

    def handler(request: httpx.Request):
        calls.append((request.url.path, json.loads(request.content)))
        if request.url.path == "/sandbox/public_token/create":
            return json_response({"public_token": "public-sandbox-1"})
        return json_response({"access_token": "access-sandbox-1", "item_id": "item-1"})

    client = PlaidClient(plaid_settings(), transport=transport(handler))
    try:
        created = await client.sandbox_create_item(username="user_good")
    finally:
        await client.close()
    assert created == {"access_token": "access-sandbox-1", "item_id": "item-1", "institution_id": "ins_109508"}
    assert calls[0][1]["options"]["override_username"] == "user_good"
    assert calls[0][1]["initial_products"] == ["transactions"]
    assert calls[1] == ("/item/public_token/exchange", {"public_token": "public-sandbox-1"})


# --------------------------------------------------------- health payload


def test_health_payload_tags_readings_and_errors_with_the_provider():
    when = datetime.datetime(2026, 9, 10, 8, tzinfo=datetime.UTC)
    payload = health_payload(
        [
            {
                "item": {"item_id": "item-ok", "institution_name": "Platypus"},
                "info": {"institution_name": "First Platypus Bank", "last_successful_update": when, "error": None},
                "accounts": [{"id": "acc-1", "name": "Checking", "balance_cents": 50000, "available_cents": 44200, "currency": "USD", "balance_updated": None}],
                "error": None,
            },
            {
                "item": {"item_id": "item-broken", "institution_name": "Other"},
                "info": None,
                "accounts": [],
                "error": PlaidError("Plaid ITEM_ERROR/ITEM_LOGIN_REQUIRED: log in", error_code="ITEM_LOGIN_REQUIRED"),
            },
            {
                "item": {"item_id": "item-flagged", "institution_name": "Third"},
                "info": {"institution_name": "Third", "last_successful_update": None, "error": {"error_code": "PENDING_EXPIRATION", "error_message": "consent expiring", "display_message": "", "needs_repair": True}},
                "accounts": [{"id": "acc-3", "name": "Card", "balance_cents": -100, "currency": "USD", "balance_updated": when}],
                "error": None,
            },
        ]
    )
    [checking, card] = payload["accounts"]
    assert checking["provider"] == "plaid"
    assert checking["org_name"] == "First Platypus Bank"
    assert checking["connection_id"] == "item-ok"
    assert checking["balance_date"] == when
    assert card["balance_date"] == when
    assert [error["conn_id"] for error in payload["errors"]] == ["item-broken", "item-flagged"]
    assert payload["errors"][0]["provider"] == "plaid"
    assert payload["errors"][0]["code"] == "ITEM_LOGIN_REQUIRED"
    assert payload["errors"][1]["message"] == "consent expiring"
