"""The categorization cascade.

Clerk answers the cheapest, most reliable question first and only escalates when
that fails:

1. **Rules.** The user has said where this merchant belongs. A rule is applied
   without thresholds and regardless of the apply mode; it is the one answer
   Clerk never second-guesses.
2. **Memory.** The same merchant has been filed before, in this budget, by this
   person. Recency-weighted evidence with a clear majority ends the matter.
3. **Model.** A merchant the budget has never seen goes to the local model,
   once per merchant rather than once per transaction, with the budget's own
   categories and its own filing habits as context.
4. **Review.** Anything the first three cannot settle confidently is queued for
   a human instead of guessed at.

Rules and memory are read through one resolver (`domain.intelligence`), which
anticipated charges share. The reverse direction exists too: a merchant Clerk
has filed the same way several times becomes a *proposal* to make a rule, so
the user's knowledge grows from evidence without Clerk ever asserting a rule
on its own.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from actual_clerk.clients.openai_compatible import ModelError, OpenAICompatibleClient
from actual_clerk.config import Settings
from actual_clerk.domain import tagging
from actual_clerk.domain.intelligence import SOURCE_RULE, RuleBook, alias_candidates, resolve
from actual_clerk.domain.memory import MerchantMemory, similar_examples
from actual_clerk.prompts import (
    ALIAS_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_alias_prompt,
    build_user_prompt,
)
from actual_clerk.schemas import (
    ALIAS_CHOICE_SCHEMA,
    CATEGORY_CHOICE_SCHEMA,
    AliasChoice,
    CategoryChoice,
)

# An alias the model is less sure of than this is not worth a person's time.
ALIAS_MIN_CONFIDENCE = 0.7

SOURCE_MEMORY = "memory"
SOURCE_MODEL = "model"
# SOURCE_RULE is defined with the resolver and re-exported here for callers.
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
    rule_id: str = ""

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
    # Merchants the model believes are ones the budget knows by another name.
    alias_proposals: list[dict[str, Any]] = field(default_factory=list)
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
            "alias_proposals": len(self.alias_proposals),
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
        rules: RuleBook | None = None,
        aliases: Mapping[str, str] | None = None,
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
                rules=rules,
                aliases=aliases or {},
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
        rules: RuleBook | None,
        aliases: Mapping[str, str],
        candidates: Sequence[dict[str, Any]],
        candidate_ids: set[str],
        names: dict[str, str],
        history: Sequence[dict[str, Any]],
        result: CategorizationResult,
    ) -> dict[str, Any]:
        settings = self.settings
        real_key = "" if merchant_key.startswith(" ") else merchant_key

        # A merchant is one question, but the account can change the answer
        # for a scoped rule; the first transaction's account stands for all.
        account_id = str(items[0].get("account_id") or "") if items else ""
        resolution = (
            resolve(
                real_key,
                account_id=account_id,
                rules=rules,
                memory=memory,
                aliases=aliases,
                min_observations=settings.memory_min_observations,
                min_confidence=settings.memory_min_confidence,
                allowed_categories=candidate_ids or None,
                names=names,
            )
            if real_key
            else None
        )
        if resolution is not None:
            return {
                "source": resolution.source,
                "category_id": resolution.category_id,
                "category_name": resolution.category_name,
                "confidence": resolution.confidence,
                # A rule applies regardless of the apply mode; memory may
                # apply only when the evidence meets the automatic thresholds,
                # and a lone exact sighting is a review-only suggestion.
                "automatic_eligible": resolution.automatic,
                "rule_id": resolution.rule.id if resolution.rule else "",
                "rationale": resolution.rationale,
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
        hints = [
            {
                "merchant_key": rule.merchant_key,
                "merchant_label": rule.merchant_label,
                "category_name": names.get(rule.category_id, rule.category_name),
            }
            for rule in (rules.related(real_key) if rules is not None and real_key else [])
        ]
        prompt = build_user_prompt(
            merchant=merchant,
            candidates=candidates,
            examples=examples,
            currency=settings.budget_currency,
            rule_hints=hints,
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

        if settings.ai_alias_questions and real_key:
            await self._ask_same_merchant(
                model,
                merchant=merchant,
                merchant_key=real_key,
                known_keys=[
                    *memory.keys,
                    *(rule.merchant_key for rule in (rules.rules if rules is not None else [])),
                    *aliases.values(),
                ],
                result=result,
            )

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

    async def _ask_same_merchant(
        self,
        model: OpenAICompatibleClient,
        *,
        merchant: dict[str, Any],
        merchant_key: str,
        known_keys: Sequence[str],
        result: CategorizationResult,
    ) -> None:
        """Ask whether an unfamiliar merchant is a known one under another name.

        A yes is recorded as an alias proposal for the user, never as an
        alias; a bad answer is simply dropped, since this question is a
        courtesy rather than part of filing.
        """

        candidates = alias_candidates(merchant_key, known_keys)
        if not candidates:
            return
        try:
            result.model_calls += 1
            raw = await model.structured(
                name="alias_choice",
                schema=ALIAS_CHOICE_SCHEMA,
                system=ALIAS_SYSTEM_PROMPT,
                user=build_alias_prompt(merchant=merchant, candidates=candidates),
            )
            choice = AliasChoice.model_validate(raw)
        except (ModelError, ValueError):
            return
        if not 0 < choice.candidate_number <= len(candidates):
            return
        if choice.confidence < ALIAS_MIN_CONFIDENCE:
            return
        result.alias_proposals.append(
            {
                "alias_key": merchant_key,
                "alias_label": merchant.get("label") or merchant_key,
                "merchant_key": candidates[choice.candidate_number - 1],
                "confidence": choice.confidence,
                "reason": choice.reason,
            }
        )

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
        is_rule = resolution["source"] == SOURCE_RULE
        threshold = (
            0.0
            if is_rule
            else settings.memory_min_confidence
            if resolution["source"] == SOURCE_MEMORY
            else settings.ai_min_confidence
        )
        confident = bool(category_id) and confidence >= threshold
        # A model answer is a proposal, never permission to alter the budget.
        # Only this person's established filing history can auto-apply. Once a
        # model proposal is approved, that decision becomes memory for future
        # transactions from the same merchant. A rule is the person's own
        # word and is applied even when the apply mode holds memory back.
        automatic_eligible = resolution.get(
            "automatic_eligible", resolution["source"] == SOURCE_MEMORY
        )
        status = (
            STATUS_APPLIED
            if bool(category_id) and is_rule
            else STATUS_APPLIED
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
            rule_id=str(resolution.get("rule_id") or ""),
            rationale={
                **resolution.get("rationale", {}),
                "threshold": threshold,
                "apply_mode": settings.apply_mode,
                "approval_required": status == STATUS_REVIEW,
            },
        )


def rule_proposal_candidates(
    proposals: Sequence[Proposal],
    stored_memory: dict[str, list[dict[str, Any]]],
    *,
    promote_after: int,
    rules: RuleBook | None = None,
) -> list[dict[str, Any]]:
    """Merchants Clerk has now filed the same way often enough to ask for a rule.

    A merchant that already has a rule, or was filed by one, is not asked
    about again; a merchant filed two different ways is not settled enough.
    The result is a proposal for the user, never a rule: Clerk's evidence can
    earn the question but only a person can give the answer.
    """

    seen: dict[str, dict[str, Any]] = {}
    for proposal in proposals:
        if proposal.status != STATUS_APPLIED or not proposal.category_id:
            continue
        if not proposal.merchant_key or proposal.source == SOURCE_RULE:
            continue
        if rules is not None and rules.has_merchant(proposal.merchant_key):
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
        candidates.append(
            {
                "merchant_key": merchant_key,
                "merchant_label": entry["merchant_label"],
                "category_id": entry["category_id"],
                "category_name": entry["category_name"],
                "descriptors": entry["descriptors"][:4],
                "observations": observations,
            }
        )
    candidates.sort(key=lambda item: item["observations"], reverse=True)
    return candidates
