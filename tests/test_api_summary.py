"""The flat summary a home dashboard tile reads."""

# ruff: noqa: F811 - the shared API fixtures are imported by name below

from __future__ import annotations

import pytest

from actual_clerk.processing import OVERVIEW_SNAPSHOT

from .test_api import client, decision, gateway  # noqa: F401 - shared fixtures


def seed_budget(database, **budget):
    database.set_snapshot(
        OVERVIEW_SNAPSHOT,
        {"budget": {"configured": True, "month": "2026-11", **budget}, "categories": [], "health": []},
    )


async def test_before_the_first_read_the_tile_says_nothing_rather_than_nan(client):
    body = (await client.get("/api/summary")).json()
    assert body["budget_left_text"] == "—" and body["budget_left_percent"] is None
    assert body["alerts"] == 0 and body["applied_today"] == 0


@pytest.mark.parametrize(
    ("budget", "percent", "text"),
    [
        ({"available_cents": 152953, "remaining_cents": 111679, "remaining_percent": 0.730152}, 73.0, "73% left"),
        # The Overview never shows "-12% left"; past zero it says how far past.
        ({"available_cents": 100000, "remaining_cents": -12000, "remaining_percent": -0.12}, -12.0, "12% over"),
        ({"available_cents": 100000, "remaining_cents": 500, "remaining_percent": 0.005}, 0.5, "1% left"),
        # Bills budgeted beyond income leave nothing to take a share of.
        ({"available_cents": 0, "remaining_cents": -3000, "remaining_percent": 0.0}, None, "—"),
    ],
)
async def test_free_money_left_is_a_share_worded_as_the_overview_words_it(client, budget, percent, text):
    seed_budget(client.database, **budget)
    body = (await client.get("/api/summary")).json()
    assert body["budget_left_percent"] == percent
    assert body["budget_left_text"] == text


async def test_an_unconfigured_budget_is_not_measured(client):
    seed_budget(client.database, configured=False, available_cents=5000, remaining_percent=1.0)
    assert (await client.get("/api/summary")).json()["budget_left_text"] == "—"


async def test_alerts_are_reviews_and_unhealthy_connections_together(client):
    client.database.add_decision(decision())
    client.database.add_decision(decision(transaction_id="txn-2"))
    client.database.record_health(
        [
            {"account_id": "a1", "account_name": "Checking", "status": "error", "detail": "down"},
            {"account_id": "a2", "account_name": "Savings", "status": "ok", "detail": ""},
        ]
    )
    body = (await client.get("/api/summary")).json()
    assert (body["needs_review"], body["degraded_accounts"], body["alerts"]) == (2, 1, 3)
