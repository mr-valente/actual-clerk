from __future__ import annotations

import datetime

from actual_clerk.domain.actual_rules import classify_rule, classify_rules, replay, summarize_rule

from .factories import transaction

TODAY = datetime.date(2026, 8, 21)

PAYEES = {
    "p-bb": {"id": "p-bb", "name": "Blue Bottle Coffee", "transfer_account_id": ""},
    "p-sb": {"id": "p-sb", "name": "Starbucks", "transfer_account_id": ""},
    "p-xfer": {"id": "p-xfer", "name": "", "transfer_account_id": "acct-card"},
    "p-num": {"id": "p-num", "name": "4471", "transfer_account_id": ""},
}
ACCOUNTS = {"acct-checking": "Checking", "acct-card": "Card"}
CATEGORIES = {"cat-coffee": "Coffee", "cat-income": "Income"}


def rule(conditions, actions=None, *, op="and", stage="default", rule_id="r1"):
    return {
        "id": rule_id,
        "stage": stage,
        "conditions_op": op,
        "conditions": conditions,
        "actions": actions or [{"op": "set", "field": "category", "value": "cat-coffee", "type": "id"}],
    }


def classify(item):
    return classify_rule(item, payees=PAYEES, accounts=ACCOUNTS, categories=CATEGORIES)


def test_a_payee_is_rule_becomes_one_clerk_rule_on_the_normalized_key():
    item = classify(rule([{"op": "is", "field": "payee", "value": "p-bb", "type": "id"}]))
    assert item.disposition == "move"
    [translation] = item.translations
    assert translation.merchant_key == "blue bottle coffee"
    assert translation.merchant_label == "Blue Bottle Coffee"
    assert translation.category_id == "cat-coffee"
    assert translation.category_name == "Coffee"
    assert translation.account_id == ""
    assert translation.problem == ""
    assert item.summary == "If payee is Blue Bottle Coffee, set category to Coffee"


def test_internal_column_names_are_read_like_the_public_ones():
    item = classify(rule([{"op": "is", "field": "description", "value": "p-bb", "type": "id"}]))
    assert item.disposition == "move"
    assert item.translations[0].merchant_key == "blue bottle coffee"


def test_one_of_and_or_name_several_payees():
    one_of = classify(rule([{"op": "oneOf", "field": "payee", "value": ["p-bb", "p-sb"], "type": "id"}]))
    either = classify(rule([
        {"op": "is", "field": "payee", "value": "p-bb", "type": "id"},
        {"op": "is", "field": "payee", "value": "p-sb", "type": "id"},
    ], op="or"))
    for item in (one_of, either):
        assert item.disposition == "move"
        assert [t.merchant_key for t in item.translations] == ["blue bottle coffee", "starbucks"]


def test_an_imported_payee_contains_rule_normalizes_the_text():
    item = classify(rule([{"op": "contains", "field": "imported_payee", "value": "Yardsale Cafe", "type": "string"}]))
    assert item.disposition == "move"
    assert item.translations[0].merchant_key == "yardsale cafe"
    assert item.translations[0].condition == "imported payee contains Yardsale Cafe"


def test_an_account_condition_scopes_each_payee_to_each_account():
    item = classify(rule([
        {"op": "oneOf", "field": "account", "value": ["acct-checking", "acct-card"], "type": "id"},
        {"op": "oneOf", "field": "payee", "value": ["p-bb", "p-sb"], "type": "id"},
    ], actions=[{"op": "set", "field": "category", "value": "cat-income", "type": "id"}]))
    assert item.disposition == "move"
    assert sorted((t.merchant_key, t.account_id) for t in item.translations) == [
        ("blue bottle coffee", "acct-card"),
        ("blue bottle coffee", "acct-checking"),
        ("starbucks", "acct-card"),
        ("starbucks", "acct-checking"),
    ]
    assert item.translations[0].account_name in ("Checking", "Card")


def test_rules_that_do_something_other_than_pick_a_category_are_kept():
    payee_set = classify(rule(
        [{"op": "is", "field": "account", "value": "acct-card", "type": "id"}, {"op": "is", "field": "payee", "value": "p-bb", "type": "id"}],
        actions=[{"op": "set", "field": "payee", "value": "p-xfer", "type": "id"}],
    ))
    assert payee_set.disposition == "kept"
    assert "set the payee" in payee_set.reason
    delete = classify(rule([{"op": "is", "field": "payee", "value": "p-bb", "type": "id"}], actions=[{"op": "delete-transaction", "value": None}]))
    assert delete.disposition == "kept"
    assert "delete the transaction" in delete.reason
    two = classify(rule([{"op": "is", "field": "payee", "value": "p-bb", "type": "id"}], actions=[
        {"op": "set", "field": "category", "value": "cat-coffee"}, {"op": "set", "field": "notes", "value": "x"},
    ]))
    assert two.disposition == "kept" and "more than one action" in two.reason


def test_rules_that_depend_on_more_than_the_payee_are_kept():
    amount = classify(rule([
        {"op": "is", "field": "payee", "value": "p-bb", "type": "id"},
        {"op": "gt", "field": "amount", "value": 100, "type": "number"},
    ]))
    assert amount.disposition == "kept" and "depends on the amount" in amount.reason
    regex = classify(rule([{"op": "matches", "field": "payee", "value": "^Blue", "type": "string"}]))
    assert regex.disposition == "kept" and "matches" in regex.reason
    mixed_or = classify(rule([
        {"op": "is", "field": "account", "value": "acct-card", "type": "id"},
        {"op": "is", "field": "payee", "value": "p-bb", "type": "id"},
    ], op="or"))
    assert mixed_or.disposition == "kept" and "'or'" in mixed_or.reason
    staged = classify(rule([{"op": "is", "field": "payee", "value": "p-bb", "type": "id"}], stage="pre"))
    assert staged.disposition == "kept" and "pre stage" in staged.reason
    no_payee = classify(rule([{"op": "is", "field": "account", "value": "acct-card", "type": "id"}]))
    assert no_payee.disposition == "kept" and "names no payee" in no_payee.reason


def test_translations_with_a_problem_are_flagged_rather_than_dropped():
    item = classify(rule([{"op": "oneOf", "field": "payee", "value": ["p-xfer", "p-num", "p-gone", "p-bb"], "type": "id"}]))
    assert item.disposition == "move"
    problems = {t.merchant_label or t.merchant_key: t.problem for t in item.translations}
    assert "transfer payee" in problems[""]
    assert "normalizes to nothing" in problems["4471"]
    assert "no longer exists" in problems["p-gone"]
    assert problems["Blue Bottle Coffee"] == ""


def test_a_category_actual_no_longer_has_is_a_problem():
    item = classify(rule([{"op": "is", "field": "payee", "value": "p-bb", "type": "id"}], actions=[{"op": "set", "field": "category", "value": "cat-gone"}]))
    assert item.disposition == "move"
    assert "no longer has" in item.translations[0].problem


def test_replay_counts_agreement_and_collisions_over_history():
    history = [
        transaction(TODAY, -500, payee="Blue Bottle Coffee", category_id="cat-coffee", category_name="Coffee"),
        transaction(TODAY, -500, payee="Blue Bottle Coffee", category_id="cat-coffee", category_name="Coffee"),
        transaction(TODAY, -500, payee="SQ *BLUE BOTTLE COFFEE 4471", category_id="cat-dining", category_name="Dining"),
        transaction(TODAY, -500, payee="Starbucks", category_id="cat-coffee", category_name="Coffee", account_id="acct-card"),
        transaction(TODAY, -500, payee="Starbucks", category_id="cat-coffee", category_name="Coffee", is_transfer=True),
    ]
    classified = classify_rules([
        rule([{"op": "is", "field": "payee", "value": "p-bb", "type": "id"}], rule_id="r1"),
        rule([{"op": "is", "field": "payee", "value": "p-sb", "type": "id"}], rule_id="r2"),
        rule([{"op": "is", "field": "account", "value": "acct-card", "type": "id"}, {"op": "is", "field": "payee", "value": "p-sb", "type": "id"}], actions=[{"op": "set", "field": "category", "value": "cat-income"}], rule_id="r3"),
    ], payees=PAYEES, accounts=ACCOUNTS, categories=CATEGORIES)
    replay(classified, history, categories=CATEGORIES)
    blue = classified[0].translations[0].replay
    assert blue == {"matched": 3, "agree": 2, "disagree": 1, "disagreeing": {"Dining": 1}}
    # The transfer row is not history; the scoped rule sees only its account.
    assert classified[1].translations[0].replay["matched"] == 1
    assert classified[2].translations[0].replay["matched"] == 1
    assert classified[2].translations[0].replay["agree"] == 0
    # The general and the scoped Starbucks rules do not collide: different scopes.
    assert classified[1].translations[0].problem == ""


def test_two_rules_claiming_one_key_with_different_categories_collide():
    classified = classify_rules([
        rule([{"op": "is", "field": "payee", "value": "p-bb", "type": "id"}], rule_id="r1"),
        rule([{"op": "contains", "field": "imported_payee", "value": "Blue Bottle Coffee"}], actions=[{"op": "set", "field": "category", "value": "cat-income"}], rule_id="r2"),
    ], payees=PAYEES, accounts=ACCOUNTS, categories=CATEGORIES)
    replay(classified, [], categories=CATEGORIES)
    assert "collides" in classified[0].translations[0].problem
    assert "Coffee vs Income" in classified[1].translations[0].problem


def test_summaries_read_as_sentences():
    kept = rule(
        [{"op": "is", "field": "account", "value": "acct-card", "type": "id"}, {"op": "is", "field": "payee", "value": "p-bb", "type": "id"}],
        actions=[{"op": "delete-transaction", "value": None}],
    )
    assert summarize_rule(kept, PAYEES, ACCOUNTS, CATEGORIES) == "If account is Card and payee is Blue Bottle Coffee, delete the transaction"
