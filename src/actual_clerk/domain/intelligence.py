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

from actual_clerk.domain.memory import MerchantMemory, token_similarity
from actual_clerk.domain.merchants import keys_related, merchant_label, normalize_merchant

SOURCE_RULE = "rule"
SOURCE_MEMORY = "memory"

# Where an alias came from.
ALIAS_TAUGHT = "taught"
ALIAS_SETTLED = "settled"
ALIAS_ACTUAL_PAYEE = "actual_payee"

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

    @property
    def rules(self) -> list[Rule]:
        return list(self._exact.values())

    def related(self, merchant_key: str, *, limit: int = 6) -> list[Rule]:
        """Rules for merchants that look like this one, closest first.

        Shown to the model as hints: a rule for `amazon` says a lot about
        `amazon fresh`, and the user's own rules are a better guide than the
        category names.
        """

        if not merchant_key:
            return []
        scored = []
        for rule in self._exact.values():
            score = (
                1.0
                if keys_related(rule.merchant_key, merchant_key)
                else token_similarity(rule.merchant_key, merchant_key)
            )
            if score > 0:
                scored.append((score, rule))
        scored.sort(key=lambda item: (-item[0], item[1].merchant_key))
        return [rule for _, rule in scored[:limit]]

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


# A pair has to recur before it is read as an alias rather than a one-off.
ALIAS_MIN_SIGHTINGS = 2
# Words that describe how money moved rather than where it went. A descriptor
# left with nothing else names the rail, and a rail is not a merchant.
_RAIL_TOKENS = frozenset(
    {
        "ach", "eft", "pos", "sig", "visa", "mastercard", "amex", "discover", "paypal", "pp",
        "venmo", "zelle", "inst", "xfer", "transfer", "tfr", "debit", "credit", "card",
        "consumer", "withdrawal", "deposit", "dep", "electronic", "payment", "pmnt", "pymt",
        "rcvd", "received", "recurring", "purchase", "pur", "online", "mobile", "internet",
        "check", "chk", "atm", "cash", "fee", "interest", "direct", "ext", "trn", "merchant",
        "location", "timestamp", "date", "retail", "store", "web", "ppd", "ccd", "pending",
    }
)


def _names_a_rail(key: str) -> bool:
    return all(token in _RAIL_TOKENS or len(token) <= 2 for token in key.split())


def _too_generic(key: str) -> bool:
    tokens = key.split()
    return not tokens or (len(tokens) == 1 and len(tokens[0]) <= 3)


def payee_aliases(transactions: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    """Aliases the user's own payee catalogue in Actual already implies.

    Actual keeps two names on an imported transaction: the bank's descriptor
    (`imported_payee`) and the payee the user settled on. Where the two
    normalize to different keys, the user has curated an alias without
    calling it one: `SQ *BLUE BOTTLE 4471` is `Blue Bottle Coffee`. Reading
    those pairs back is what keeps a rule firing after a payee is renamed.

    Only pairs that have earned it are kept. A descriptor settled on two
    different payees says nothing. A pair seen once may be a one-off rename;
    it has to recur. A descriptor made only of payment-rail words (`ACH
    PAYPAL INST`, `DEBIT CARD WITHDRAWAL`) names the rail, not the shop, and
    would make every future charge on that rail read as whatever the user
    once filed one as. And a key that appears as both a descriptor and a
    payee is left alone so the alias table never chains.
    """

    seen: dict[str, dict[str, dict[str, Any]]] = {}
    for row in transactions:
        imported = str(row.get("imported_description") or "")
        payee = str(row.get("payee_name") or "")
        if not imported or not payee:
            continue
        source_key = normalize_merchant(imported)
        target_key = normalize_merchant(payee)
        if not source_key or not target_key or source_key == target_key:
            continue
        if _names_a_rail(source_key) or _too_generic(target_key):
            continue
        targets = seen.setdefault(source_key, {})
        entry = targets.setdefault(
            target_key,
            {
                "merchant_key": target_key,
                "alias_label": merchant_label(imported),
                "merchant_label": merchant_label(payee),
                "sightings": 0,
            },
        )
        entry["sightings"] += 1
    pairs = {
        source_key: {key: value for key, value in entry.items() if key != "sightings"}
        for source_key, targets in seen.items()
        if len(targets) == 1
        for entry in targets.values()
        if entry["sightings"] >= ALIAS_MIN_SIGHTINGS
    }
    target_keys = {entry["merchant_key"] for entry in pairs.values()}
    return {
        source_key: entry
        for source_key, entry in pairs.items()
        if source_key not in target_keys and entry["merchant_key"] not in pairs
    }


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


# ------------------------------------------------------------- observations
#
# What Actual shows Clerk about its own work. Every decision Clerk applied
# is compared with the transaction's current category: the same means it
# stands, a different one set by hand is a correction, and a correction to a
# rule's decision is a dispute against that rule.

OBSERVED_STANDING = "standing"
OBSERVED_CORRECTED = "corrected"
OBSERVED_CLEARED = "cleared"
OBSERVED_GONE = "gone"


@dataclass(frozen=True)
class Observation:
    decision_id: str
    status: str
    category_id: str = ""
    category_name: str = ""
    reason: str = ""


def observe_decisions(
    decisions: Iterable[Mapping[str, Any]],
    transactions: Iterable[Mapping[str, Any]],
) -> list[Observation]:
    """Compare applied decisions with the transactions as Actual holds them now.

    A decision whose transaction is not in the snapshot is reported as gone
    only when the snapshot should have held it; the caller limits decisions
    to the snapshot's own window. A transaction turned into a transfer is
    gone as well: there is nothing to learn from a category that no longer
    applies.
    """

    current = {str(row.get("id")): row for row in transactions}
    observations: list[Observation] = []
    for decision in decisions:
        decision_id = str(decision.get("id") or "")
        applied = str(decision.get("category_id") or "")
        if not decision_id or not applied:
            continue
        row = current.get(str(decision.get("transaction_id") or ""))
        if row is None:
            observations.append(Observation(decision_id, OBSERVED_GONE, reason="deleted"))
            continue
        if row.get("is_transfer"):
            observations.append(Observation(decision_id, OBSERVED_GONE, reason="transfer"))
            continue
        now = str(row.get("category_id") or "")
        if now == applied:
            observations.append(Observation(decision_id, OBSERVED_STANDING))
        elif not now:
            observations.append(Observation(decision_id, OBSERVED_CLEARED, reason="uncategorized"))
        else:
            observations.append(
                Observation(
                    decision_id,
                    OBSERVED_CORRECTED,
                    category_id=now,
                    category_name=str(row.get("category_name") or ""),
                    reason="recategorized",
                )
            )
    return observations


def rules_needing_repair(
    rules: Iterable[Mapping[str, Any]], categories: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Active rules whose category is no longer in the budget."""
    known = {str(category.get("id")) for category in categories}
    return [
        dict(rule)
        for rule in rules
        if str(rule.get("status") or "active") == "active"
        and str(rule.get("category_id") or "") not in known
    ]


def history_rule_candidates(
    memory: MerchantMemory,
    *,
    rules: RuleBook | None = None,
    min_sightings: float = 3,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Merchants the user has filed one way, every time, often enough to be a rule.

    For a first run against an existing budget: the evidence alone earns
    the question, and the answer is still the user's. Merchants that already
    have a rule are skipped; the most-seen come first.
    """

    candidates: list[dict[str, Any]] = []
    for key in memory.keys:
        if rules is not None and rules.has_merchant(key):
            continue
        evidence = memory.evidence(key)
        if not evidence:
            continue
        top = evidence[0]
        sightings = float(top.get("sightings") or 0)
        if float(top.get("share") or 0) < 1.0 or sightings < min_sightings:
            continue
        candidates.append(
            {
                "merchant_key": key,
                "category_id": str(top.get("category_id") or ""),
                "category_name": str(top.get("category_name") or ""),
                "observations": int(sightings),
            }
        )
    candidates.sort(key=lambda item: (-item["observations"], item["merchant_key"]))
    return candidates[:limit]


def alias_candidates(
    merchant_key: str,
    known_keys: Iterable[str],
    *,
    limit: int = 12,
) -> list[str]:
    """Known merchants worth asking about: the closest by name, then the rest by rule."""
    if not merchant_key:
        return []
    scored = []
    for key in set(known_keys):
        if not key or key == merchant_key:
            continue
        score = 1.0 if keys_related(key, merchant_key) else token_similarity(key, merchant_key)
        scored.append((score, key))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [key for _, key in scored[:limit]]
