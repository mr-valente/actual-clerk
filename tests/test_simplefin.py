from __future__ import annotations

import base64
import datetime

import httpx
import pytest

from actual_clerk.clients.simplefin import (
    SimpleFinClient,
    SimpleFinError,
    decode_setup_token,
    parse_account_set,
    split_access_url,
)

V1_BODY = {
    "errors": ["Connection to Chase may need attention"],
    "accounts": [
        {
            "org": {"domain": "chase.com", "name": "Chase", "id": "c1"},
            "id": "sf1",
            "name": "Checking",
            "currency": "USD",
            "balance": "1234.56",
            "available-balance": "1200.00",
            "balance-date": 1755000000,
            "transactions": [
                {"id": "t1", "posted": 1754900000, "amount": "-12.50", "description": "COFFEE"}
            ],
        }
    ],
}

V2_BODY = {
    "errlist": [{"code": "con.auth", "msg": "Reauthorize", "conn_id": "c1"}],
    "connections": [{"conn_id": "c1", "name": "Chase", "org_id": "o1", "sfin_url": "https://x"}],
    "accounts": [
        {
            "id": "sf2",
            "name": "Card",
            "conn_id": "c1",
            "currency": "USD",
            "balance": "-50.00",
            "balance-date": 1755000000,
        }
    ],
}


def test_version_one_payloads_are_understood():
    result = parse_account_set(V1_BODY)
    [account] = result["accounts"]
    assert account["id"] == "sf1"
    assert account["org_name"] == "Chase"
    assert account["balance_cents"] == 123456
    assert account["available_cents"] == 120000
    assert account["last_transaction_date"] == datetime.date(2025, 8, 11)
    assert result["errors"][0]["message"] == "Connection to Chase may need attention"


def test_version_two_payloads_are_understood():
    result = parse_account_set(V2_BODY)
    [account] = result["accounts"]
    assert account["balance_cents"] == -5000
    assert account["org_name"] == "Chase"
    assert account["available_cents"] is None
    error = result["errors"][0]
    assert (error["code"], error["conn_id"]) == ("con.auth", "c1")


def test_amounts_are_read_as_decimals_not_floats():
    result = parse_account_set({"accounts": [{"id": "a", "name": "A", "balance": "0.07"}]})
    assert result["accounts"][0]["balance_cents"] == 7
    result = parse_account_set({"accounts": [{"id": "a", "name": "A", "balance": "-1234.005"}]})
    assert result["accounts"][0]["balance_cents"] == -123400


def test_an_unreadable_amount_is_an_error_not_a_silent_zero():
    with pytest.raises(SimpleFinError):
        parse_account_set({"accounts": [{"id": "a", "name": "A", "balance": "many dollars"}]})


def test_malformed_entries_are_skipped_rather_than_crashing():
    result = parse_account_set({"accounts": ["nonsense", None], "errors": [None, ""]})
    assert result == {"accounts": [], "errors": [], "connections": []}


def test_an_unusable_balance_date_becomes_unknown():
    result = parse_account_set({"accounts": [{"id": "a", "name": "A", "balance": "1", "balance-date": "soon"}]})
    assert result["accounts"][0]["balance_date"] is None


def test_a_setup_token_is_base64_around_a_claim_url():
    token = base64.b64encode(b"https://bridge.simplefin.org/simplefin/claim/DEMO").decode()
    assert decode_setup_token(token) == "https://bridge.simplefin.org/simplefin/claim/DEMO"
    # A URL pasted directly is accepted too.
    assert decode_setup_token("https://bridge.simplefin.org/claim/X").endswith("/claim/X")


def test_bad_setup_tokens_are_refused():
    with pytest.raises(SimpleFinError):
        decode_setup_token("")
    with pytest.raises(SimpleFinError):
        decode_setup_token("not base64 at all !!!")
    with pytest.raises(SimpleFinError):
        # http, not https: a claim URL carries credentials in the response.
        decode_setup_token(base64.b64encode(b"http://bridge/claim/x").decode())


def test_credentials_are_lifted_out_of_the_access_url():
    url, auth = split_access_url("https://user%40x:se%2Fcret@bridge.simplefin.org/simplefin/")
    assert url == "https://bridge.simplefin.org/simplefin"
    assert auth == ("user@x", "se/cret")
    assert "secret" not in url


def test_an_access_url_without_credentials_still_works():
    url, auth = split_access_url("https://bridge.simplefin.org/simplefin")
    assert (url, auth) == ("https://bridge.simplefin.org/simplefin", None)


def test_a_nonsense_access_url_is_refused():
    with pytest.raises(SimpleFinError):
        split_access_url("ftp://bridge/simplefin")


def _client(settings, handler, access_url="https://user:pass@bridge.example/simplefin"):
    settings.simplefin_access_url = access_url
    client = SimpleFinClient(settings)
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


async def test_a_balances_only_fetch_asks_for_exactly_that(settings):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=V1_BODY)

    client = _client(settings, handler)
    try:
        result = await client.fetch(balances_only=True, start_date=datetime.date(2026, 8, 1))
    finally:
        await client.close()

    assert "balances-only=1" in seen["url"]
    assert "start-date=" in seen["url"]
    assert seen["auth"].startswith("Basic ")
    assert len(result["accounts"]) == 1


async def test_revoked_credentials_are_explained_rather_than_retried(settings):
    client = _client(settings, lambda request: httpx.Response(403, text="nope"))
    try:
        with pytest.raises(SimpleFinError) as excinfo:
            await client.fetch()
    finally:
        await client.close()
    assert excinfo.value.status_code == 403
    assert excinfo.value.retryable is False
    assert "revoked" in str(excinfo.value)


async def test_a_server_error_is_marked_retryable(settings):
    client = _client(settings, lambda request: httpx.Response(503, text="later"))
    try:
        with pytest.raises(SimpleFinError) as excinfo:
            await client.fetch()
    finally:
        await client.close()
    assert excinfo.value.retryable is True


async def test_a_non_json_response_is_reported_clearly(settings):
    client = _client(settings, lambda request: httpx.Response(200, text="<html>"))
    try:
        with pytest.raises(SimpleFinError, match="not JSON"):
            await client.fetch()
    finally:
        await client.close()


async def test_an_unconfigured_client_refuses_to_pretend(settings):
    settings.simplefin_access_url = ""
    client = SimpleFinClient(settings)
    try:
        assert client.configured is False
        with pytest.raises(SimpleFinError, match="not configured"):
            await client.fetch()
    finally:
        await client.close()


async def test_the_connection_test_summarises_what_it_found(settings):
    client = _client(settings, lambda request: httpx.Response(200, json=V1_BODY))
    try:
        result = await client.test_connection()
    finally:
        await client.close()
    assert result["accounts"] == 1
    assert "1 account(s) visible" in result["message"]
    assert "problem" in result["message"]
