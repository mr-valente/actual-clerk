from __future__ import annotations

import datetime

from actual_clerk.config import Settings
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


def only(*names):
    """Every block off but the ones named, as the settings checkboxes do."""
    blocks = (
        "headline", "spending", "safe_to_spend", "pace",
        "projection", "commitments", "connections", "attention",
    )
    return {name: name in names for name in blocks}


def test_a_good_month_leads_with_what_is_left():
    payload = digest()
    # The header is the report's name, never a figure: everything the morning
    # holds is in the body, so the notification is recognisable at a glance.
    assert payload["title"] == "The Morning Report"
    assert payload["message"].splitlines()[0] == "Friday 21 August"
    assert "$1,905.00 free money left" in payload["message"]
    assert "92%" in payload["message"]
    assert "$173.18 a day" in payload["message"]
    assert "under an even pace for the month" in payload["message"]
    assert payload["priority"] == 3
    assert payload["local_date"] == "2026-08-21"


def test_the_header_is_whatever_it_was_named():
    assert digest(title="Budget o'clock")["title"] == "Budget o'clock"


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
    assert payload["title"] == "The Morning Report"
    assert "cannot work out your monthly income" in payload["message"]


def test_committing_everything_leaves_nothing_to_report_on():
    payload = digest({"free_cents": 0, "committed_cents": 400000})
    assert "No free money budgeted" in payload["message"]
    assert payload["priority"] == 4


def test_a_broken_connection_is_the_most_urgent_thing_in_the_digest():
    payload = digest(
        health=[
            {"account_name": "Checking", "status": "error", "detail": "Reauthorize"},
            {"account_name": "Savings", "status": "ok", "detail": ""},
        ]
    )
    assert payload["priority"] == 5
    assert "Checking \u2014 Connection error" in payload["message"]
    assert payload["degraded_accounts"] == [{"account_name": "Checking", "status": "error"}]


def test_many_broken_connections_are_summarised_not_listed():
    health = [
        {"account_name": f"Account {index}", "status": "stale", "detail": ""} for index in range(5)
    ]
    payload = digest(health=health)
    assert "and 1 more" in payload["message"]


def test_work_waiting_for_a_human_is_mentioned():
    payload = digest(review_count=3, report={"uncategorized_count": 2, "uncategorized_cents": 4500})
    assert "Waiting for you" in payload["message"]
    assert "3 transactions to review in Clerk" in payload["message"]
    assert "2 transactions still uncategorized this month ($45.00)" in payload["message"]


def test_counts_of_one_are_not_written_as_plurals():
    payload = digest(review_count=1, report={"uncategorized_count": 1, "uncategorized_cents": 900})
    assert "1 transaction to review" in payload["message"]
    assert "1 transaction still uncategorized" in payload["message"]


def test_overspent_bills_are_called_out():
    payload = digest({"committed_overspend_cents": 4200})
    assert "over budget by $42.00" in payload["message"]


def test_the_message_stays_inside_what_ntfy_will_carry():
    """The client truncates the body at 1000 characters; a full report fits."""
    payload = digest(
        review_count=3,
        report={"uncategorized_count": 4, "uncategorized_cents": 9900,
                "committed_overspend_cents": 4200},
        health=[
            {"account_name": f"A rather long account name {index}", "status": "error", "detail": "x"}
            for index in range(6)
        ],
        sections=only(
            "headline", "spending", "safe_to_spend", "pace",
            "projection", "commitments", "connections", "attention",
        ),
    )
    assert len(payload["message"]) < 1000


# --------------------------------------------------------- what to include


def test_every_block_can_be_switched_off():
    payload = digest(
        review_count=3,
        report={"committed_overspend_cents": 4200},
        health=[{"account_name": "Checking", "status": "error", "detail": "x"}],
        sections=only(),
    )
    # The date stays: a report with no date is not a report.
    assert payload["message"] == "Friday 21 August"


def test_a_block_left_out_of_the_settings_defaults_to_shown():
    """An older stored configuration must not silently empty the report."""
    payload = digest(sections={"pace": False})
    assert "free money left" in payload["message"]
    assert "an even pace" not in payload["message"]


def test_the_projection_is_off_until_it_is_asked_for():
    """The one block that is opt-in, so the default report stays short."""
    assert Settings().digest_sections["projection"] is False
    assert "On this pace" not in digest(sections=Settings().digest_sections)["message"]
    body = digest(
        report={"projected_remaining_cents": 96200},
        sections=only("projection"),
    )["message"]
    assert "On this pace the month ends with $962.00" in body


def test_a_projection_that_lands_short_says_so():
    body = digest(
        report={"projected_remaining_cents": -12500},
        sections=only("projection"),
    )["message"]
    assert "the month ends over by $125.00" in body


def test_hiding_the_connections_block_does_not_silence_the_alarm():
    """A broken bank connection is not a formatting preference."""
    payload = digest(
        health=[{"account_name": "Checking", "status": "error", "detail": "x"}],
        sections=only("headline"),
    )
    assert "Checking" not in payload["message"]
    assert payload["priority"] == 5
    assert "rotating_light" in payload["tags"]
    # It still reaches the stored payload, which is what the interface reads.
    assert payload["degraded_accounts"] == [{"account_name": "Checking", "status": "error"}]


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
    headline = payload["message"].splitlines()[2]
    assert headline == "$995.90 over budget  \u00b7  49%"
    assert "left" not in headline
