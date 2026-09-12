"""The phone companion's endpoints and the web view of anticipated charges."""

# ruff: noqa: F811 - the shared API fixtures are imported by name below

from __future__ import annotations

import datetime

import pytest

from actual_clerk.processing import OVERVIEW_SNAPSHOT

from .test_api import client, gateway  # noqa: F401 - shared fixtures

DEVICE = {"device_id": "phone-1", "device_name": "Pixel"}
PACKAGE = "com.konylabs.capitalone"


def seed_overview(database, *, budget=None):
    database.set_snapshot(
        OVERVIEW_SNAPSHOT,
        {
            "budget": budget or {"configured": True, "remaining_cents": 100000, "available_cents": 120000,
                                 "spent_cents": 20000, "anticipated_cents": 0, "anticipated_count": 0,
                                 "daily_safe_to_spend_cents": 5000, "month": "2026-08"},
            "accounts": [
                {"id": "acct-card", "name": "Venture", "off_budget": False, "closed": False, "sync_source": "plaid"},
                {"id": "acct-closed", "name": "Old", "off_budget": False, "closed": True, "sync_source": ""},
            ],
            "categories": [],
            "health": [],
        },
    )


async def register(client, **overrides):
    payload = {**DEVICE, "package_name": PACKAGE, "app_label": "Capital One", "actual_account_id": "acct-card"}
    payload.update(overrides)
    return await client.post("/api/anticipated/device/sources", json=payload)


def notification(text, *, posted_at_ms=1_755_780_000_000, title="Capital One", **extra):
    return {**DEVICE, "package_name": PACKAGE, "posted_at_ms": posted_at_ms, "title": title, "text": text, **extra}


# ---------------------------------------------------------------- device auth


async def test_the_device_token_gates_the_phone_endpoints_when_set(client):
    client.settings_manager.update({"anticipated_device_token": "s3cret"})
    assert (await client.get("/api/anticipated/device/hello")).status_code == 401
    assert (await client.get("/api/anticipated/device/hello", headers={"Authorization": "Bearer wrong"})).status_code == 401
    ok = await client.get("/api/anticipated/device/hello", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
    also_ok = await client.get("/api/anticipated/device/hello", headers={"X-Clerk-Device-Token": "s3cret"})
    assert also_ok.status_code == 200
    # The web UI's view of the same ledger is not behind the device token.
    assert (await client.get("/api/anticipated")).status_code == 200


async def test_without_a_token_the_phone_endpoints_are_open(client):
    assert (await client.get("/api/anticipated/device/hello")).status_code == 200


async def test_turning_the_feature_off_refuses_the_phone(client):
    client.settings_manager.update({"anticipated_enabled": False})
    assert (await client.get("/api/anticipated/device/hello")).status_code == 409


# ------------------------------------------------------------------- sources


async def test_hello_lists_open_accounts_and_the_device_s_sources(client):
    seed_overview(client.database)
    assert (await register(client)).status_code == 201
    body = (await client.get("/api/anticipated/device/hello", params={"device_id": "phone-1"})).json()
    assert [a["id"] for a in body["accounts"]] == ["acct-card"]
    assert [s["package_name"] for s in body["sources"]] == [PACKAGE]
    assert body["sources"][0]["account_name"] == "Venture"
    assert body["budget"]["remaining_cents"] == 100000
    other = (await client.get("/api/anticipated/device/hello", params={"device_id": "phone-2"})).json()
    assert other["sources"] == []


async def test_a_source_must_point_at_an_account_actual_knows(client):
    seed_overview(client.database)
    assert (await register(client, actual_account_id="acct-closed")).status_code == 404
    assert (await register(client, actual_account_id="nope")).status_code == 404


async def test_a_device_can_only_unregister_its_own_source(client):
    seed_overview(client.database)
    source = (await register(client)).json()["source"]
    wrong = await client.delete(f"/api/anticipated/device/sources/{source['id']}", params={"device_id": "phone-2"})
    assert wrong.status_code == 404
    right = await client.delete(f"/api/anticipated/device/sources/{source['id']}", params={"device_id": "phone-1"})
    assert right.json() == {"deleted": True}


async def test_the_web_can_move_or_pause_a_source(client):
    seed_overview(client.database)
    client.database.get_snapshot(OVERVIEW_SNAPSHOT)
    source = (await register(client)).json()["source"]
    paused = await client.patch(f"/api/anticipated/sources/{source['id']}", json={"enabled": False})
    assert paused.json()["source"]["enabled"] is False
    forwarded = await client.post("/api/anticipated/device/notifications", json=notification("A charge of $1.00 at A was approved."))
    assert forwarded.status_code == 202
    assert forwarded.json() == {"accepted": False, "reason": "source_disabled", "source_id": source["id"]}
    moved = await client.patch(f"/api/anticipated/sources/{source['id']}", json={"actual_account_id": "nope"})
    assert moved.status_code == 404


# ------------------------------------------------------------- notifications


async def test_a_forwarded_charge_is_read_stored_and_reflected_in_the_budget(client, gateway):
    seed_overview(client.database)
    await register(client)
    response = await client.post(
        "/api/anticipated/device/notifications",
        json=notification("A charge of $12.34 at STARBUCKS STORE 1 was approved on your Venture card ending in 1234."),
    )
    assert response.status_code == 202
    body = response.json()
    assert body["created"] is True
    charge = body["charge"]
    assert charge["kind"] == "charge"
    assert charge["amount_cents"] == -1234
    assert charge["merchant"] == "STARBUCKS STORE 1"
    assert charge["status"] == "open"
    assert charge["counts"] is True
    assert charge["actual_account_id"] == "acct-card"
    # The budget was re-read so the phone's figure already includes the charge.
    assert body["refreshed"] is True
    assert body["budget"]["currency"] == "USD"
    overview = (await client.get("/api/anticipated")).json()
    assert [c["id"] for c in overview["open"]] == [charge["id"]]
    assert (await client.get("/api/overview")).json()["counts"]["anticipated_open"] == 1


async def test_the_same_notification_again_is_not_a_second_charge(client):
    seed_overview(client.database)
    await register(client)
    first = (await client.post("/api/anticipated/device/notifications", json=notification("A charge of $5.00 at SHOP was approved."))).json()
    second = (await client.post("/api/anticipated/device/notifications", json=notification("A charge of $5.00 at SHOP was approved."))).json()
    assert second["created"] is False
    assert second["charge"]["id"] == first["charge"]["id"]
    assert second["refreshed"] is False
    assert len((await client.get("/api/anticipated")).json()["open"]) == 1


async def test_the_phone_s_own_key_wins_over_the_derived_one(client):
    seed_overview(client.database)
    await register(client)
    a = (await client.post("/api/anticipated/device/notifications", json=notification("A charge of $5.00 at SHOP was approved.", notification_key="k1"))).json()
    b = (await client.post("/api/anticipated/device/notifications", json=notification("A charge of $5.00 at SHOP was approved.", notification_key="k1", posted_at_ms=999))).json()
    assert b["charge"]["id"] == a["charge"]["id"]


async def test_an_unregistered_app_is_refused_so_the_phone_re_reads_its_sources(client):
    seed_overview(client.database)
    response = await client.post("/api/anticipated/device/notifications", json=notification("A charge of $5.00 at SHOP was approved."))
    assert response.status_code == 404


async def test_a_declined_charge_is_logged_but_never_open(client):
    seed_overview(client.database)
    await register(client)
    body = (await client.post("/api/anticipated/device/notifications", json=notification("A $9.99 charge at ACME was declined."))).json()
    assert body["charge"]["status"] == "ignored"
    assert body["charge"]["counts"] is False
    assert body["refreshed"] is False
    listing = (await client.get("/api/anticipated/device/charges", params={"device_id": "phone-1"})).json()
    assert [c["kind"] for c in listing["charges"]] == ["declined"]


async def test_a_budget_read_failure_still_keeps_the_charge(client, gateway, monkeypatch):
    seed_overview(client.database)
    await register(client)

    async def broken():
        from actual_clerk.clients.actual import ActualGatewayError
        raise ActualGatewayError("Actual is down")

    from actual_clerk.main import app
    monkeypatch.setattr(app.state.job_manager, "refresh_now", broken)
    body = (await client.post("/api/anticipated/device/notifications", json=notification("A charge of $5.00 at SHOP was approved."))).json()
    assert body["created"] is True
    assert body["refreshed"] is False
    assert "Actual is down" in body["refresh_error"]
    assert len((await client.get("/api/anticipated")).json()["open"]) == 1


async def test_a_notification_with_nothing_to_read_is_rejected(client):
    seed_overview(client.database)
    await register(client)
    response = await client.post("/api/anticipated/device/notifications", json={**DEVICE, "package_name": PACKAGE})
    assert response.status_code == 422


# --------------------------------------------------------------- web actions


async def test_a_charge_can_be_dismissed_and_reopened_by_hand(client):
    seed_overview(client.database)
    await register(client)
    charge = (await client.post("/api/anticipated/device/notifications", json=notification("A charge of $5.00 at SHOP was approved."))).json()["charge"]
    assert (await client.post(f"/api/anticipated/charges/{charge['id']}/dismiss")).json() == {"status": "dismissed"}
    assert (await client.post(f"/api/anticipated/charges/{charge['id']}/dismiss")).status_code == 409
    overview = (await client.get("/api/anticipated")).json()
    assert overview["open"] == []
    assert [c["status"] for c in overview["recent"]] == ["dismissed"]
    assert (await client.post(f"/api/anticipated/charges/{charge['id']}/reopen")).json() == {"status": "open"}
    assert (await client.post("/api/anticipated/charges/nope/reopen")).status_code == 404


async def test_deleting_a_source_from_the_web_drops_its_charges(client):
    seed_overview(client.database)
    source = (await register(client)).json()["source"]
    await client.post("/api/anticipated/device/notifications", json=notification("A charge of $5.00 at SHOP was approved."))
    assert (await client.delete(f"/api/anticipated/sources/{source['id']}")).json() == {"deleted": True}
    assert (await client.delete(f"/api/anticipated/sources/{source['id']}")).status_code == 404
    overview = (await client.get("/api/anticipated")).json()
    assert overview["sources"] == [] and overview["open"] == [] and overview["recent"] == []


async def test_the_overview_exposes_the_feature_s_settings(client):
    client.settings_manager.update({"anticipated_device_token": "abc", "anticipated_match_window_days": 7})
    body = (await client.get("/api/anticipated")).json()
    assert body["enabled"] is True
    assert body["token_configured"] is True
    assert body["match_window_days"] == 7
    settings = (await client.get("/api/settings")).json()
    assert "anticipated_device_token" not in settings
    assert settings["anticipated_device_token_configured"] is True


@pytest.mark.parametrize("values", [{"anticipated_expire_days": 5, "anticipated_match_window_days": 10}])
async def test_the_expiry_must_outlast_the_match_window(client, values):
    response = await client.patch("/api/settings", json={"values": values})
    assert response.status_code == 422
    assert "match window" in response.json()["detail"]


async def test_noticed_date_follows_the_configured_time_zone(client):
    seed_overview(client.database)
    client.settings_manager.update({"timezone": "America/New_York"})
    await register(client)
    # 03:00 UTC on 22 August is still the evening of 21 August in New York.
    posted = int(datetime.datetime(2026, 8, 22, 3, tzinfo=datetime.UTC).timestamp() * 1000)
    body = (await client.post("/api/anticipated/device/notifications", json=notification("A charge of $5.00 at SHOP was approved.", posted_at_ms=posted))).json()
    assert body["charge"]["noticed_date"] == "2026-08-21"


async def test_registering_from_a_notification_records_it_as_a_charge(client):
    seed_overview(client.database)
    response = await register(
        client,
        sample_title="Venture Credit Card…4273",
        sample_text="Your purchase for $3.19 at Valve was approved.",
        sample_posted_at_ms=1_755_780_000_000,
    )
    assert response.status_code == 201
    charge = response.json()["charge"]
    assert charge["amount_cents"] == -319
    assert charge["merchant"] == "Valve"
    assert charge["status"] == "open"
    assert len((await client.get("/api/anticipated")).json()["open"]) == 1
    # Registering again with the same sample is not a second charge.
    again = await register(
        client,
        sample_title="Venture Credit Card…4273",
        sample_text="Your purchase for $3.19 at Valve was approved.",
        sample_posted_at_ms=1_755_780_000_000,
    )
    assert again.json()["charge"]["id"] == charge["id"]
    assert len((await client.get("/api/anticipated")).json()["open"]) == 1


async def test_registering_without_a_sample_records_nothing(client):
    seed_overview(client.database)
    assert (await register(client)).json()["charge"] is None


def seed_categories(database):
    snapshot = database.get_snapshot(OVERVIEW_SNAPSHOT)
    snapshot["categories"] = [
        {"id": "cat-games", "name": "Games", "group_name": "Fun", "is_income": False},
        {"id": "cat-pay", "name": "Paycheck", "group_name": "Income", "is_income": True},
    ]
    snapshot.pop("snapshot_updated_at", None)
    database.set_snapshot(OVERVIEW_SNAPSHOT, snapshot)


async def test_a_category_can_be_taught_and_cleared(client):
    seed_overview(client.database)
    seed_categories(client.database)
    await register(client)
    charge = (await client.post("/api/anticipated/device/notifications", json=notification("Your purchase for $3.19 at Valve was approved."))).json()["charge"]
    assert charge["category_id"] == ""
    taught = await client.post(f"/api/anticipated/charges/{charge['id']}/category", json={"category_id": "cat-games"})
    assert taught.status_code == 200
    assert taught.json()["charge"]["category_name"] == "Games"
    assert taught.json()["charge"]["category_source"] == "taught"
    assert (await client.post(f"/api/anticipated/charges/{charge['id']}/category", json={"category_id": "nope"})).status_code == 404
    cleared = await client.post(f"/api/anticipated/charges/{charge['id']}/category", json={"category_id": ""})
    assert cleared.json()["charge"]["category_id"] == ""
    body = (await client.get("/api/anticipated")).json()
    assert [c["id"] for c in body["categories"]] == ["cat-games"]


async def test_an_alias_can_be_taught_and_forgotten(client):
    seed_overview(client.database)
    await register(client)
    charge = (await client.post("/api/anticipated/device/notifications", json=notification("Your purchase for $3.19 at Valve was approved."))).json()["charge"]
    taught = await client.post(f"/api/anticipated/charges/{charge['id']}/alias", json={"payee": "Steam"})
    assert taught.status_code == 200
    assert taught.json()["alias"]["alias_key"] == "valve"
    assert taught.json()["alias"]["merchant_key"] == "steam"
    assert taught.json()["alias"]["source"] == "taught"
    assert [a["alias_key"] for a in (await client.get("/api/anticipated")).json()["aliases"]] == ["valve"]
    assert (await client.delete("/api/anticipated/aliases/valve")).json() == {"deleted": True}
    assert (await client.delete("/api/anticipated/aliases/valve")).status_code == 404


# ------------------------------------------------------ repeats and timing


async def test_a_non_ascii_device_token_is_refused_not_crashed(client):
    client.settings_manager.update({"anticipated_device_token": "clé-secrète"})
    assert (await client.get("/api/anticipated/device/hello", headers={"Authorization": "Bearer cle"})).status_code == 401
    # HTTP carries header bytes; the server reads them as latin-1, as the phone sends them.
    ok = await client.get(
        "/api/anticipated/device/hello",
        headers={"Authorization": "Bearer clé-secrète".encode("latin-1")},
    )
    assert ok.status_code == 200


async def test_a_re_posted_notification_under_a_new_key_is_one_charge(client):
    seed_overview(client.database)
    await register(client)
    text = "Your purchase for $3.19 at Valve was approved."
    first = (await client.post("/api/anticipated/device/notifications", json=notification(text, notification_key="k1"))).json()
    # The card app re-posts the same words two minutes later with a new post time.
    again = (await client.post("/api/anticipated/device/notifications", json=notification(text, notification_key="k2", posted_at_ms=1_755_780_000_000 + 120_000))).json()
    assert again["created"] is False
    assert again["charge"]["id"] == first["charge"]["id"]
    # The same words a quarter of an hour later are a new purchase.
    later = (await client.post("/api/anticipated/device/notifications", json=notification(text, notification_key="k3", posted_at_ms=1_755_780_000_000 + 15 * 60_000))).json()
    assert later["created"] is True
    assert len((await client.get("/api/anticipated")).json()["open"]) == 2


async def test_the_registration_sample_seen_again_by_the_listener_is_one_charge(client):
    seed_overview(client.database)
    registered = await register(
        client,
        sample_title="Venture Credit Card…4273",
        sample_text="Your purchase for $3.19 at Valve was approved.",
        sample_posted_at_ms=1_755_780_000_000,
    )
    sample = registered.json()["charge"]
    forwarded = (await client.post(
        "/api/anticipated/device/notifications",
        json=notification(
            "Your purchase for $3.19 at Valve was approved.",
            title="Venture Credit Card…4273",
            notification_key=f"{PACKAGE}:1755780060000:abcdef0123456789",
            posted_at_ms=1_755_780_060_000,
        ),
    )).json()
    assert forwarded["created"] is False
    assert forwarded["charge"]["id"] == sample["id"]


async def test_a_slow_budget_read_does_not_hold_the_phone(client, monkeypatch):
    import asyncio

    from actual_clerk import main as clerk_main

    seed_overview(client.database)
    await register(client)
    finished = asyncio.Event()

    async def slow():
        await asyncio.sleep(0.2)
        finished.set()
        return {}

    monkeypatch.setattr(clerk_main.app.state.job_manager, "refresh_now", slow)
    monkeypatch.setattr(clerk_main, "FORWARD_REFRESH_SECONDS", 0.02)
    body = (await client.post("/api/anticipated/device/notifications", json=notification("A charge of $5.00 at SHOP was approved."))).json()
    assert body["created"] is True
    assert body["refreshed"] is False
    assert "still" in body["refresh_error"]
    # The read carries on in the background rather than being cancelled.
    await asyncio.wait_for(finished.wait(), timeout=2)
    assert len((await client.get("/api/anticipated")).json()["open"]) == 1
