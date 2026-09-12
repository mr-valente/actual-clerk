from __future__ import annotations

import datetime

from actual_clerk.domain.intelligence import Rule, RuleBook, canonical_key, resolve
from actual_clerk.domain.memory import MerchantMemory

TODAY = datetime.date(2026, 8, 21)


def history(*rows):
    return [
        {"merchant_key": key, "category_id": category, "category_name": name, "date": TODAY}
        for key, category, name in rows
    ]


def rule(**kwargs):
    base = {"id": "r1", "merchant_key": "starbucks", "category_id": "cat-coffee", "category_name": "Coffee"}
    base.update(kwargs)
    return base


# ------------------------------------------------------------------ rule book


def test_an_exact_rule_claims_only_its_own_key():
    book = RuleBook.from_rows([rule()])
    assert book.lookup("starbucks").id == "r1"
    assert book.lookup("starbucks seattle") is None
    assert book.has_merchant("starbucks")
    assert not book.has_merchant("starbucks seattle")


def test_a_family_rule_reaches_the_merchants_other_store_keys():
    book = RuleBook.from_rows([rule(match="family")])
    assert book.lookup("starbucks seattle").id == "r1"
    # `bp` and `bp fuel station` are related only when the shared part is real.
    assert RuleBook.from_rows([rule(id="bp", merchant_key="bp", match="family")]).lookup("bp fuel") is None


def test_the_longest_family_key_wins():
    book = RuleBook.from_rows([
        rule(id="broad", merchant_key="starbucks", match="family"),
        rule(id="narrow", merchant_key="starbucks reserve", category_id="cat-treats", match="family"),
    ])
    assert book.lookup("starbucks reserve roastery").id == "narrow"
    assert book.lookup("starbucks airport").id == "broad"


def test_an_account_scoped_rule_beats_the_general_one_only_on_its_account():
    book = RuleBook.from_rows([
        rule(id="general"),
        rule(id="scoped", account_id="acct-1", category_id="cat-work"),
    ])
    assert book.lookup("starbucks", "acct-1").id == "scoped"
    assert book.lookup("starbucks", "acct-2").id == "general"
    assert book.lookup("starbucks").id == "general"


def test_an_exact_rule_beats_a_family_rule_for_the_same_key():
    book = RuleBook.from_rows([
        rule(id="family", merchant_key="starbucks", match="family"),
        rule(id="exact", merchant_key="starbucks seattle", category_id="cat-travel"),
    ])
    assert book.lookup("starbucks seattle").id == "exact"


def test_rules_without_a_key_or_category_are_ignored():
    book = RuleBook.from_rows([rule(merchant_key=""), rule(id="r2", category_id="")])
    assert len(book) == 0
    assert not book


def test_a_rule_row_is_read_defensively():
    parsed = Rule.from_row({"id": "x", "merchant_key": "k", "category_id": "c"})
    assert parsed.match == "exact"
    assert parsed.account_id == ""
    assert parsed.claims("k") and not parsed.claims("")


# ------------------------------------------------------------------- resolver


def test_a_rule_answers_before_evidence_with_full_confidence():
    memory = MerchantMemory().add_transactions(history(*[("starbucks", "cat-dining", "Dining")] * 5), TODAY)
    answer = resolve("starbucks", rules=RuleBook.from_rows([rule()]), memory=memory)
    assert answer.source == "rule"
    assert answer.category_id == "cat-coffee"
    assert answer.confidence == 1.0
    assert answer.automatic is True
    assert answer.rationale["rule"]["id"] == "r1"
    assert answer.rationale["matched_key"] == "starbucks"


def test_evidence_answers_when_there_is_no_rule():
    memory = MerchantMemory().add_transactions(history(*[("starbucks", "cat-dining", "Dining")] * 3), TODAY)
    answer = resolve("starbucks", rules=RuleBook(), memory=memory)
    assert answer.source == "memory"
    assert answer.category_id == "cat-dining"
    assert answer.automatic is True
    assert answer.rationale["memory"]["exact"] is True


def test_the_apply_mode_holds_memory_back_but_not_a_rule():
    memory = MerchantMemory().add_transactions(history(*[("starbucks", "cat-dining", "Dining")] * 3), TODAY)
    held = resolve("starbucks", memory=memory, automatic_memory=False)
    assert held.source == "memory" and held.automatic is False
    ruled = resolve("starbucks", rules=RuleBook.from_rows([rule()]), memory=memory, automatic_memory=False)
    assert ruled.automatic is True


def test_one_exact_sighting_is_a_suggestion_not_an_answer():
    memory = MerchantMemory().add_transactions(history(("starbucks", "cat-dining", "Dining")), TODAY)
    answer = resolve("starbucks", memory=memory)
    assert answer.source == "memory"
    assert answer.automatic is False
    assert answer.rationale["provisional"] is True


def test_nothing_known_returns_none_so_the_caller_may_escalate():
    assert resolve("starbucks", rules=RuleBook(), memory=MerchantMemory()) is None
    assert resolve("", rules=RuleBook.from_rows([rule(merchant_key="")]), memory=MerchantMemory()) is None


def test_an_alias_is_followed_before_rules_and_evidence():
    aliases = {"valve": "steam"}
    assert canonical_key("valve", aliases) == "steam"
    assert canonical_key("steam", aliases) == "steam"
    book = RuleBook.from_rows([rule(merchant_key="steam", category_id="cat-games")])
    answer = resolve("valve", rules=book, memory=MerchantMemory(), aliases=aliases)
    assert answer.source == "rule"
    assert answer.rationale["alias"] == "steam"
    memory = MerchantMemory().add_transactions(history(*[("steam", "cat-games", "Games")] * 3), TODAY)
    learned = resolve("valve", memory=memory, aliases=aliases)
    assert learned.category_id == "cat-games"
    assert learned.rationale["alias"] == "steam"


def test_the_original_key_is_still_tried_when_the_alias_knows_nothing():
    memory = MerchantMemory().add_transactions(history(*[("valve", "cat-games", "Games")] * 3), TODAY)
    answer = resolve("valve", memory=memory, aliases={"valve": "steam"})
    assert answer.category_id == "cat-games"
    assert answer.rationale["alias"] == ""


def test_a_rule_for_a_category_that_no_longer_exists_is_skipped():
    memory = MerchantMemory().add_transactions(history(*[("starbucks", "cat-dining", "Dining")] * 3), TODAY)
    answer = resolve(
        "starbucks",
        rules=RuleBook.from_rows([rule(category_id="cat-gone")]),
        memory=memory,
        allowed_categories={"cat-dining"},
    )
    assert answer.source == "memory"


def test_category_names_come_from_the_budget_when_known():
    answer = resolve("starbucks", rules=RuleBook.from_rows([rule(category_name="Old name")]), names={"cat-coffee": "Coffee & tea"})
    assert answer.category_name == "Coffee & tea"
