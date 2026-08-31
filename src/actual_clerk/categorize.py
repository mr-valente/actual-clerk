"""The categorization cascade.

Clerk answers the cheapest, most reliable question first and only escalates when
that fails:

1. **Memory.** The same merchant has been filed before, in this budget, by this
   person. Recency-weighted evidence with a clear majority ends the matter.
2. **Model.** A merchant the budget has never seen goes to the local model,
   once per merchant rather than once per transaction, with the budget's own
   categories and its own filing habits as context.
3. **Review.** Anything the first two cannot settle confidently is queued for a
   human instead of guessed at.

Actual's own rules are not re-implemented here. Actual applies them during
import, so a transaction that reaches Clerk uncategorized is one no rule
claimed. What Clerk does add is the reverse direction: a merchant it has
categorized the same way several times is promoted into a real Actual rule, and
from then on the answer costs nothing at all.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from actual_clerk.clients.openai_compatible import ModelError, OpenAICompatibleClient
from actual_clerk.config import Settings
from actual_clerk.domain import tagging
from actual_clerk.domain.memory import MerchantMemory, similar_examples
from actual_clerk.domain.merchants import rule_match_value
from actual_clerk.prompts import SYSTEM_PROMPT, build_user_prompt
from actual_clerk.schemas import CATEGORY_CHOICE_SCHEMA, CategoryChoice

SOURCE_MEMORY = "memory"
SOURCE_MODEL = "model"
SOURCE_UNRESOLVED = "unresolved"

STATUS_APPLIED = "applied"
STATUS_REVIEW = "needs_review"

# A model server that is simply switched off should cost one run a handful of
# failed requests, not one failed request per unfamiliar merchant.
MODEL_FAILURE_LIMIT = 3


@dataclass
class Proposal:
    transaction_id: str
    merchant_key: str
    merchant_label: str
    payee_name: str
    account_id: str
    account_name: str
    transaction_date: str
    amount_cents: int
    source: str
    status: str
    confidence: float
    category_id: str | None = None
    category_name: str = ""
    proposed_category: str = ""
    tags: list[str] = field(default_factory=list)
    note_tags: list[str] = field(default_factory=list)
    rationale: dict[str, Any] = field(default_factory=dict)

    def as_decision(self, job_id: str | None) -> dict[str, Any]:
        return {
            "job_id": job_id,
            "transaction_id": self.transaction_id,
            "account_id": self.account_id,
            "account_name": self.account_name,
            "payee_name": self.payee_name or self.merchant_label,
            "merchant_key": self.merchant_key,
            "transaction_date": self.transaction_date,
            "amount_cents": self.amount_cents,
            "source": self.source,
            "status": self.status,
            "category_id": self.category_id,
            "category_name": self.category_name,
            "proposed_category": self.proposed_category,
            "confidence": self.confidence,
            "tags": self.tags,
            "rationale": self.rationale,
        }


@dataclass
class CategorizationResult:
    proposals: list[Proposal] = field(default_factory=list)
    updates: list[dict[str, Any]] = field(default_factory=list)
    model_calls: int = 0
    merchants_seen: int = 0
    suggested_categories: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    model_abandoned: bool = False

    @property
    def applied(self) -> list[Proposal]:
        return [item for item in self.proposals if item.status == STATUS_APPLIED]

    @property
    def review(self) -> list[Proposal]:
        return [item for item in self.proposals if item.status == STATUS_REVIEW]

    def summary(self) -> dict[str, Any]:
        by_source: dict[str, int] = {}
        for proposal in self.applied:
            by_source[proposal.source] = by_source.get(proposal.source, 0) + 1
        return {
            "considered": len(self.proposals),
            "applied": len(self.applied),
            "needs_review": len(self.review),
            "model_calls": self.model_calls,
            "merchants": self.merchants_seen,
            "model_abandoned": self.model_abandoned,
            "by_source": by_source,
            "suggested_categories": self.suggested_categories,
            "errors": self.errors,
        }


def build_memory(
    snapshot: dict[str, Any], stored: Sequence[dict[str, Any]] = (), *, today: datetime.date
) -> MerchantMemory:
    """Evidence from the user's own filed history plus Clerk's applied decisions."""
    history = [
        {
            "merchant_key": item["merchant_key"],
            "category_id": item["category_id"],
            "category_name": item["category_name"],
            "date": item["date"],
        }
        for item in snapshot["transactions"]
        if item.get("category_id")
        and item.get("merchant_key")
        and not item.get("is_transfer")
        and not item.get("is_starting_balance")
    ]
    memory = MerchantMemory().add_transactions(history, today)
    return memory.add_stored(stored, today)


def candidate_categories(
    snapshot: dict[str, Any], *, limit: int, include_income: bool = True
) -> list[dict[str, Any]]:
    """The categories offered to the model, most-used first and bounded.

    A large budget can hold more categories than a local model's context can
    absorb, so the list is trimmed by how much the person actually uses each
    one, which is also the order in which they are most likely to be right.
    """

    usage: dict[str, int] = {}
    for item in snapshot["transactions"]:
        category_id = item.get("category_id")
        if category_id:
            usage[category_id] = usage.get(category_id, 0) + 1
    candidates = [
        category
        for category in snapshot["categories"]
        if str(category.get("name") or "").strip()
        and not category["hidden"]
        and (include_income or not category["is_income"])
    ]
    candidates.sort(key=lambda item: (-usage.get(item["id"], 0), item["name"].casefold()))
    return candidates[:limit]


def select_targets(
    snapshot: dict[str, Any],
    *,
    today: datetime.date,
    lookback_days: int,
    exclude_transaction_ids: set[str] | None = None,
    only_transaction_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Uncategorized on-budget spending recent enough to still matter."""
    exclude = exclude_transaction_ids or set()
    cutoff = today - datetime.timedelta(days=lookback_days)
    targets = [
        item
        for item in snapshot["transactions"]
        if not item.get("category_id")
        and (
            item["id"] in only_transaction_ids
            if only_transaction_ids is not None
            else item["date"] >= cutoff
        )
        and not item.get("off_budget")
        and not item.get("is_transfer")
        and not item.get("is_starting_balance")
        and not item.get("closed_account")
        and item["id"] not in exclude
    ]
    targets.sort(key=lambda item: (item["date"], item["id"]), reverse=True)
    return targets


def group_by_merchant(targets: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """One model question per merchant, not per transaction.

    A merchant determines the category; twelve coffees do not need twelve
    answers. Transactions whose descriptor normalizes to nothing keep their own
    bucket, marked with a leading space so it can never collide with a real key,
    so they are still reviewed individually.
    """

    grouped: dict[str, list[dict[str, Any]]] = {}
    for target in targets:
        key = target.get("merchant_key") or f" {target['id']}"
        grouped.setdefault(key, []).append(target)
    return grouped


def merchant_amount_stats(snapshot: dict[str, Any]) -> dict[str, tagging.MerchantStats]:
    amounts: dict[str, list[int]] = {}
    for item in snapshot["transactions"]:
        key = item.get("merchant_key")
        if key and not item.get("is_transfer"):
            amounts.setdefault(key, []).append(item["amount_cents"])
    return {key: tagging.MerchantStats.from_amounts(values) for key, values in amounts.items()}


class Categorizer:
    def __init__(self, settings: Settings, *, model_client: OpenAICompatibleClient | None = None):
        self.settings = settings
        self._model = model_client
        self._owns_model = model_client is None
        self._model_failures = 0

    def _ensure_model(self) -> OpenAICompatibleClient | None:
        if not self.settings.ai_enabled or not self.settings.model:
            return None
        if self._model is None:
            self._model = OpenAICompatibleClient(self.settings)
        return self._model

    async def close(self) -> None:
        if self._owns_model and self._model is not None:
            await self._model.close()
            self._model = None

    async def run(
        self,
        snapshot: dict[str, Any],
        *,
        today: datetime.date,
        lookback_days: int | None = None,
        stored_memory: Sequence[dict[str, Any]] = (),
        exclude_transaction_ids: set[str] | None = None,
        only_transaction_ids: set[str] | None = None,
    ) -> CategorizationResult:
        result = CategorizationResult()
        settings = self.settings
        self._model_failures = 0
        targets = select_targets(
            snapshot,
            today=today,
            lookback_days=(
                settings.categorize_lookback_days if lookback_days is None else lookback_days
            ),
            exclude_transaction_ids=exclude_transaction_ids,
            only_transaction_ids=only_transaction_ids,
        )
        if not targets:
            return result

        memory = build_memory(snapshot, stored_memory, today=today)
        candidates = candidate_categories(snapshot, limit=settings.category_candidate_limit)
        candidate_ids = {candidate["id"] for candidate in candidates}
        names = {candidate["id"]: candidate["name"] for candidate in candidates}
        stats = merchant_amount_stats(snapshot)
        history = [
            item
            for item in snapshot["transactions"]
            if item.get("category_id") and item.get("merchant_key")
        ]

        grouped = group_by_merchant(targets)
        result.merchants_seen = len(grouped)

        for merchant_key, items in grouped.items():
            resolution = await self._resolve_merchant(
                merchant_key=merchant_key,
                items=items,
                memory=memory,
                candidates=candidates,
                candidate_ids=candidate_ids,
                names=names,
                history=history,
                result=result,
            )
            for item in items:
                result.proposals.append(
                    self._build_proposal(
                        item,
                        resolution=resolution,
                                stats=stats.get(merchant_key),
                    )
                )

        for proposal in result.proposals:
            if proposal.status == STATUS_APPLIED and (
                proposal.category_id or proposal.note_tags
            ):
                result.updates.append(
                    {
                        "transaction_id": proposal.transaction_id,
                        "category_id": proposal.category_id,
                        "add_tags": proposal.note_tags,
                    }
                )
        return result

    async def _resolve_merchant(
        self,
        *,
        merchant_key: str,
        items: Sequence[dict[str, Any]],
        memory: MerchantMemory,
        candidates: Sequence[dict[str, Any]],
        candidate_ids: set[str],
        names: dict[str, str],
        history: Sequence[dict[str, Any]],
        result: CategorizationResult,
    ) -> dict[str, Any]:
        settings = self.settings
        real_key = "" if merchant_key.startswith(" ") else merchant_key

        match = (
            memory.lookup(
                real_key,
                min_observations=settings.memory_min_observations,
                min_confidence=settings.memory_min_confidence,
                allowed_categories=candidate_ids or None,
            )
            if real_key
            else None
        )
        if match is not None:
            return {
                "source": SOURCE_MEMORY,
                "category_id": match.category_id,
                "category_name": names.get(match.category_id, match.category_name),
                "confidence": match.confidence,
                "rationale": {
                    "memory": match.as_dict(),
                    "evidence": memory.evidence(real_key)[:5],
                },
            }

        suggestion = (
            memory.exact_suggestion(real_key, allowed_categories=candidate_ids or None)
            if real_key
            else None
        )
        if suggestion is not None:
            return {
                "source": SOURCE_MEMORY,
                "category_id": suggestion.category_id,
                "category_name": names.get(suggestion.category_id, suggestion.category_name),
                "confidence": suggestion.confidence,
                # One exact prior filing is useful enough to propose, but not
                # enough to bypass the configured automatic-memory thresholds.
                "automatic_eligible": False,
                "rationale": {
                    "memory": suggestion.as_dict(),
                    "evidence": memory.evidence(real_key)[:5],
                    "provisional": True,
                    "reason": "One exact prior filing suggests this category; approval is required.",
                },
            }

        model = None if result.model_abandoned else self._ensure_model()
        if model is None or not candidates:
            return {
                "source": SOURCE_UNRESOLVED,
                "category_id": None,
                "category_name": "",
                "confidence": 0.0,
                "rationale": {
                    "reason": (
                        "No prior example of this merchant, and the local model is unavailable."
                    ),
                    "evidence": memory.evidence(real_key)[:5] if real_key else [],
                },
            }

        sample = list(items)[:6]
        merchant = {
            "key": real_key or "unknown",
            "label": next(
                (
                    item.get("merchant_label") or item.get("payee_name")
                    for item in sample
                    if item.get("merchant_label") or item.get("payee_name")
                ),
                real_key or "Unnamed transaction",
            ),
            "descriptions": list(
                dict.fromkeys(
                    item.get("imported_description") or item.get("payee_name") or ""
                    for item in sample
                )
            ),
            "amounts": [item["amount_cents"] for item in sample],
            "account_name": sample[0].get("account_name", "") if sample else "",
            "count": len(items),
        }
        examples = similar_examples(real_key, history, limit=settings.ai_example_count)
        prompt = build_user_prompt(
            merchant=merchant,
            candidates=candidates,
            examples=examples,
            currency=settings.budget_currency,
        )
        try:
            # This counts classification requests, including requests whose
            # response proves unusable. Transport retries remain an internal
            # detail of the model client.
            result.model_calls += 1
            raw = await model.structured(
                name="category_choice",
                schema=CATEGORY_CHOICE_SCHEMA,
                system=SYSTEM_PROMPT,
                user=prompt,
            )
            choice = CategoryChoice.model_validate(raw)
        except ModelError as exc:
            result.errors.append(f"{merchant['label']}: {exc}")
            self._record_model_failure(result)
            return self._unresolved(f"The model could not classify this merchant: {exc}")
        except ValueError as exc:
            result.errors.append(f"{merchant['label']}: invalid model output ({exc})")
            self._record_model_failure(result)
            return self._unresolved(f"The model returned an unusable answer: {exc}")
        self._model_failures = 0

        if choice.category_number <= 0 or choice.category_number > len(candidates):
            # A missing category is only worth raising when the user has said
            # they want to hear about it. Clerk never creates one by itself.
            suggestion = choice.suggested_new_category if settings.allow_new_categories else ""
            if suggestion:
                result.suggested_categories.append(
                    {
                        "name": suggestion,
                        "merchant": merchant["label"],
                        "merchant_key": real_key,
                        "reason": choice.reason,
                    }
                )
            return {
                "source": SOURCE_MODEL,
                "category_id": None,
                "category_name": "",
                "confidence": 0.0,
                "proposed_category": suggestion,
                "rationale": {
                    "reason": (
                        choice.reason
                        or "The model found no existing category for this merchant."
                    ),
                    "abstained": True,
                    "examples": [example.get("merchant_key") for example in examples],
                },
            }

        chosen = candidates[choice.category_number - 1]
        return {
            "source": SOURCE_MODEL,
            "category_id": chosen["id"],
            "category_name": chosen["name"],
            "confidence": choice.confidence,
            "rationale": {
                "reason": choice.reason,
                "candidate_count": len(candidates),
                "examples": [example.get("merchant_key") for example in examples],
            },
        }

    @staticmethod
    def _unresolved(reason: str) -> dict[str, Any]:
        return {
            "source": SOURCE_UNRESOLVED,
            "category_id": None,
            "category_name": "",
            "confidence": 0.0,
            "rationale": {"reason": reason},
        }

    def _record_model_failure(self, result: CategorizationResult) -> None:
        self._model_failures += 1
        if self._model_failures >= MODEL_FAILURE_LIMIT:
            result.model_abandoned = True
            result.errors.append(
                "Gave up on the model for this run after "
                f"{self._model_failures} consecutive failures; "
                "the remaining merchants are queued for review."
            )

    def _build_proposal(
        self,
        item: dict[str, Any],
        *,
        resolution: dict[str, Any],
        stats: tagging.MerchantStats | None,
    ) -> Proposal:
        settings = self.settings
        category_id = resolution.get("category_id")
        confidence = float(resolution.get("confidence") or 0.0)
        threshold = (
            settings.memory_min_confidence
            if resolution["source"] == SOURCE_MEMORY
            else settings.ai_min_confidence
        )
        confident = bool(category_id) and confidence >= threshold
        # A model answer is a proposal, never permission to alter the budget.
        # Only this person's established filing history can auto-apply. Once a
        # model proposal is approved, that decision becomes memory for future
        # transactions from the same merchant.
        automatic_eligible = resolution.get(
            "automatic_eligible", resolution["source"] == SOURCE_MEMORY
        )
        status = (
            STATUS_APPLIED
            if confident and automatic_eligible and settings.apply_mode == "automatic"
            else STATUS_REVIEW
        )

        tags: list[str] = []
        note_tags: list[str] = []
        if settings.tagging_enabled:
            tags = tagging.derive_tags(
                amount_cents=item["amount_cents"],
                merchant_key=item.get("merchant_key", ""),
                stats=stats,
                tag_anomalies=settings.tag_anomalies,
            )
        if status == STATUS_APPLIED and settings.tagging_enabled:
            # The provenance tag is only written alongside a real change, so a
            # note is never touched just to record that Clerk looked at it.
            note_tags = list(tags)
            if settings.tag_provenance and settings.clerk_tag:
                note_tags.append(settings.clerk_tag)

        return Proposal(
            transaction_id=item["id"],
            merchant_key=item.get("merchant_key", ""),
            merchant_label=item.get("merchant_label", ""),
            payee_name=item.get("payee_name", ""),
            account_id=item.get("account_id", ""),
            account_name=item.get("account_name", ""),
            transaction_date=item["date"].isoformat(),
            amount_cents=item["amount_cents"],
            source=resolution["source"],
            status=status,
            confidence=confidence,
            category_id=category_id,
            category_name=resolution.get("category_name", ""),
            proposed_category=resolution.get("proposed_category", ""),
            tags=tags,
            note_tags=note_tags,
            rationale={
                **resolution.get("rationale", {}),
                "threshold": threshold,
                "apply_mode": settings.apply_mode,
                "approval_required": status == STATUS_REVIEW,
            },
        )


def rule_promotion_candidates(
    proposals: Sequence[Proposal],
    stored_memory: dict[str, list[dict[str, Any]]],
    *,
    promote_after: int,
) -> list[dict[str, Any]]:
    """Merchants Clerk has now filed the same way often enough to hand to Actual.

    Promotion needs a match value that appears verbatim on the statement, so
    merchants whose key only exists after normalization are never promoted: a
    rule that can never fire is worse than no rule.
    """

    seen: dict[str, dict[str, Any]] = {}
    for proposal in proposals:
        if proposal.status != STATUS_APPLIED or not proposal.category_id:
            continue
        if not proposal.merchant_key:
            continue
        entry = seen.setdefault(
            proposal.merchant_key,
            {
                "merchant_key": proposal.merchant_key,
                "merchant_label": proposal.merchant_label or proposal.payee_name,
                "category_id": proposal.category_id,
                "category_name": proposal.category_name,
                "descriptors": [],
                "count": 0,
            },
        )
        if entry["category_id"] != proposal.category_id:
            entry["conflict"] = True
        entry["count"] += 1
        for descriptor in (proposal.merchant_label, proposal.payee_name):
            if descriptor and descriptor not in entry["descriptors"]:
                entry["descriptors"].append(descriptor)

    candidates: list[dict[str, Any]] = []
    for merchant_key, entry in seen.items():
        if entry.get("conflict"):
            continue
        history = stored_memory.get(merchant_key, [])
        observations = entry["count"] + sum(
            int(row.get("hits") or 0)
            for row in history
            if row.get("category_id") == entry["category_id"]
        )
        if observations < promote_after:
            continue
        match_value = rule_match_value(*entry["descriptors"])
        if not match_value:
            continue
        candidates.append(
            {
                "merchant_key": merchant_key,
                "merchant_label": entry["merchant_label"],
                "category_id": entry["category_id"],
                "category_name": entry["category_name"],
                "match_value": match_value,
                "observations": observations,
            }
        )
    candidates.sort(key=lambda item: item["observations"], reverse=True)
    return candidates
