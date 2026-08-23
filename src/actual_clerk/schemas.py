from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EnqueueRequest(BaseModel):
    kind: Literal["sync", "categorize", "health", "digest"]
    # Categorize only: reach back over the whole retained history instead of
    # the recent window, for the first run against an existing budget.
    full: bool = False
    # Digest only: send it now to see what the morning looks like, without
    # spending the one delivery today's date is allowed.
    force: bool = False


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

    @field_validator("reason", "suggested_new_category")
    @classmethod
    def collapse_whitespace(cls, value: str) -> str:
        return " ".join(str(value).split())

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
            "type": "string",
            "description": "Only when category_number is 0: a category name this budget lacks.",
        },
    },
    "required": ["category_number", "confidence", "reason", "suggested_new_category"],
    "additionalProperties": False,
}
