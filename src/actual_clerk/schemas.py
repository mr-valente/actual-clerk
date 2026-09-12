from __future__ import annotations

import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EnqueueRequest(BaseModel):
    kind: Literal["sync", "categorize", "health", "digest"]
    # Categorize only: reach back over the whole retained history instead of
    # the recent window, for the first run against an existing budget.
    full: bool = False
    # Categorize only: reconsider exactly the transactions already waiting in
    # Review. Ordinary scheduled runs leave these exceptions stable.
    reviews: bool = False
    # Digest only: send it now to see what the morning looks like, without
    # spending the one delivery today's date is allowed.
    force: bool = False

    @model_validator(mode="after")
    def one_categorization_scope(self) -> EnqueueRequest:
        if self.kind == "categorize" and self.full and self.reviews:
            raise ValueError("choose either full history or the review queue, not both")
        return self


class SettingsPatch(BaseModel):
    values: dict[str, Any]


class BulkResolveRequest(BaseModel):
    """Resolve a whole merchant at once, which is how a backlog is actually cleared."""

    ids: list[str] = Field(min_length=1, max_length=2000)
    action: Literal["accept", "dismiss", "recategorize"]
    category_id: str | None = None

    @model_validator(mode="after")
    def category_required_for_recategorize(self) -> BulkResolveRequest:
        if self.action == "recategorize" and not self.category_id:
            raise ValueError("a category is required when recategorizing")
        return self


class ClaimSetupTokenRequest(BaseModel):
    setup_token: str = Field(min_length=8, max_length=4000)


class ResolveDecisionRequest(BaseModel):
    action: Literal["accept", "dismiss", "recategorize"]
    category_id: str | None = None

    @model_validator(mode="after")
    def category_required_for_recategorize(self) -> ResolveDecisionRequest:
        if self.action == "recategorize" and not self.category_id:
            raise ValueError("a category is required when recategorizing")
        return self


class ResolveRuleRequest(BaseModel):
    action: Literal["create", "decline"]


class MonitoringRequest(BaseModel):
    monitored: bool
    account_name: str = Field(default="", max_length=200)


class CreateCategoryRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    group_name: str = Field(min_length=1, max_length=100)

    @field_validator("name", "group_name")
    @classmethod
    def normalize(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("may not be blank")
        return normalized


# ---------------------------------------------------------------- model output


class CategoryChoice(StrictModel):
    """One merchant classified against the budget's existing categories."""

    category_number: int = Field(ge=0, le=999)
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(default="", max_length=400)
    suggested_new_category: str = Field(default="", max_length=100)

    @field_validator("reason", "suggested_new_category", mode="before")
    @classmethod
    def empty_optional_text(cls, value: Any) -> Any:
        """Treat model JSON null like an omitted optional explanation."""
        return "" if value is None else value

    @field_validator("reason", "suggested_new_category")
    @classmethod
    def collapse_whitespace(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("confidence", mode="before")
    @classmethod
    def clamp_confidence(cls, value: Any) -> Any:
        """Small models sometimes answer with a percentage instead of a ratio."""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        if number > 1.0:
            number = number / 100 if number <= 100 else 1.0
        return max(0.0, min(1.0, number))


CATEGORY_CHOICE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "category_number": {
            "type": "integer",
            "description": "Number of the chosen category, or 0 if none of them fit.",
        },
        "confidence": {
            "type": "number",
            "description": "How certain the choice is, between 0 and 1.",
        },
        "reason": {"type": "string", "description": "One short sentence explaining the choice."},
        "suggested_new_category": {
            "type": ["string", "null"],
            "description": (
                "Only when category_number is 0: a category name this budget lacks; "
                "otherwise an empty string or null."
            ),
        },
    },
    "required": ["category_number", "confidence", "reason", "suggested_new_category"],
    "additionalProperties": False,
}


# ------------------------------------------------------------------- Plaid


class LinkTokenRequest(BaseModel):
    # Naming an Item asks for Link's update mode, which repairs that Item's
    # login without creating a new one.
    item_id: str = Field(default="", max_length=100)
    account_selection: bool = False


class LinkAccountMetadata(BaseModel):
    id: str = Field(default="", max_length=100)
    name: str = Field(default="", max_length=200)
    mask: str = Field(default="", max_length=10)
    type: str = Field(default="", max_length=40)
    subtype: str = Field(default="", max_length=40)


class ExchangeRequest(BaseModel):
    public_token: str = Field(min_length=8, max_length=200)
    institution_id: str = Field(default="", max_length=100)
    institution_name: str = Field(default="", max_length=200)
    accounts: list[LinkAccountMetadata] = Field(default_factory=list)


class SandboxItemRequest(BaseModel):
    institution_id: str = Field(default="ins_109508", max_length=40)
    username: str = Field(default="user_transactions_dynamic", max_length=80)


class NewActualAccount(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    off_budget: bool = False

    @field_validator("name")
    @classmethod
    def normalize(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("may not be blank")
        return normalized


class CreateLinkRequest(BaseModel):
    """Map one Plaid account onto an Actual account, existing or new."""

    item_id: str = Field(min_length=1, max_length=100)
    external_account_id: str = Field(min_length=1, max_length=100)
    actual_account_id: str = Field(default="", max_length=100)
    new_account: NewActualAccount | None = None
    # The first date Clerk imports from Plaid. Earlier history stays with
    # whatever fed the account before; blank means today.
    cutover_date: str = Field(default="", max_length=10)

    @field_validator("cutover_date")
    @classmethod
    def validate_cutover(cls, value: str) -> str:
        value = value.strip()
        if value:
            datetime.date.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def one_target(self) -> CreateLinkRequest:
        if bool(self.actual_account_id) == bool(self.new_account):
            raise ValueError("choose an existing Actual account or describe a new one, not both")
        return self


class UpdateLinkRequest(BaseModel):
    enabled: bool | None = None
    cutover_date: str | None = Field(default=None, max_length=10)

    @field_validator("cutover_date")
    @classmethod
    def validate_cutover(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if value:
            datetime.date.fromisoformat(value)
        return value


# --------------------------------------------------------------- migration


class MigrateToPlaidRequest(BaseModel):
    """Move one Actual account's feed from whatever Actual links to Plaid."""

    actual_account_id: str = Field(min_length=1, max_length=100)
    item_id: str = Field(min_length=1, max_length=100)
    external_account_id: str = Field(min_length=1, max_length=100)
    cutover_date: str = Field(default="", max_length=10)
    # Detach Actual's own link (SimpleFIN) so the account is fed once, by Clerk.
    unlink_actual: bool = True
    dry_run: bool = False

    @field_validator("cutover_date")
    @classmethod
    def validate_cutover(cls, value: str) -> str:
        value = value.strip()
        if value:
            datetime.date.fromisoformat(value)
        return value


class MigrateToSimpleFinRequest(BaseModel):
    """Hand an account back to Actual's own SimpleFIN link."""

    actual_account_id: str = Field(min_length=1, max_length=100)
    # Blank means the SimpleFIN account the mapping remembers from before.
    simplefin_account_id: str = Field(default="", max_length=200)
    starting_date: str = Field(default="", max_length=10)
    dry_run: bool = False

    @field_validator("starting_date")
    @classmethod
    def validate_starting(cls, value: str) -> str:
        value = value.strip()
        if value:
            datetime.date.fromisoformat(value)
        return value


class ServerTokenRequest(BaseModel):
    setup_token: str = Field(min_length=8, max_length=4000)


# ------------------------------------------------------ anticipated charges


class RegisterSourceRequest(BaseModel):
    """The phone points one app's notifications at one Actual account."""

    device_id: str = Field(min_length=1, max_length=100)
    device_name: str = Field(default="", max_length=120)
    package_name: str = Field(min_length=1, max_length=200)
    app_label: str = Field(default="", max_length=120)
    actual_account_id: str = Field(min_length=1, max_length=100)
    sample_title: str = Field(default="", max_length=400)
    sample_text: str = Field(default="", max_length=2000)
    # When the sample was posted on the phone. The notification a source is
    # registered from is usually the charge that prompted the registration,
    # so it is recorded as one rather than waiting for the next.
    sample_posted_at_ms: int = Field(default=0, ge=0)

    @field_validator("device_name", "app_label", "sample_title", "sample_text")
    @classmethod
    def collapse(cls, value: str) -> str:
        return " ".join(value.split())


class UpdateSourceRequest(BaseModel):
    actual_account_id: str | None = Field(default=None, max_length=100)
    enabled: bool | None = None


class ForwardNotificationRequest(BaseModel):
    """One notification as the phone saw it. Clerk does the reading."""

    device_id: str = Field(min_length=1, max_length=100)
    package_name: str = Field(min_length=1, max_length=200)
    # The phone's own identity for the notification, so a redelivery after a
    # retry or a reboot is the same charge. Blank lets Clerk derive one.
    notification_key: str = Field(default="", max_length=300)
    posted_at_ms: int = Field(default=0, ge=0)
    title: str = Field(default="", max_length=400)
    text: str = Field(default="", max_length=4000)

    @field_validator("title", "text")
    @classmethod
    def collapse(cls, value: str) -> str:
        return " ".join(value.split())

    @model_validator(mode="after")
    def something_to_read(self) -> ForwardNotificationRequest:
        if not self.title and not self.text:
            raise ValueError("a notification needs a title or a text")
        return self


class TeachCategoryRequest(BaseModel):
    # Blank clears a taught category and lets memory decide again.
    category_id: str = Field(default="", max_length=100)


class TeachAliasRequest(BaseModel):
    """The bank's payee name for a notification's merchant, e.g. "Steam" for "Valve"."""

    payee: str = Field(min_length=1, max_length=200)

    @field_validator("payee")
    @classmethod
    def collapse(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("may not be blank")
        return normalized
