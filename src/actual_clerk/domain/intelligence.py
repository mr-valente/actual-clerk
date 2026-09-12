"""What Clerk knows about a transaction's meaning, and how it answers.

Everything Clerk knows is one of four things: an *alias* saying two merchant
keys name the same shop, a *rule* the user has declared, *evidence* learned
from the user's own filed history, and *observations* of what the user did in
Actual afterwards. One resolver reads them in a fixed order:

1. Canonicalize the key through the alias table.
2. A rule for the merchant, scoped to the account first, then to any account.
   A rule is the user's word: confidence 1.0, applied without thresholds and
   regardless of the apply mode.
3. Learned evidence meeting the configured thresholds.
4. One consistent exact sighting, as a review-only suggestion.

The caller may go on to ask the model; nothing here ever does. Steps 1 to 4
need no network and complete when the model and the bank are both down,
which is what lets anticipated charges and the filing cascade share them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from actual_clerk.domain.memory import MerchantMemory
from actual_clerk.domain.merchants import keys_related

SOURCE_RULE = "rule"
SOURCE_MEMORY = "memory"

MATCH_EXACT = "exact"
MATCH_FAMILY = "family"
MATCH_MODES = (MATCH_EXACT, MATCH_FAMILY)

STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_RETIRED = "retired"
RULE_STATUSES = (STATUS_ACTIVE, STATUS_PAUSED, STATUS_RETIRED)


@dataclass(frozen=True)
class Rule:
    id: str
    merchant_key: str
    category_id: str
    category_name: str = ""
    account_id: str = ""
    match: str = MATCH_EXACT
    merchant_label: str = ""
    source: str = "user"

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Rule:
        return cls(
            id=str(row.get("id") or ""),
            merchant_key=str(row.get("merchant_key") or ""),
            category_id=str(row.get("category_id") or ""),
            category_name=str(row.get("category_name") or ""),
            account_id=str(row.get("account_id") or ""),
            match=str(row.get("match") or MATCH_EXACT),
            merchant_label=str(row.get("merchant_label") or ""),
            source=str(row.get("source") or "user"),
        )

    def claims(self, merchant_key: str) -> bool:
        if not merchant_key or not self.merchant_key:
            return False
        if self.merchant_key == merchant_key:
            return True
        return self.match == MATCH_FAMILY and keys_related(self.merchant_key, merchant_key)


class RuleBook:
    """The active rules, indexed for the resolver.

    An exact rule beats a family one, and a rule scoped to the transaction's
    account beats one for any account. Among family rules the longest key
    wins, since `starbucks reserve` describes the merchant better than
    `starbucks` does.
    """

    def __init__(self, rules: Iterable[Rule] = ()) -> None:
        self._exact: dict[tuple[str, str], Rule] = {}
        self._family: list[Rule] = []
        for rule in rules:
            if not rule.merchant_key or not rule.category_id:
                continue
            self._exact.setdefault((rule.merchant_key, rule.account_id), rule)
            if rule.match == MATCH_FAMILY:
                self._family.append(rule)
        self._family.sort(key=lambda rule: -len(rule.merchant_key))

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, Any]]) -> RuleBook:
        return cls(Rule.from_row(row) for row in rows)

    def __len__(self) -> int:
        return len(self._exact)

    def __bool__(self) -> bool:
        return bool(self._exact)

    def has_merchant(self, merchant_key: str) -> bool:
        return any(key == merchant_key for key, _ in self._exact)

    def lookup(self, merchant_key: str, account_id: str = "") -> Rule | None:
        if not merchant_key:
            return None
        for scope in ((account_id, "") if account_id else ("",)):
            rule = self._exact.get((merchant_key, scope))
            if rule is not None:
                return rule
        for scope in ((account_id, "") if account_id else ("",)):
            for rule in self._family:
                if rule.account_id == scope and rule.claims(merchant_key):
                    return rule
        return None


@dataclass
class Resolution:
    """Where an answer came from and whether Clerk may act on it alone."""

    source: str
    category_id: str
    category_name: str
    confidence: float
    automatic: bool
    rationale: dict[str, Any] = field(default_factory=dict)
    rule: Rule | None = None

    @property
    def is_rule(self) -> bool:
        return self.source == SOURCE_RULE


def canonical_key(merchant_key: str, aliases: Mapping[str, str]) -> str:
    """Follow one alias hop; the alias table is kept acyclic on write."""
    if not merchant_key:
        return ""
    return aliases.get(merchant_key) or merchant_key


def resolve(
    merchant_key: str,
    *,
    account_id: str = "",
    rules: RuleBook | None = None,
    memory: MerchantMemory | None = None,
    aliases: Mapping[str, str] | None = None,
    min_observations: float = 2.0,
    min_confidence: float = 0.75,
    allowed_categories: set[str] | None = None,
    automatic_memory: bool = True,
    names: Mapping[str, str] | None = None,
) -> Resolution | None:
    """Answer from rules and evidence, or return None so the caller may escalate."""

    aliases = aliases or {}
    names = names or {}
    key = canonical_key(merchant_key, aliases)
    if not key:
        return None
    keys = [key] if key == merchant_key else [key, merchant_key]

    if rules is not None:
        for candidate in keys:
            rule = rules.lookup(candidate, account_id)
            if rule is None:
                continue
            if allowed_categories is not None and rule.category_id not in allowed_categories:
                # The category is gone from Actual; the rule needs repair and
                # must not file anything until it has it.
                continue
            return Resolution(
                source=SOURCE_RULE,
                category_id=rule.category_id,
                category_name=names.get(rule.category_id, rule.category_name),
                confidence=1.0,
                automatic=True,
                rationale={
                    "rule": {
                        "id": rule.id,
                        "merchant_key": rule.merchant_key,
                        "match": rule.match,
                        "account_id": rule.account_id,
                        "source": rule.source,
                    },
                    "matched_key": candidate,
                    "alias": key if key != merchant_key else "",
                    "reason": "A rule you set files this merchant here.",
                },
                rule=rule,
            )

    if memory is None:
        return None
    for candidate in keys:
        match = memory.lookup(
            candidate,
            min_observations=min_observations,
            min_confidence=min_confidence,
            allowed_categories=allowed_categories,
        )
        if match is not None:
            return Resolution(
                source=SOURCE_MEMORY,
                category_id=match.category_id,
                category_name=names.get(match.category_id, match.category_name),
                confidence=match.confidence,
                automatic=automatic_memory,
                rationale={
                    "memory": match.as_dict(),
                    "evidence": memory.evidence(candidate)[:5],
                    "alias": key if key != merchant_key and candidate == key else "",
                },
            )
    for candidate in keys:
        suggestion = memory.exact_suggestion(candidate, allowed_categories=allowed_categories)
        if suggestion is not None:
            return Resolution(
                source=SOURCE_MEMORY,
                category_id=suggestion.category_id,
                category_name=names.get(suggestion.category_id, suggestion.category_name),
                confidence=suggestion.confidence,
                automatic=False,
                rationale={
                    "memory": suggestion.as_dict(),
                    "evidence": memory.evidence(candidate)[:5],
                    "provisional": True,
                    "reason": "One exact prior filing suggests this category; approval is required.",
                },
            )
    return None
