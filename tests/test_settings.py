from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from actual_clerk.config import Settings, SettingsManager, load_persisted_settings


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
