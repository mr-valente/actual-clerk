from __future__ import annotations

import datetime

from actual_clerk.digest import build_digest, health_alert

TODAY = datetime.date(2026, 8, 21)

HEALTHY_REPORT = {
    "configured": True,
    "free_cents": 208000,
    "spent_cents": 17500,
    "remaining_cents": 190500,
    "remaining_percent": 0.9158,
    "daily_safe_to_spend_cents": 17318,
    "days_remaining": 11,
    "pace_delta_cents": 123403,
    "on_track": True,
    "expected_income_cents": 400000,
    "committed_cents": 192000,
    "committed_overspend_cents": 0,
    "uncategorized_count": 0,
    "uncategorized_cents": 0,
}


def digest(report=None, **kwargs):
    options = {"health": [], "review_count": 0, "today": TODAY}
    options.update(kwargs)
    return build_digest(report={**HEALTHY_REPORT, **(report or {})}, **options)


def test_a_good_month_leads_with_what_is_left():
    payload = digest()
    assert payload["title"] == "$1,905.00 free money left (92%)"
    assert "$173.18 a day" in payload["message"]
    assert "under an even pace for the month" in payload["message"]
    assert payload["priority"] == 3
    assert payload["local_date"] == "2026-08-21"


def test_being_behind_the_pace_raises_the_priority():
    payload = digest({"on_track": False, "pace_delta_cents": -20000})
    assert "over an even pace for the month" in payload["message"]
    assert payload["priority"] == 4


def test_running_out_of_free_money_is_said_plainly():
    payload = digest({"remaining_cents": -5000, "remaining_percent": -0.02})
    assert "already spent" in payload["message"]
    assert payload["priority"] >= 4


def test_a_budget_with_no_income_asks_to_be_set_up_instead():
    payload = digest({"configured": False})
    assert payload["title"] == "Set up your budget in Clerk"
    assert "monthly income" in payload["message"].lower() or "income" in payload["message"]


def test_committing_everything_leaves_nothing_to_report_on():
    payload = digest({"free_cents": 0, "committed_cents": 400000})
    assert "No free money budgeted" in payload["title"]
    assert payload["priority"] == 4


def test_a_broken_connection_is_the_most_urgent_thing_in_the_digest():
    payload = digest(
        health=[
            {"account_name": "Checking", "status": "error", "detail": "Reauthorize"},
            {"account_name": "Savings", "status": "ok", "detail": ""},
        ]
    )
    assert payload["priority"] == 5
    assert "Checking (Connection error)" in payload["message"]
    assert payload["degraded_accounts"] == [{"account_name": "Checking", "status": "error"}]


def test_many_broken_connections_are_summarised_not_listed():
    health = [
        {"account_name": f"Account {index}", "status": "stale", "detail": ""} for index in range(5)
    ]
    payload = digest(health=health)
    assert "and 2 more" in payload["message"]


def test_work_waiting_for_a_human_is_mentioned():
    payload = digest(review_count=3, report={"uncategorized_count": 2, "uncategorized_cents": 4500})
    assert "3 transaction(s) waiting" in payload["message"]
    assert "2 transaction(s) this month are still uncategorized" in payload["message"]


def test_overspent_bills_are_called_out():
    payload = digest({"committed_overspend_cents": 4200})
    assert "over budget by $42.00" in payload["message"]


def test_the_message_stays_readable_on_a_lock_screen():
    payload = digest(
        review_count=3,
        health=[{"account_name": "Checking", "status": "error", "detail": "x"}],
    )
    assert len(payload["message"]) < 700
    assert len(payload["message"].splitlines()) <= 8


# ------------------------------------------------------------ health alerts


def test_nothing_changed_means_no_alert():
    assert health_alert([]) is None
    assert health_alert([{"account_name": "A", "status": "ok", "previous_status": ""}]) is None


def test_a_newly_broken_connection_is_announced_loudly():
    alert = health_alert(
        [{"account_name": "Checking", "status": "error", "previous_status": "ok", "detail": "Reauthorize"}]
    )
    assert alert["priority"] == 5
    assert "Checking" in alert["title"]
    assert "Reauthorize" in alert["message"]


def test_a_recovery_is_announced_quietly():
    alert = health_alert(
        [{"account_name": "Checking", "status": "ok", "previous_status": "error", "detail": ""}]
    )
    assert alert["priority"] == 3
    assert "restored" in alert["title"]


def test_a_first_sighting_of_a_healthy_account_is_not_a_recovery():
    assert health_alert([{"account_name": "New", "status": "ok", "previous_status": ""}]) is None


def test_breakage_leads_and_recovery_is_appended():
    alert = health_alert(
        [
            {"account_name": "Checking", "status": "error", "previous_status": "ok", "detail": "down"},
            {"account_name": "Savings", "status": "ok", "previous_status": "stale", "detail": ""},
        ]
    )
    assert "problem" in alert["title"]
    assert "Savings" in alert["message"]


def test_a_manual_account_appearing_never_raises_an_alert():
    assert health_alert([{"account_name": "Cash", "status": "not_linked", "previous_status": ""}]) is None


def test_an_unmonitored_account_never_reaches_the_digest():
    payload = digest(
        health=[
            {"account_name": "Dormant", "status": "muted", "detail": "Monitoring is off"},
            {"account_name": "Checking", "status": "ok", "detail": ""},
        ]
    )
    assert payload["degraded_accounts"] == []
    assert "Dormant" not in payload["message"]
    assert payload["priority"] == 3


def test_muting_a_broken_account_is_not_announced_as_a_recovery():
    assert (
        health_alert(
            [{"account_name": "Dormant", "status": "muted", "previous_status": "error"}]
        )
        is None
    )


def test_unmuting_a_broken_account_does_raise_the_alarm():
    alert = health_alert(
        [{"account_name": "Dormant", "status": "error", "previous_status": "muted", "detail": "down"}]
    )
    assert alert is not None
    assert alert["priority"] == 5


def test_an_overspent_month_leads_with_how_far_past_it_is():
    """A negative amount "left" is not a quantity anyone has."""
    payload = digest({"remaining_cents": -99590, "remaining_percent": -0.49})
    assert payload["title"] == "$995.90 over budget (49%)"
    assert "left" not in payload["title"]
