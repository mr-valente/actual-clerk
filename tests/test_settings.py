from __future__ import annotations

import json
import zoneinfo

import pytest
from pydantic import ValidationError

from actual_clerk import config
from actual_clerk.config import (
    Settings,
    SettingsManager,
    load_persisted_settings,
)


def test_secrets_never_leave_through_the_settings_api():
    settings = Settings(
        actual_password="hunter2",
        simplefin_access_url="https://user:pass@bridge/simplefin",
        ntfy_token="tok",
    )
    public = settings.public_dict()
    serialized = json.dumps(public)
    assert "hunter2" not in serialized
    assert "pass@bridge" not in serialized
    assert public["actual_password_configured"] is True
    assert public["simplefin_access_url_configured"] is True
    assert public["ntfy_token_configured"] is True
    assert public["openai_api_key_configured"] is False


def test_persisting_keeps_the_secrets_the_public_view_hides():
    settings = Settings(actual_password="hunter2")
    assert settings.persisted_dict()["actual_password"] == "hunter2"


def test_urls_are_normalized_and_validated():
    assert Settings(actual_url="http://actual:5006/").actual_url == "http://actual:5006"
    with pytest.raises(ValidationError):
        Settings(actual_url="actual:5006")


def test_a_tag_that_could_not_round_trip_is_refused():
    assert Settings(clerk_tag="#clerk").clerk_tag == "clerk"
    with pytest.raises(ValidationError):
        Settings(clerk_tag="my clerk")


def test_provenance_tagging_needs_a_tag_to_write():
    with pytest.raises(ValidationError):
        Settings(clerk_tag="", tag_provenance=True)
    assert Settings(clerk_tag="", tag_provenance=False).clerk_tag == ""


def test_notifications_need_somewhere_to_go():
    with pytest.raises(ValidationError):
        Settings(notifications_enabled=True, ntfy_topic="")
    with pytest.raises(ValidationError):
        Settings(ntfy_topic="not a topic!")
    assert Settings(notifications_enabled=True, ntfy_topic="clerk-abc_1").ntfy_topic == "clerk-abc_1"


def test_the_model_context_must_leave_room_for_input():
    with pytest.raises(ValidationError):
        Settings(model_context_tokens=4096, model_max_output_tokens=4096)
    with pytest.raises(ValidationError):
        Settings(model_context_tokens=4096, model_max_output_tokens=3072)
    assert Settings(model_context_tokens=8192, model_max_output_tokens=2048)


def test_reasoning_effort_has_bounded_choices_and_an_environment_override(monkeypatch):
    assert Settings().model_reasoning == ""
    assert Settings(model_reasoning="high").model_reasoning == "high"
    with pytest.raises(ValidationError):
        Settings(model_reasoning="extreme")

    monkeypatch.setenv("CLERK_MODEL_REASONING", "off")
    settings = load_persisted_settings(json.dumps(Settings(model_reasoning="low").persisted_dict()))
    assert settings.model_reasoning == "off"
    assert "model_reasoning" in settings.public_dict()["environment_overrides"]


def test_the_digest_time_and_zone_are_validated():
    assert Settings(digest_time="7:5").digest_time == "07:05"
    assert Settings(timezone="America/New_York").zone.key == "America/New_York"
    with pytest.raises(ValidationError):
        Settings(digest_time="quarter past seven")
    with pytest.raises(ValidationError):
        Settings(timezone="Mars/Olympus")


def test_committed_groups_accept_a_list_or_a_comma_string():
    assert Settings(committed_groups="Bills, Housing").committed_groups == ["Bills", "Housing"]
    assert Settings(committed_groups=["Bills", "bills", " Bills "]).committed_groups == ["Bills"]
    assert Settings(committed_groups=[]).committed_groups == []


def test_money_settings_convert_to_cents():
    settings = Settings(monthly_income_override=4210.55, balance_tolerance=1.5)
    assert settings.monthly_income_override_cents == 421055
    assert settings.balance_tolerance_cents == 150


# ------------------------------------------------------------- environment


def test_the_container_environment_wins_over_saved_settings(monkeypatch):
    monkeypatch.setenv("ACTUAL_URL", "http://from-env:5006")
    saved = json.dumps(Settings(actual_url="http://from-ui:5006").persisted_dict())
    assert load_persisted_settings(saved).actual_url == "http://from-env:5006"


def test_an_absent_environment_value_leaves_the_saved_one_alone(monkeypatch):
    monkeypatch.delenv("ACTUAL_URL", raising=False)
    saved = json.dumps(Settings(actual_url="http://from-ui:5006").persisted_dict())
    assert load_persisted_settings(saved).actual_url == "http://from-ui:5006"


def test_an_empty_environment_value_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("ACTUAL_URL", "")
    saved = json.dumps(Settings(actual_url="http://from-ui:5006").persisted_dict())
    assert load_persisted_settings(saved).actual_url == "http://from-ui:5006"


def test_tz_seeds_a_fresh_install_but_never_overrides_a_chosen_zone(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    assert load_persisted_settings(None).timezone == "Europe/Berlin"
    saved = json.dumps(Settings(timezone="America/Chicago").persisted_dict())
    assert load_persisted_settings(saved).timezone == "America/Chicago"


def test_clerk_tz_does_override_a_chosen_zone(monkeypatch):
    monkeypatch.setenv("CLERK_TIMEZONE", "Europe/Berlin")
    saved = json.dumps(Settings(timezone="America/Chicago").persisted_dict())
    assert load_persisted_settings(saved).timezone == "Europe/Berlin"


def test_environment_managed_fields_are_named_so_the_ui_can_lock_them(monkeypatch):
    monkeypatch.setenv("ACTUAL_URL", "http://from-env:5006")
    monkeypatch.setenv("TZ", "Europe/Berlin")
    overrides = Settings().public_dict()["environment_overrides"]
    assert "actual_url" in overrides
    # TZ is a container convention, not a Clerk setting, so it stays editable.
    assert "timezone" not in overrides


# ----------------------------------------------------------------- manager


def test_the_manager_persists_and_reloads(database):
    manager = SettingsManager(database)
    manager.update({"sync_interval_minutes": 15})
    assert SettingsManager(database).get().sync_interval_minutes == 15


def test_the_manager_refuses_unknown_and_invalid_values(database):
    manager = SettingsManager(database)
    with pytest.raises(ValueError, match="Unknown settings"):
        manager.update({"nonsense": 1})
    with pytest.raises(ValueError):
        manager.update({"timezone": "Mars/Olympus"})
    # A rejected update leaves the previous configuration intact.
    assert manager.get().timezone == "UTC"


def test_container_managed_values_cannot_be_changed_from_the_ui(database, monkeypatch):
    monkeypatch.setenv("ACTUAL_URL", "http://from-env:5006")
    manager = SettingsManager(database)
    updated = manager.update({"actual_url": "http://from-ui:5006"})
    assert updated.actual_url == "http://from-env:5006"


def test_the_manager_hands_out_copies(database):
    manager = SettingsManager(database)
    first = manager.get()
    first.sync_interval_minutes = 999
    assert manager.get().sync_interval_minutes != 999


# ------------------------------------------------------------- the time zone


def test_tz_still_applies_when_no_zone_was_ever_chosen(monkeypatch):
    """The saved blob always carries a time zone, chosen or not.

    Every field is persisted, so "timezone" is present from the first save with
    the "UTC" default in it. Reading that as a deliberate choice made TZ a
    no-op on every restart after the first, which is what silently kept the
    morning digest on UTC time.
    """

    monkeypatch.setenv("TZ", "Europe/Berlin")
    saved = json.dumps(Settings().persisted_dict())
    assert json.loads(saved)["timezone"] == "UTC"
    assert load_persisted_settings(saved).timezone == "Europe/Berlin"
    # Once someone picks a zone in the interface, TZ stops reclaiming it.
    assert load_persisted_settings(saved, timezone_chosen=True).timezone == "UTC"


def test_a_zone_saved_before_the_marker_existed_is_grandfathered(monkeypatch):
    """An upgrade must not hand a working zone back to the container's TZ."""
    monkeypatch.setenv("TZ", "Europe/Berlin")
    saved = json.dumps(Settings(timezone="America/Chicago").persisted_dict())
    assert load_persisted_settings(saved).timezone == "America/Chicago"


def test_adding_tz_to_an_existing_install_takes_effect(database, monkeypatch):
    """Install first, discover the digest is on UTC, add TZ, restart."""
    monkeypatch.delenv("TZ", raising=False)
    manager = SettingsManager(database)
    assert manager.get().timezone == "UTC"
    manager.update({"digest_time": "07:30"})  # an unrelated save still happens

    monkeypatch.setenv("TZ", "Europe/Berlin")
    assert SettingsManager(database).get().timezone == "Europe/Berlin"


def test_a_zone_chosen_in_the_interface_survives_a_restart(database, monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    manager = SettingsManager(database)
    assert manager.get().timezone == "Europe/Berlin"
    manager.update({"timezone": "America/Chicago"})
    assert SettingsManager(database).get().timezone == "America/Chicago"


def test_clerk_timezone_outranks_a_zone_chosen_in_the_interface(database, monkeypatch):
    monkeypatch.setenv("CLERK_TIMEZONE", "Europe/Berlin")
    manager = SettingsManager(database)
    manager.update({"timezone": "America/Chicago"})
    assert SettingsManager(database).get().timezone == "Europe/Berlin"


def test_a_container_with_no_tz_database_says_so_instead_of_blaming_the_name(monkeypatch):
    def missing(key):
        raise zoneinfo.ZoneInfoNotFoundError(key)

    monkeypatch.setattr(config.zoneinfo, "ZoneInfo", missing)
    assert config.tz_database_available() is False
    with pytest.raises(ValidationError, match="no time zone database"):
        Settings(timezone="America/Chicago")


def test_a_real_typo_is_still_reported_as_a_typo():
    assert config.tz_database_available() is True
    with pytest.raises(ValidationError, match="unknown time zone"):
        Settings(timezone="Mars/Olympus")


# --------------------------------------------------------- the morning report


def test_the_report_header_is_named_and_never_left_blank(database):
    assert Settings().digest_title == "The Morning Report"
    assert Settings(digest_title="  Budget   o'clock ").digest_title == "Budget o'clock"
    # A blank header would leave ntfy showing the topic name instead.
    assert Settings(digest_title="   ").digest_title == "The Morning Report"


def test_the_include_switches_reach_the_builder_by_block_name(database):
    manager = SettingsManager(database)
    manager.update({"digest_show_pace": False, "digest_show_projection": True})
    sections = manager.get().digest_sections
    assert sections["pace"] is False
    assert sections["projection"] is True
    assert sections["headline"] is True
    # Every switch is offered, and none is named after its field.
    assert not any(name.startswith("digest_show_") for name in sections)
