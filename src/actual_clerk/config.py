from __future__ import annotations

import json
import os
import threading
import zoneinfo
from datetime import time as clock_time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

# Actual stores every amount as an integer number of cents.
CENTS = 100


class Settings(BaseModel):
    """Runtime settings.

    Values changed in the UI are persisted in SQLite. Explicit environment
    values remain authoritative, and secret values are never returned by the
    settings API.
    """

    # --- Actual Budget -----------------------------------------------------
    actual_url: str = "http://actual_server:5006"
    actual_password: SecretStr = SecretStr("")
    actual_budget_id: str = Field(default="", max_length=100)
    actual_encryption_password: SecretStr = SecretStr("")
    actual_verify_ssl: bool = True

    # --- SimpleFIN --------------------------------------------------------
    # The access URL embeds basic-auth credentials, so it is always a secret.
    # A setup token may be pasted instead; Clerk claims it once and stores the
    # resulting access URL.
    simplefin_access_url: SecretStr = SecretStr("")
    simplefin_setup_token: SecretStr = SecretStr("")

    # --- Plaid ------------------------------------------------------------
    # The bank feed Clerk delivers itself. The secret belongs to one Plaid
    # environment; switching environments means a different secret and a
    # fresh set of Items.
    plaid_client_id: str = Field(default="", max_length=100)
    plaid_secret: SecretStr = SecretStr("")
    plaid_env: Literal["sandbox", "production"] = "sandbox"
    # History requested when an Item is first linked; Plaid allows 1 to 730.
    plaid_days_requested: int = Field(default=90, ge=1, le=730)
    # Required only for OAuth institutions: an https URL registered in the
    # Plaid dashboard that returns the browser to Clerk's own page.
    plaid_redirect_uri: str = ""
    plaid_client_name: str = Field(default="Actual Clerk", max_length=30)
    # The sync engine. Refresh asks Plaid to extract now instead of on its own
    # one-to-four-times-a-day schedule; Clerk then waits, bounded, for the
    # Item to report a newer successful update before reading the stream.
    plaid_sync_enabled: bool = True
    plaid_refresh_enabled: bool = True
    plaid_refresh_min_interval_minutes: int = Field(default=55, ge=1, le=1440)
    plaid_refresh_wait_seconds: int = Field(default=45, ge=0, le=300)
    # A pending charge the bank withdrew is deleted from Actual only while it
    # is still uncleared and unreconciled; a cleared row is never deleted.
    plaid_delete_removed_pending: bool = True
    # First import into an empty Actual account adds an opening balance so the
    # account matches the bank, the way Actual's own linking does.
    plaid_starting_balance: bool = True
    # How far either side of the cutover date a foreign-id row may be adopted.
    plaid_adopt_window_days: int = Field(default=14, ge=0, le=90)

    # --- Local OpenAI-compatible endpoint ---------------------------------
    openai_base_url: str = "http://host.docker.internal:11434/v1"
    openai_api_key: SecretStr = SecretStr("")
    model: str = "qwen2.5:14b"
    # Clerk asks the model bounded questions about records it supplies rather
    # than open-ended problems. Empty leaves the server's own default in place.
    model_reasoning: Literal["", "off", "low", "medium", "high"] = ""
    model_context_tokens: int = Field(default=16384, ge=2048, le=1_000_000)
    model_max_output_tokens: int = Field(default=2048, ge=256, le=131_072)

    # --- Categorization ---------------------------------------------------
    categorization_enabled: bool = True
    # The cascade only reaches the model when cheaper, deterministic evidence
    # has already failed, so these two thresholds are separate on purpose.
    memory_min_confidence: float = Field(default=0.75, ge=0.0, le=1.0)
    memory_min_observations: int = Field(default=2, ge=1, le=50)
    # Retained for persisted/environment configuration compatibility. Model
    # confidence is still displayed, but no value can bypass human approval.
    ai_min_confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    ai_enabled: bool = True
    # Automatic applies only established merchant memory. A first-time
    # merchant that reaches the model always requires review.
    apply_mode: Literal["automatic", "review"] = "automatic"
    categorize_lookback_days: int = Field(default=45, ge=1, le=730)
    history_lookback_days: int = Field(default=730, ge=30, le=3650)
    ai_example_count: int = Field(default=8, ge=0, le=40)
    category_candidate_limit: int = Field(default=90, ge=10, le=400)
    allow_new_categories: bool = False

    # --- Rule promotion ---------------------------------------------------
    rule_promotion_enabled: bool = True
    rule_promote_after: int = Field(default=3, ge=2, le=25)

    # --- Tagging ----------------------------------------------------------
    tagging_enabled: bool = True
    clerk_tag: str = Field(default="clerk", max_length=40)
    tag_provenance: bool = True
    tag_anomalies: bool = True

    # --- Budget report ----------------------------------------------------
    # Empty means "every non-income category that carries a budget this month
    # is committed", which is the shape docs/budget-setup.md sets up.
    committed_groups: list[str] = Field(default_factory=list)
    monthly_income_override: float = Field(default=0.0, ge=0.0, le=10_000_000.0)
    income_lookback_months: int = Field(default=3, ge=1, le=12)
    budget_currency: str = Field(default="USD", min_length=3, max_length=3)

    # --- Sync and freshness ----------------------------------------------
    sync_enabled: bool = True
    sync_interval_minutes: int = Field(default=60, ge=5, le=1440)
    bank_sync_enabled: bool = True
    transaction_stale_days: int = Field(default=8, ge=1, le=365)

    # --- Connection health ------------------------------------------------
    health_interval_minutes: int = Field(default=60, ge=5, le=1440)
    balance_stale_hours: int = Field(default=36, ge=2, le=720)
    balance_tolerance: float = Field(default=1.0, ge=0.0, le=10_000.0)
    health_alerts_enabled: bool = True

    # --- Morning digest ---------------------------------------------------
    digest_enabled: bool = True
    digest_time: str = "07:30"
    timezone: str = "UTC"
    # The notification's header. Everything the report says lives in the body,
    # so this stays the same every morning and is recognisable at a glance.
    digest_title: str = Field(default="The Morning Report", max_length=80)
    digest_show_headline: bool = True
    digest_show_spending: bool = True
    digest_show_safe_to_spend: bool = True
    digest_show_pace: bool = True
    digest_show_projection: bool = False
    digest_show_commitments: bool = True
    # Account balances are more detailed than the default lock-screen report,
    # so they are deliberately opt-in as one complete monitored-account list.
    digest_show_balances: bool = False
    digest_show_connections: bool = True
    digest_show_attention: bool = True

    # --- Notifications ----------------------------------------------------
    notifications_enabled: bool = False
    ntfy_url: str = "https://ntfy.sh"
    ntfy_topic: str = Field(default="", max_length=64)
    ntfy_token: SecretStr = SecretStr("")

    # --- Reliability ------------------------------------------------------
    request_timeout_seconds: int = Field(default=180, ge=10, le=3600)
    model_max_retries: int = Field(default=3, ge=0, le=10)
    job_max_attempts: int = Field(default=3, ge=1, le=10)
    lease_seconds: int = Field(default=1800, ge=60, le=7200)

    # --- Interface --------------------------------------------------------
    appearance_theme: Literal["system", "light", "dark"] = "system"
    appearance_density: Literal["comfortable", "compact"] = "comfortable"
    appearance_motion: Literal["system", "full", "reduced"] = "system"

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("actual_url", "openai_base_url", "ntfy_url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("must start with http:// or https://")
        return value

    @field_validator("plaid_redirect_uri")
    @classmethod
    def validate_redirect_uri(cls, value: str) -> str:
        value = value.strip()
        if value and not value.startswith("https://"):
            raise ValueError("the Plaid redirect URI must be an https:// URL")
        return value

    @field_validator("plaid_client_id")
    @classmethod
    def strip_plaid_client_id(cls, value: str) -> str:
        return value.strip()

    @field_validator("plaid_client_name")
    @classmethod
    def normalize_plaid_client_name(cls, value: str) -> str:
        return " ".join(value.split()) or "Actual Clerk"

    @field_validator("clerk_tag")
    @classmethod
    def normalize_tag(cls, value: str) -> str:
        # Actual writes tags into notes as `#tag`, so a tag holding whitespace
        # or a leading hash would not round-trip.
        value = value.strip().lstrip("#")
        if value and any(character.isspace() for character in value):
            raise ValueError("the Clerk tag may not contain spaces")
        return value

    @field_validator("committed_groups", mode="before")
    @classmethod
    def split_groups(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("committed_groups")
    @classmethod
    def normalize_groups(cls, value: list[str]) -> list[str]:
        seen: dict[str, str] = {}
        for item in value:
            name = " ".join(str(item).split())
            if name:
                seen.setdefault(name.casefold(), name)
        return list(seen.values())

    @field_validator("budget_currency")
    @classmethod
    def upper_currency(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("digest_title")
    @classmethod
    def normalize_digest_title(cls, value: str) -> str:
        # A blank header would leave ntfy showing the topic name instead.
        return " ".join(value.split()) or "The Morning Report"

    @field_validator("digest_time")
    @classmethod
    def validate_digest_time(cls, value: str) -> str:
        # Accept `7:5` as readily as `07:05`; both mean the same thing to a
        # person typing into a text field.
        parts = value.strip().split(":")
        if len(parts) != 2 or not all(part.strip().isdigit() for part in parts):
            raise ValueError("digest time must be written as HH:MM")
        hour, minute = (int(part) for part in parts)
        try:
            parsed = clock_time(hour=hour, minute=minute)
        except ValueError as exc:
            raise ValueError("digest time must be a real time of day") from exc
        return f"{parsed.hour:02d}:{parsed.minute:02d}"

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        value = value.strip() or "UTC"
        try:
            zoneinfo.ZoneInfo(value)
        except zoneinfo.ZoneInfoNotFoundError as exc:
            # A slim base image with no tz database rejects every name alike,
            # so "unknown time zone" would send the reader hunting for a typo
            # that is not there. Say which of the two it actually is.
            if not tz_database_available():
                raise ValueError(
                    "this container has no time zone database, so only UTC resolves; "
                    "install the tzdata package"
                ) from exc
            raise ValueError(f"unknown time zone: {value}") from exc
        except ValueError as exc:
            raise ValueError(f"unknown time zone: {value}") from exc
        return value

    @field_validator("ntfy_topic")
    @classmethod
    def validate_ntfy_topic(cls, value: str) -> str:
        value = value.strip()
        if value and any(
            not (character.isascii() and character.isalnum()) and character not in "-_"
            for character in value
        ):
            raise ValueError(
                "ntfy topic may contain only letters, numbers, dashes, and underscores"
            )
        return value

    @model_validator(mode="after")
    def coherent_configuration(self) -> Settings:
        if self.notifications_enabled and not self.ntfy_topic:
            raise ValueError("an ntfy topic is required when notifications are enabled")
        if self.model_max_output_tokens >= self.model_context_tokens:
            raise ValueError("model output tokens must be smaller than the context limit")
        if self.model_context_tokens - self.model_max_output_tokens < 2_048:
            raise ValueError("the model context must reserve at least 2048 input tokens")
        if self.tag_provenance and not self.clerk_tag:
            raise ValueError("a Clerk tag is required when provenance tagging is enabled")
        return self

    @property
    def digest_clock(self) -> clock_time:
        return clock_time.fromisoformat(self.digest_time)

    @property
    def digest_sections(self) -> dict[str, bool]:
        """The `digest_show_*` switches, keyed by the block they control."""
        prefix = "digest_show_"
        return {
            name.removeprefix(prefix): bool(getattr(self, name))
            for name in Settings.model_fields
            if name.startswith(prefix)
        }

    @property
    def plaid_configured(self) -> bool:
        return bool(self.plaid_client_id and self.secret_value("plaid_secret"))

    @property
    def zone(self) -> zoneinfo.ZoneInfo:
        return zoneinfo.ZoneInfo(self.timezone)

    @property
    def monthly_income_override_cents(self) -> int:
        return round(self.monthly_income_override * CENTS)

    @property
    def balance_tolerance_cents(self) -> int:
        return round(self.balance_tolerance * CENTS)

    def secret_value(self, name: str) -> str:
        value = getattr(self, name)
        return value.get_secret_value() if isinstance(value, SecretStr) else str(value)

    def persisted_dict(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        for name in SECRET_FIELDS:
            data[name] = self.secret_value(name)
        return data

    def public_dict(self) -> dict[str, Any]:
        data = self.model_dump(mode="json", exclude=set(SECRET_FIELDS))
        for name in SECRET_FIELDS:
            data[f"{name}_configured"] = bool(self.secret_value(name))
        # Names only: this lets the UI explain why a container-managed value is
        # read-only without exposing any environment secrets. Only genuinely
        # locked fields are listed -- TZ seeds the time zone without owning it,
        # so the UI must not present it as read-only.
        data["environment_overrides"] = sorted(locked_fields())
        return data


SECRET_FIELDS = (
    "actual_password",
    "actual_encryption_password",
    "simplefin_access_url",
    "simplefin_setup_token",
    "plaid_secret",
    "openai_api_key",
    "ntfy_token",
)


ENVIRONMENT_FIELDS = {
    "ACTUAL_URL": "actual_url",
    "ACTUAL_PASSWORD": "actual_password",
    "ACTUAL_BUDGET_ID": "actual_budget_id",
    "ACTUAL_ENCRYPTION_PASSWORD": "actual_encryption_password",
    "ACTUAL_VERIFY_SSL": "actual_verify_ssl",
    "CLERK_SIMPLEFIN_ACCESS_URL": "simplefin_access_url",
    "CLERK_SIMPLEFIN_SETUP_TOKEN": "simplefin_setup_token",
    "CLERK_PLAID_CLIENT_ID": "plaid_client_id",
    "CLERK_PLAID_SECRET": "plaid_secret",
    "CLERK_PLAID_ENV": "plaid_env",
    "CLERK_PLAID_DAYS_REQUESTED": "plaid_days_requested",
    "CLERK_PLAID_REDIRECT_URI": "plaid_redirect_uri",
    "CLERK_PLAID_CLIENT_NAME": "plaid_client_name",
    "CLERK_PLAID_SYNC_ENABLED": "plaid_sync_enabled",
    "CLERK_PLAID_REFRESH_ENABLED": "plaid_refresh_enabled",
    "CLERK_PLAID_REFRESH_MIN_INTERVAL_MINUTES": "plaid_refresh_min_interval_minutes",
    "CLERK_PLAID_REFRESH_WAIT_SECONDS": "plaid_refresh_wait_seconds",
    "CLERK_PLAID_DELETE_REMOVED_PENDING": "plaid_delete_removed_pending",
    "CLERK_PLAID_STARTING_BALANCE": "plaid_starting_balance",
    "CLERK_PLAID_ADOPT_WINDOW_DAYS": "plaid_adopt_window_days",
    "CLERK_OPENAI_BASE_URL": "openai_base_url",
    "CLERK_OPENAI_API_KEY": "openai_api_key",
    "CLERK_MODEL": "model",
    "CLERK_MODEL_REASONING": "model_reasoning",
    "CLERK_MODEL_CONTEXT_TOKENS": "model_context_tokens",
    "CLERK_MODEL_MAX_OUTPUT_TOKENS": "model_max_output_tokens",
    "CLERK_CATEGORIZATION_ENABLED": "categorization_enabled",
    "CLERK_AI_ENABLED": "ai_enabled",
    "CLERK_APPLY_MODE": "apply_mode",
    "CLERK_MEMORY_MIN_CONFIDENCE": "memory_min_confidence",
    "CLERK_MEMORY_MIN_OBSERVATIONS": "memory_min_observations",
    "CLERK_AI_MIN_CONFIDENCE": "ai_min_confidence",
    "CLERK_CATEGORIZE_LOOKBACK_DAYS": "categorize_lookback_days",
    "CLERK_HISTORY_LOOKBACK_DAYS": "history_lookback_days",
    "CLERK_AI_EXAMPLE_COUNT": "ai_example_count",
    "CLERK_CATEGORY_CANDIDATE_LIMIT": "category_candidate_limit",
    "CLERK_ALLOW_NEW_CATEGORIES": "allow_new_categories",
    "CLERK_RULE_PROMOTION_ENABLED": "rule_promotion_enabled",
    "CLERK_RULE_PROMOTE_AFTER": "rule_promote_after",
    "CLERK_TAGGING_ENABLED": "tagging_enabled",
    "CLERK_TAG": "clerk_tag",
    "CLERK_TAG_PROVENANCE": "tag_provenance",
    "CLERK_TAG_ANOMALIES": "tag_anomalies",
    "CLERK_COMMITTED_GROUPS": "committed_groups",
    "CLERK_MONTHLY_INCOME": "monthly_income_override",
    "CLERK_INCOME_LOOKBACK_MONTHS": "income_lookback_months",
    "CLERK_CURRENCY": "budget_currency",
    "CLERK_SYNC_ENABLED": "sync_enabled",
    "CLERK_SYNC_INTERVAL_MINUTES": "sync_interval_minutes",
    "CLERK_BANK_SYNC_ENABLED": "bank_sync_enabled",
    "CLERK_TRANSACTION_STALE_DAYS": "transaction_stale_days",
    "CLERK_HEALTH_INTERVAL_MINUTES": "health_interval_minutes",
    "CLERK_BALANCE_STALE_HOURS": "balance_stale_hours",
    "CLERK_BALANCE_TOLERANCE": "balance_tolerance",
    "CLERK_HEALTH_ALERTS_ENABLED": "health_alerts_enabled",
    "CLERK_DIGEST_ENABLED": "digest_enabled",
    "CLERK_DIGEST_TIME": "digest_time",
    "CLERK_DIGEST_TITLE": "digest_title",
    "CLERK_TIMEZONE": "timezone",
    "TZ": "timezone",
    "CLERK_NOTIFICATIONS_ENABLED": "notifications_enabled",
    "CLERK_NTFY_URL": "ntfy_url",
    "CLERK_NTFY_TOPIC": "ntfy_topic",
    "CLERK_NTFY_TOKEN": "ntfy_token",
    "CLERK_REQUEST_TIMEOUT_SECONDS": "request_timeout_seconds",
    "CLERK_MODEL_MAX_RETRIES": "model_max_retries",
    "CLERK_JOB_MAX_ATTEMPTS": "job_max_attempts",
    "CLERK_LOG_LEVEL": "log_level",
}

# TZ is a container convention rather than a Clerk setting, so it seeds the
# time zone but never locks the field in the UI.
SOFT_ENVIRONMENT_NAMES = frozenset({"TZ"})

# Set once the time zone is chosen through the interface, which is what stops
# TZ from reclaiming it on the next restart.
TIMEZONE_CHOSEN_KEY = "timezone_chosen"


def environment_values(*, include_soft: bool = True) -> dict[str, str]:
    values: dict[str, str] = {}
    for name, field in ENVIRONMENT_FIELDS.items():
        if name in SOFT_ENVIRONMENT_NAMES and not include_soft:
            continue
        raw = os.environ.get(name, "")
        if raw != "" and field not in values:
            values[field] = raw
    return values


def locked_fields() -> set[str]:
    return set(environment_values(include_soft=False))


def settings_from_environment() -> Settings:
    return Settings.model_validate(environment_values())


def data_directory() -> Path:
    return Path(os.environ.get("CLERK_DATA_DIR", "./data")).expanduser().resolve()


def tz_database_available() -> bool:
    """Whether this interpreter can resolve any IANA zone at all.

    `python:*-slim` images ship without /usr/share/zoneinfo, and nothing says
    so until a zone name fails to resolve. The `tzdata` distribution is a
    declared dependency precisely so this stays True, but a hand-built image
    can still lack it, and that is worth reporting rather than guessing at.
    """

    try:
        zoneinfo.ZoneInfo("America/New_York")
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return False
    return True


def load_persisted_settings(raw_json: str | None, *, timezone_chosen: bool = False) -> Settings:
    """Rebuild the stored settings, letting the container environment win.

    `timezone_chosen` records that someone picked a time zone in the interface.
    TZ seeds an install that has never been told otherwise, but it must not
    keep overwriting a choice made in the UI -- and, just as importantly, the
    absence of a choice must not be inferred from the stored value, which is
    always populated (with "UTC") whether or not anyone ever chose it.
    """

    if not raw_json:
        return settings_from_environment()
    raw = json.loads(raw_json)
    # Explicit container environment remains authoritative on restart. Values
    # absent from the environment continue to use UI-persisted configuration.
    # TZ is the one exception: it seeds an install, but a time zone the user
    # chose in the UI outlives the container's own default. An install that
    # predates the marker is grandfathered by its own value: anything other
    # than the "UTC" default can only have come from a deliberate choice.
    chosen = timezone_chosen or raw.get("timezone", "UTC") != "UTC"
    raw.update(environment_values(include_soft=not chosen))
    return Settings.model_validate(raw)


class SettingsManager:
    def __init__(self, database: Any):
        self.database = database
        self._lock = threading.RLock()
        persisted = database.get_setting("runtime")
        self._settings = load_persisted_settings(
            persisted, timezone_chosen=database.get_setting(TIMEZONE_CHOSEN_KEY) == "1"
        )
        if persisted is None:
            database.set_setting("runtime", json.dumps(self._settings.persisted_dict()))

    def get(self) -> Settings:
        with self._lock:
            return self._settings.model_copy(deep=True)

    def update(self, values: dict[str, Any]) -> Settings:
        unknown = set(values) - set(Settings.model_fields)
        if unknown:
            raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}")
        with self._lock:
            merged = self._settings.persisted_dict()
            for key, value in values.items():
                merged[key] = value
            # Container-managed values stay authoritative for the lifetime of
            # the process as well as after a restart.
            merged.update(environment_values(include_soft=False))
            updated = Settings.model_validate(merged)
            self.database.set_setting("runtime", json.dumps(updated.persisted_dict()))
            if "timezone" in values:
                # Remember that this was a deliberate choice, so a restart does
                # not hand the zone back to whatever TZ the container carries.
                self.database.set_setting(TIMEZONE_CHOSEN_KEY, "1")
            self._settings = updated
            return updated.model_copy(deep=True)
