from __future__ import annotations

import datetime

import pytest

from actual_clerk.categorize import (
    MODEL_FAILURE_LIMIT,
    Categorizer,
    Proposal,
    candidate_categories,
    group_by_merchant,
    rule_proposal_candidates,
    select_targets,
)
from actual_clerk.clients.openai_compatible import ModelError
from actual_clerk.domain.intelligence import RuleBook

from .factories import FakeModel, account, category, snapshot, transaction

TODAY = datetime.date(2026, 8, 21)

# Eight merchants that each normalize to a distinct key, so one model call is
# made per merchant rather than one for the whole batch.
DISTINCT_MERCHANTS = [
    "Verve Coffee",
    "Petco",
    "Shell Oil",
    "Home Depot",
    "Walgreens",
    "Delta Air Lines",
    "Lyft",
    "Sweetgreen",
]


def coffee_history(count=3, category_id="cat-coffee"):
    return [
        transaction(
            TODAY - datetime.timedelta(days=30 * index + 5),
            -650,
            payee="Blue Bottle Coffee",
            description="SQ *BLUE BOTTLE COFFEE 4471",
            category_id=category_id,
            category_name="Coffee",
        )
        for index in range(count)
    ]


def pending_coffee(count=1):
    return [
        transaction(
            TODAY - datetime.timedelta(days=index),
            -700,
            payee="Blue Bottle Coffee",
            description="BLUE BOTTLE COFFEE #4471 OAKLAND CA",
        )
        for index in range(count)
    ]


async def run(settings, snap, *, model=None, **kwargs):
    categorizer = Categorizer(settings, model_client=model)
    try:
        return await categorizer.run(snap, today=TODAY, **kwargs)
    finally:
        await categorizer.close()


# ------------------------------------------------------------------ selection


def test_only_uncategorized_recent_on_budget_spending_is_considered(settings):
    snap = snapshot(
        transactions=[
            transaction(TODAY, -100, payee="New Shop"),
            transaction(TODAY, -100, payee="Filed", category_id="cat-dining"),
            transaction(TODAY - datetime.timedelta(days=400), -100, payee="Ancient"),
            transaction(TODAY, -100, payee="Moved", is_transfer=True),
            transaction(TODAY, -100, payee="Brokerage", off_budget=True),
            transaction(TODAY, -100, payee="Opening", is_starting_balance=True),
            transaction(TODAY, -100, payee="Closed", closed_account=True),
        ]
    )
    targets = select_targets(snap, today=TODAY, lookback_days=45)
    assert [item["payee_name"] for item in targets] == ["New Shop"]


def test_an_open_review_is_not_proposed_again(settings):
    snap = snapshot(transactions=[transaction(TODAY, -100, payee="New Shop", transaction_id="t-1")])
    assert select_targets(snap, today=TODAY, lookback_days=45, exclude_transaction_ids={"t-1"}) == []


def test_a_review_retry_targets_only_the_requested_items_regardless_of_age(settings):
    old = transaction(
        TODAY - datetime.timedelta(days=400),
        -100,
        payee="Old Review",
        transaction_id="t-old",
    )
    recent = transaction(TODAY, -100, payee="New Item", transaction_id="t-new")
    snap = snapshot(transactions=[old, recent])

    selected = select_targets(
        snap,
        today=TODAY,
        lookback_days=45,
        only_transaction_ids={"t-old"},
    )
    assert [item["id"] for item in selected] == ["t-old"]


def test_candidates_are_ordered_by_how_much_the_budget_uses_them(settings):
    snap = snapshot(
        transactions=[
            transaction(TODAY, -100, payee="A", category_id="cat-dining"),
            transaction(TODAY, -100, payee="B", category_id="cat-dining"),
            transaction(TODAY, -100, payee="C", category_id="cat-rent"),
        ]
    )
    candidates = candidate_categories(snap, limit=3)
    assert [item["id"] for item in candidates][:2] == ["cat-dining", "cat-rent"]


def test_hidden_categories_are_never_offered(settings):
    snap = snapshot(categories=[category("Old", "Bills", hidden=True), category("Dining")])
    assert [item["name"] for item in candidate_categories(snap, limit=10)] == ["Dining"]


def test_blank_categories_are_never_offered(settings):
    snap = snapshot(categories=[category("   ", "Everyday"), category("Dining")])
    assert [item["name"] for item in candidate_categories(snap, limit=10)] == ["Dining"]


def test_transactions_without_a_usable_key_are_grouped_individually():
    targets = [
        {"id": "a", "merchant_key": ""},
        {"id": "b", "merchant_key": ""},
        {"id": "c", "merchant_key": "netflix"},
    ]
    grouped = group_by_merchant(targets)
    assert len(grouped) == 3
    assert grouped["netflix"] == [targets[2]]


# --------------------------------------------------------------- the cascade


async def test_a_known_merchant_is_filed_from_memory_without_the_model(settings):
    model = FakeModel()
    snap = snapshot(transactions=coffee_history() + pending_coffee(2))
    result = await run(settings, snap, model=model)

    assert model.calls == []
    assert result.model_calls == 0
    assert len(result.applied) == 2
    assert {item.source for item in result.applied} == {"memory"}
    assert {item.category_id for item in result.applied} == {"cat-coffee"}


async def test_one_exact_prior_filing_is_proposed_without_asking_the_model(settings):
    model = FakeModel()
    prior = transaction(
        TODAY - datetime.timedelta(days=21),
        -1024,
        payee="86st",
        category_id="cat-dining",
        category_name="Dining",
    )
    pending = transaction(TODAY, -1024, payee="86st")

    result = await run(settings, snapshot(transactions=[prior, pending]), model=model)

    assert model.calls == []
    assert result.applied == []
    assert len(result.review) == 1
    proposal = result.review[0]
    assert proposal.source == "memory"
    assert proposal.category_id == "cat-dining"
    assert proposal.rationale["provisional"] is True
    assert proposal.rationale["approval_required"] is True


async def test_an_unknown_merchant_is_proposed_once_per_merchant_and_never_auto_applied(
    settings,
):
    model = FakeModel([{"category_number": 1, "confidence": 0.9, "reason": "Coffee shop", "suggested_new_category": ""}])
    pending = [
        transaction(TODAY, -650, payee="Verve Coffee", description="SQ *VERVE COFFEE 12"),
        transaction(TODAY, -700, payee="Verve Coffee", description="SQ *VERVE COFFEE 12"),
        transaction(TODAY, -720, payee="Verve Coffee", description="SQ *VERVE COFFEE 12"),
    ]
    result = await run(settings, snapshot(transactions=pending), model=model)

    assert len(model.calls) == 1
    assert result.merchants_seen == 1
    assert result.applied == []
    assert len(result.review) == 3
    assert {item.source for item in result.review} == {"model"}
    assert {item.category_id for item in result.review}
    assert {item.rationale["approval_required"] for item in result.review} == {True}
    assert result.updates == []


async def test_the_model_sees_the_budgets_own_filing_habits(settings):
    model = FakeModel([{"category_number": 1, "confidence": 0.9, "reason": "ok", "suggested_new_category": ""}])
    snap = snapshot(transactions=coffee_history() + [transaction(TODAY, -650, payee="Verve Coffee")])
    await run(settings, snap, model=model)

    prompt = model.calls[0]["user"]
    assert "HOW THIS BUDGET FILES SIMILAR SPENDING" in prompt
    assert "Blue Bottle Coffee" in prompt
    assert "CATEGORIES IN THIS BUDGET" in prompt


async def test_a_low_confidence_answer_is_queued_rather_than_applied(settings):
    settings.ai_min_confidence = 0.7
    model = FakeModel([{"category_number": 1, "confidence": 0.4, "reason": "Unsure", "suggested_new_category": ""}])
    result = await run(settings, snapshot(transactions=pending_coffee()), model=model)

    assert result.applied == []
    assert len(result.review) == 1
    # The proposal is kept so the review screen can offer it in one click.
    assert result.review[0].category_id == candidate_categories(
        snapshot(transactions=pending_coffee()), limit=90
    )[0]["id"]
    assert result.review[0].rationale["threshold"] == 0.7


async def test_an_abstaining_model_produces_a_category_suggestion_not_a_guess(settings):
    settings.allow_new_categories = True
    model = FakeModel(
        [{"category_number": 0, "confidence": 0.0, "reason": "Nothing fits", "suggested_new_category": "Pet Care"}]
    )
    result = await run(settings, snapshot(transactions=pending_coffee()), model=model)

    assert result.applied == []
    assert result.review[0].category_id is None
    assert result.review[0].proposed_category == "Pet Care"
    assert result.suggested_categories[0]["name"] == "Pet Care"


async def test_a_nonsense_category_number_is_treated_as_an_abstention(settings):
    model = FakeModel([{"category_number": 999, "confidence": 1.0, "reason": "?", "suggested_new_category": ""}])
    result = await run(settings, snapshot(transactions=pending_coffee()), model=model)
    assert result.review[0].category_id is None


async def test_review_mode_withholds_even_a_confident_answer(settings):
    settings.apply_mode = "review"
    snap = snapshot(transactions=coffee_history() + pending_coffee())
    result = await run(settings, snap)

    assert result.applied == []
    assert len(result.review) == 1
    assert result.review[0].category_id == "cat-coffee"
    assert result.updates == []


async def test_with_the_model_disabled_unknown_merchants_wait_for_a_human(settings):
    settings.ai_enabled = False
    result = await run(settings, snapshot(transactions=pending_coffee()))

    assert result.model_calls == 0
    assert len(result.review) == 1
    assert result.review[0].source == "unresolved"


async def test_a_model_failure_degrades_to_review_rather_than_failing_the_run(settings):
    model = FakeModel(error=ModelError("connection refused", retryable=True))
    result = await run(settings, snapshot(transactions=pending_coffee(2)), model=model)

    assert len(result.review) == 2
    assert result.review[0].source == "unresolved"
    assert result.model_calls == 1
    assert result.errors and "connection refused" in result.errors[0]


async def test_repeated_invalid_model_answers_abandon_the_model(settings):
    model = FakeModel(
        [
            {
                "category_number": 1,
                "confidence": 0.9,
                "reason": "ok",
                "suggested_new_category": "",
                "unexpected": True,
            }
        ]
        * MODEL_FAILURE_LIMIT
    )
    pending = [
        transaction(TODAY, -100 * index, payee=name)
        for index, name in enumerate(DISTINCT_MERCHANTS, start=1)
    ]
    result = await run(settings, snapshot(transactions=pending), model=model)

    assert result.model_calls == MODEL_FAILURE_LIMIT
    assert result.model_abandoned is True
    assert len(result.review) == len(DISTINCT_MERCHANTS)


async def test_a_percentage_confidence_is_read_as_a_ratio(settings):
    """Small models answer 85 as often as 0.85."""
    model = FakeModel([{"category_number": 1, "confidence": 85, "reason": "ok", "suggested_new_category": ""}])
    result = await run(settings, snapshot(transactions=pending_coffee()), model=model)
    assert result.review and result.review[0].confidence == pytest.approx(0.85)


async def test_an_approved_merchant_can_use_memory_automatically_next_time(settings):
    prior = transaction(
        TODAY - datetime.timedelta(days=1),
        -650,
        payee="Verve Coffee",
        category_id="cat-coffee",
        category_name="Coffee",
    )
    pending = transaction(TODAY, -700, payee="Verve Coffee")
    stored = [
        {
            "merchant_key": "verve coffee",
            "category_id": "cat-coffee",
            "category_name": "Coffee",
            "hits": 1,
            "corrections": 0,
            "last_seen_date": TODAY - datetime.timedelta(days=1),
        }
    ]

    result = await run(
        settings,
        snapshot(transactions=[prior, pending]),
        model=FakeModel(),
        stored_memory=stored,
    )

    assert result.applied and result.applied[0].source == "memory"
    assert result.applied[0].rationale["approval_required"] is False


async def test_clerks_own_stored_memory_counts_as_evidence(settings):
    stored = [
        {
            "merchant_key": "blue bottle coffee",
            "category_id": "cat-coffee",
            "category_name": "Coffee",
            "hits": 4,
            "corrections": 0,
            "last_seen_date": TODAY,
        }
    ]
    model = FakeModel()
    result = await run(
        settings, snapshot(transactions=pending_coffee()), model=model, stored_memory=stored
    )
    assert model.calls == []
    assert result.applied[0].category_id == "cat-coffee"


async def test_nothing_to_do_costs_nothing(settings):
    result = await run(settings, snapshot(transactions=[]))
    assert result.proposals == []
    assert result.summary()["considered"] == 0


# ------------------------------------------------------------------- tagging


async def test_clerks_own_work_is_marked_in_the_note(settings):
    charges = [
        transaction(
            datetime.date(2026, 3, 4) + datetime.timedelta(days=30 * index),
            -1599,
            payee="Netflix",
            category_id="cat-subscriptions",
            category_name="Subscriptions",
        )
        for index in range(4)
    ]
    pending = transaction(TODAY, -1599, payee="Netflix")
    result = await run(settings, snapshot(transactions=charges + [pending]))

    applied = result.applied[0]
    assert settings.clerk_tag in applied.note_tags
    assert result.updates[0]["add_tags"] == applied.note_tags


async def test_an_outsized_charge_is_flagged(settings):
    history = coffee_history(5)
    pending = transaction(TODAY, -4200, payee="Blue Bottle Coffee")
    result = await run(settings, snapshot(transactions=history + [pending]))
    assert "unusual" in result.applied[0].tags


async def test_a_refund_is_tagged_as_money_coming_back(settings):
    history = coffee_history()
    pending = transaction(TODAY, 650, payee="Blue Bottle Coffee")
    result = await run(settings, snapshot(transactions=history + [pending]))
    assert "refund" in result.applied[0].tags


async def test_tagging_can_be_turned_off_entirely(settings):
    settings.tagging_enabled = False
    snap = snapshot(transactions=coffee_history() + pending_coffee())
    result = await run(settings, snap)
    assert result.applied[0].tags == []
    assert result.applied[0].note_tags == []


async def test_a_withheld_proposal_never_writes_a_note(settings):
    settings.apply_mode = "review"
    snap = snapshot(transactions=coffee_history() + pending_coffee())
    result = await run(settings, snap)
    assert result.review[0].note_tags == []


# ---------------------------------------------------------- rule promotion


def proposal(merchant_key, label, category_id="cat-coffee", status="applied"):
    return Proposal(
        transaction_id=f"t-{merchant_key}-{label}",
        merchant_key=merchant_key,
        merchant_label=label,
        payee_name=label,
        account_id="a",
        account_name="Checking",
        transaction_date="2026-08-21",
        amount_cents=-650,
        source="model",
        status=status,
        confidence=0.9,
        category_id=category_id,
        category_name="Coffee",
    )


def test_a_merchant_filed_enough_times_earns_a_rule_proposal():
    proposals = [proposal("blue bottle coffee", "BLUE BOTTLE COFFEE 4471") for _ in range(3)]
    [candidate] = rule_proposal_candidates(proposals, {}, promote_after=3)
    assert candidate["category_id"] == "cat-coffee"
    assert candidate["merchant_key"] == "blue bottle coffee"
    assert candidate["descriptors"] == ["BLUE BOTTLE COFFEE 4471"]
    assert candidate["observations"] == 3


def test_prior_stored_hits_count_towards_a_proposal():
    proposals = [proposal("blue bottle coffee", "BLUE BOTTLE COFFEE 4471")]
    stored = {
        "blue bottle coffee": [
            {"category_id": "cat-coffee", "hits": 4},
            {"category_id": "cat-dining", "hits": 1},
        ]
    }
    [candidate] = rule_proposal_candidates(proposals, stored, promote_after=3)
    assert candidate["observations"] == 5


def test_too_little_evidence_earns_no_proposal():
    proposals = [proposal("blue bottle coffee", "BLUE BOTTLE COFFEE 4471") for _ in range(2)]
    assert rule_proposal_candidates(proposals, {}, promote_after=3) == []


def test_a_merchant_filed_two_different_ways_earns_no_proposal():
    proposals = [
        proposal("target", "TARGET 0455", category_id="cat-groceries"),
        proposal("target", "TARGET 0455", category_id="cat-dining"),
        proposal("target", "TARGET 0455", category_id="cat-groceries"),
    ]
    assert rule_proposal_candidates(proposals, {}, promote_after=2) == []


def test_a_normalized_only_key_can_still_become_a_clerk_rule():
    """Clerk rules match on the key, so an alias-produced key is as good as any."""
    proposals = [proposal("7eleven", "7-ELEVEN 33445") for _ in range(3)]
    [candidate] = rule_proposal_candidates(proposals, {}, promote_after=3)
    assert candidate["merchant_key"] == "7eleven"


def test_a_merchant_with_a_rule_is_not_asked_about_again():
    proposals = [proposal("blue bottle coffee", "BLUE BOTTLE COFFEE") for _ in range(3)]
    rules = RuleBook.from_rows([{"id": "r1", "merchant_key": "blue bottle coffee", "category_id": "cat-coffee"}])
    assert rule_proposal_candidates(proposals, {}, promote_after=3, rules=rules) == []


def test_transactions_filed_by_a_rule_do_not_count_towards_a_proposal():
    proposals = [proposal("blue bottle coffee", "BLUE BOTTLE COFFEE") for _ in range(3)]
    for item in proposals:
        item.source = "rule"
    assert rule_proposal_candidates(proposals, {}, promote_after=3) == []


def test_only_applied_proposals_count_towards_a_proposal():
    proposals = [
        proposal("blue bottle coffee", "BLUE BOTTLE COFFEE", status="needs_review")
        for _ in range(4)
    ]
    assert rule_proposal_candidates(proposals, {}, promote_after=3) == []


# --------------------------------------------------------------------- rules


async def test_a_rule_files_a_merchant_without_evidence_or_a_model(settings):
    rules = RuleBook.from_rows([{"id": "r1", "merchant_key": "blue bottle coffee", "category_id": "cat-coffee", "category_name": "Coffee"}])
    result = await run(settings, snapshot(transactions=pending_coffee()), rules=rules)
    [item] = result.proposals
    assert item.status == "applied"
    assert item.source == "rule"
    assert item.confidence == 1.0
    assert item.rule_id == "r1"
    assert item.rationale["rule"]["id"] == "r1"
    assert result.model_calls == 0


async def test_a_rule_applies_even_in_review_mode(settings):
    settings = settings.model_copy(update={"apply_mode": "review"})
    rules = RuleBook.from_rows([{"id": "r1", "merchant_key": "blue bottle coffee", "category_id": "cat-coffee"}])
    result = await run(settings, snapshot(transactions=pending_coffee()), rules=rules)
    assert result.proposals[0].status == "applied"


async def test_a_rule_beats_contradicting_history(settings):
    rules = RuleBook.from_rows([{"id": "r1", "merchant_key": "blue bottle coffee", "category_id": "cat-dining", "category_name": "Dining"}])
    snap = snapshot(transactions=coffee_history(4) + pending_coffee())
    result = await run(settings, snap, rules=rules)
    assert result.proposals[0].category_id == "cat-dining"
    assert result.proposals[0].source == "rule"


async def test_a_rule_for_a_category_actual_no_longer_has_does_not_file(settings):
    settings = settings.model_copy(update={"ai_enabled": False})
    rules = RuleBook.from_rows([{"id": "r1", "merchant_key": "blue bottle coffee", "category_id": "cat-gone"}])
    result = await run(settings, snapshot(transactions=pending_coffee()), rules=rules)
    assert result.proposals[0].source == "unresolved"
    assert result.proposals[0].status == "needs_review"


async def test_an_alias_lets_a_rule_for_the_banks_name_file_the_other_name(settings):
    rules = RuleBook.from_rows([{"id": "r1", "merchant_key": "steam", "category_id": "cat-coffee"}])
    pending = [transaction(TODAY, -1999, payee="Valve")]
    result = await run(settings, snapshot(transactions=pending), rules=rules, aliases={"valve": "steam"})
    assert result.proposals[0].source == "rule"
    assert result.proposals[0].rationale["alias"] == "steam"


# ------------------------------------------------------------------ summary


async def test_the_run_summary_reports_what_happened(settings):
    model = FakeModel([{"category_number": 1, "confidence": 0.95, "reason": "ok", "suggested_new_category": ""}])
    snap = snapshot(
        transactions=coffee_history() + pending_coffee(2) + [transaction(TODAY, -900, payee="Verve")]
    )
    result = await run(settings, snap, model=model)
    summary = result.summary()

    assert summary["considered"] == 3
    assert summary["applied"] == 2
    assert summary["needs_review"] == 1
    assert summary["model_calls"] == 1
    assert summary["by_source"] == {"memory": 2}
    assert summary["merchants"] == 2


async def test_a_closed_account_is_left_alone(settings):
    snap = snapshot(
        accounts=[account("Old Card", closed=True)],
        transactions=[transaction(TODAY, -100, payee="Shop", closed_account=True)],
    )
    result = await run(settings, snap)
    assert result.proposals == []


async def test_a_missing_category_is_only_raised_when_asked_for(settings):
    answer = {
        "category_number": 0,
        "confidence": 0.0,
        "reason": "Nothing fits",
        "suggested_new_category": "Pet Care",
    }
    quiet = await run(settings, snapshot(transactions=pending_coffee()), model=FakeModel([answer]))
    assert quiet.suggested_categories == []
    assert quiet.review[0].proposed_category == ""

    settings.allow_new_categories = True
    loud = await run(settings, snapshot(transactions=pending_coffee()), model=FakeModel([answer]))
    assert loud.suggested_categories[0]["name"] == "Pet Care"


async def test_a_dead_model_server_costs_one_run_a_few_requests_not_dozens(settings):
    """Every unfamiliar merchant would otherwise retry against the same outage."""
    model = FakeModel(error=ModelError("connection refused", retryable=True))
    pending = [
        transaction(TODAY, -100 * index, payee=name)
        for index, name in enumerate(DISTINCT_MERCHANTS, start=1)
    ]
    result = await run(settings, snapshot(transactions=pending), model=model)

    assert len(model.calls) == 3
    assert result.model_abandoned is True
    assert len(result.review) == 8
    assert {item.source for item in result.review} == {"unresolved"}


async def test_an_intermittent_failure_does_not_abandon_the_model(settings):
    class Flaky(FakeModel):
        async def structured(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) % 2 == 1:
                raise ModelError("hiccup", retryable=True)
            return {
                "category_number": 1,
                "confidence": 0.9,
                "reason": "ok",
                "suggested_new_category": "",
            }

    model = Flaky()
    pending = [
        transaction(TODAY, -100 * index, payee=name)
        for index, name in enumerate(DISTINCT_MERCHANTS[:6], start=1)
    ]
    result = await run(settings, snapshot(transactions=pending), model=model)

    assert result.model_abandoned is False
    assert len(model.calls) == 6
    assert len(result.review) == 6
