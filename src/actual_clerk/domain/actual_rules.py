"""Reading Actual's rules into Clerk's terms.

Actual's rule table holds two kinds of thing. Most rules say "this payee
belongs in that category", which is exactly what a Clerk rule says, so they
can move. The rest do something else: set a transfer payee, delete a
transaction, rename an imported payee, or depend on the amount or the date.
Those stay in Actual, and nothing here reads them for any purpose other than
listing them as kept.

A simple rule becomes one Clerk rule per payee it names, keyed on the
normalized merchant key rather than the payee id. That is broader than
Actual's `payee is` (one key covers every store number), so before a
translation is trusted it is *replayed* over the retained history: how many
rows its key would claim, and whether Actual filed them the same way.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from actual_clerk.domain.merchants import merchant_label, normalize_merchant

DISPOSITION_MOVE = "move"
DISPOSITION_KEPT = "kept"

# Field names as the public API presents them, plus the internal column
# names older rules were stored under.
_PAYEE_FIELDS = frozenset({"payee", "description"})
_IMPORTED_PAYEE_FIELDS = frozenset({"imported_payee", "imported_description"})
_ACCOUNT_FIELDS = frozenset({"account", "acct"})
_PAYEE_OPS = frozenset({"is", "oneOf", "contains"})
_ACCOUNT_OPS = frozenset({"is", "oneOf"})


@dataclass
class Translation:
    """One Clerk rule a simple Actual rule becomes."""

    merchant_key: str
    merchant_label: str
    category_id: str
    category_name: str = ""
    account_id: str = ""
    account_name: str = ""
    condition: str = ""
    # Why this translation cannot be imported as it stands; empty when it can.
    problem: str = ""
    replay: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "merchant_key": self.merchant_key,
            "merchant_label": self.merchant_label,
            "category_id": self.category_id,
            "category_name": self.category_name,
            "account_id": self.account_id,
            "account_name": self.account_name,
            "condition": self.condition,
            "problem": self.problem,
            "replay": self.replay,
        }


@dataclass
class ClassifiedRule:
    rule: dict[str, Any]
    disposition: str
    reason: str = ""
    summary: str = ""
    translations: list[Translation] = field(default_factory=list)

    @property
    def id(self) -> str:
        return str(self.rule.get("id") or "")

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "stage": str(self.rule.get("stage") or "default"),
            "conditions_op": str(self.rule.get("conditions_op") or "and"),
            "disposition": self.disposition,
            "reason": self.reason,
            "summary": self.summary,
            "translations": [item.as_dict() for item in self.translations],
        }


def _values(condition: Mapping[str, Any]) -> list[Any]:
    value = condition.get("value")
    if isinstance(value, list):
        return list(value)
    return [value] if value not in (None, "") else []


def _describe_action(action: Mapping[str, Any], categories: Mapping[str, str]) -> str:
    op = str(action.get("op") or "")
    if op == "set":
        target = str(action.get("field") or "")
        if target == "category":
            return f"set category to {categories.get(str(action.get('value')), 'a category')}"
        if target in _PAYEE_FIELDS:
            return "set the payee"
        return f"set {target}"
    if op == "delete-transaction":
        return "delete the transaction"
    if op == "link-schedule":
        return "link a schedule"
    if op == "set-split-amount":
        return "split the transaction"
    return op or "do something"


def _describe_condition(
    condition: Mapping[str, Any],
    payees: Mapping[str, Mapping[str, Any]],
    accounts: Mapping[str, str],
) -> str:
    fld = str(condition.get("field") or "")
    op = str(condition.get("op") or "")
    values = _values(condition)
    if fld in _PAYEE_FIELDS:
        names = [str(payees.get(str(value), {}).get("name") or value) for value in values]
        return f"payee {op} {', '.join(names)}"
    if fld in _IMPORTED_PAYEE_FIELDS:
        return f"imported payee {op} {', '.join(str(value) for value in values)}"
    if fld in _ACCOUNT_FIELDS:
        names = [accounts.get(str(value), str(value)) for value in values]
        return f"account {op} {', '.join(names)}"
    return f"{fld} {op} {', '.join(str(value) for value in values)}"


def summarize_rule(
    rule: Mapping[str, Any],
    payees: Mapping[str, Mapping[str, Any]],
    accounts: Mapping[str, str],
    categories: Mapping[str, str],
) -> str:
    joiner = " or " if str(rule.get("conditions_op") or "and") == "or" else " and "
    conditions = joiner.join(
        _describe_condition(condition, payees, accounts)
        for condition in rule.get("conditions") or []
    )
    actions = "; ".join(_describe_action(action, categories) for action in rule.get("actions") or [])
    return f"If {conditions or 'anything'}, {actions or 'do nothing'}"


def classify_rule(
    rule: Mapping[str, Any],
    *,
    payees: Mapping[str, Mapping[str, Any]],
    accounts: Mapping[str, str],
    categories: Mapping[str, str],
) -> ClassifiedRule:
    """Decide whether an Actual rule can move to Clerk, and what it becomes there.

    A rule moves when every condition is on the payee or the imported payee
    (`is`, `oneOf`, `contains`), optionally narrowed by an account condition
    under `and`, and its only action sets a category. Anything else is kept
    in Actual with the reason.
    """

    summary = summarize_rule(rule, payees, accounts, categories)
    raw = dict(rule)

    def kept(reason: str) -> ClassifiedRule:
        return ClassifiedRule(raw, DISPOSITION_KEPT, reason=reason, summary=summary)

    stage = str(rule.get("stage") or "default")
    if stage != "default":
        return kept(f"runs in the {stage} stage, an ordering only Actual has")

    actions = list(rule.get("actions") or [])
    if len(actions) != 1:
        return kept("has more than one action" if actions else "has no action")
    action = actions[0]
    if str(action.get("op")) != "set" or str(action.get("field") or "") != "category":
        return kept(f"its action is to {_describe_action(action, categories)}")
    category_id = str(action.get("value") or "")
    if not category_id:
        return kept("sets no category")

    conditions = list(rule.get("conditions") or [])
    if not conditions:
        return kept("has no condition")
    conditions_op = str(rule.get("conditions_op") or "and")
    payee_conditions: list[Mapping[str, Any]] = []
    account_conditions: list[Mapping[str, Any]] = []
    for condition in conditions:
        fld = str(condition.get("field") or "")
        op = str(condition.get("op") or "")
        if fld in _PAYEE_FIELDS or fld in _IMPORTED_PAYEE_FIELDS:
            if op not in _PAYEE_OPS:
                return kept(f"matches the payee with {op}, which has no equivalent in Clerk")
            payee_conditions.append(condition)
        elif fld in _ACCOUNT_FIELDS:
            if op not in _ACCOUNT_OPS:
                return kept(f"matches the account with {op}")
            account_conditions.append(condition)
        else:
            return kept(f"depends on the {fld or 'transaction'}, not only on the payee")
    if not payee_conditions:
        return kept("names no payee")
    if conditions_op == "or" and account_conditions:
        return kept("combines account and payee conditions with 'or'")
    if len(account_conditions) > 1:
        return kept("has more than one account condition")

    account_ids: list[str] = [""]
    if account_conditions:
        account_ids = [str(value) for value in _values(account_conditions[0]) if value]
        if not account_ids:
            return kept("its account condition names no account")

    classified = ClassifiedRule(raw, DISPOSITION_MOVE, summary=summary)
    category_name = categories.get(category_id, "")
    for condition in payee_conditions:
        fld = str(condition.get("field") or "")
        op = str(condition.get("op") or "")
        for value in _values(condition):
            text = str(value)
            problem = ""
            if fld in _PAYEE_FIELDS and op in ("is", "oneOf"):
                payee = payees.get(text)
                if payee is None:
                    label, key, problem = text, "", "names a payee that no longer exists"
                else:
                    label = str(payee.get("name") or "")
                    key = normalize_merchant(label)
                    if payee.get("transfer_account_id"):
                        problem = "names a transfer payee; transfers stay with Actual"
            else:
                label, key = merchant_label(text), normalize_merchant(text)
            if not key and not problem:
                problem = "the name normalizes to nothing Clerk can match on"
            if not category_name and not problem:
                problem = "sets a category Actual no longer has"
            for account_id in account_ids:
                classified.translations.append(
                    Translation(
                        merchant_key=key,
                        merchant_label=label,
                        category_id=category_id,
                        category_name=category_name,
                        account_id=account_id,
                        account_name=accounts.get(account_id, "") if account_id else "",
                        condition=f"{'imported payee' if fld in _IMPORTED_PAYEE_FIELDS else 'payee'} {op} {label}",
                        problem=problem,
                    )
                )
    if not classified.translations:
        return kept("names no payee")
    return classified


def classify_rules(
    rules: Iterable[Mapping[str, Any]],
    *,
    payees: Mapping[str, Mapping[str, Any]],
    accounts: Mapping[str, str],
    categories: Mapping[str, str],
) -> list[ClassifiedRule]:
    return [
        classify_rule(rule, payees=payees, accounts=accounts, categories=categories)
        for rule in rules
    ]


def replay(
    classified: Sequence[ClassifiedRule],
    transactions: Iterable[Mapping[str, Any]],
    *,
    categories: Mapping[str, str],
) -> None:
    """Show what each translation would have claimed in the retained history.

    Fills each translation's `replay` with how many categorized rows its key
    claims, how many of those Actual filed in the same category, and the
    categories the rest went to. Two translations that claim the same key on
    the same account with different categories are a collision, which is a
    problem on both.
    """

    by_key: dict[tuple[str, str], list[Translation]] = {}
    for item in classified:
        for translation in item.translations:
            if translation.merchant_key:
                by_key.setdefault((translation.merchant_key, translation.account_id), []).append(
                    translation
                )
    for translation in (t for group in by_key.values() for t in group):
        translation.replay = {"matched": 0, "agree": 0, "disagree": 0, "disagreeing": {}}
    history = [
        row
        for row in transactions
        if row.get("category_id")
        and row.get("merchant_key")
        and not row.get("is_transfer")
        and not row.get("is_starting_balance")
    ]
    for row in history:
        key = str(row.get("merchant_key") or "")
        account_id = str(row.get("account_id") or "")
        for scope in (account_id, ""):
            for translation in by_key.get((key, scope), ()):
                stats = translation.replay
                stats["matched"] += 1
                if str(row.get("category_id")) == translation.category_id:
                    stats["agree"] += 1
                else:
                    stats["disagree"] += 1
                    name = categories.get(str(row.get("category_id")), str(row.get("category_name") or "?"))
                    stats["disagreeing"][name] = stats["disagreeing"].get(name, 0) + 1
    for group in by_key.values():
        distinct = {translation.category_id for translation in group}
        if len(distinct) > 1:
            names = sorted({translation.category_name or translation.category_id for translation in group})
            for translation in group:
                if not translation.problem:
                    translation.problem = (
                        "collides with another rule for the same merchant: " + " vs ".join(names)
                    )
