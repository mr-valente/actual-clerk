from __future__ import annotations

import datetime

from actual_clerk.domain.memory import (
    MerchantMemory,
    decay_weight,
    saturation,
    similar_examples,
    token_similarity,
)

TODAY = datetime.date(2026, 8, 21)


def history(*rows):
    return [
        {"merchant_key": key, "category_id": category, "category_name": name, "date": date}
        for key, category, name, date in rows
    ]


def test_consistent_history_produces_a_confident_match():
    memory = MerchantMemory().add_transactions(
        history(
            ("starbucks", "c-coffee", "Coffee", datetime.date(2026, 8, 1)),
            ("starbucks", "c-coffee", "Coffee", datetime.date(2026, 7, 1)),
        ),
        TODAY,
    )
    match = memory.lookup("starbucks", min_observations=2, min_confidence=0.75)
    assert match is not None
    assert match.category_id == "c-coffee"
    assert match.share == 1.0
    assert match.exact is True


def test_one_sighting_is_not_enough_evidence():
    memory = MerchantMemory().add_transactions(
        history(("shell", "c-gas", "Gas", datetime.date(2026, 8, 10))), TODAY
    )
    assert memory.lookup("shell", min_observations=2, min_confidence=0.75) is None


def test_a_split_history_abstains_rather_than_guessing():
    memory = MerchantMemory().add_transactions(
        history(
            ("target", "c-house", "Household", datetime.date(2026, 8, 3)),
            ("target", "c-food", "Groceries", datetime.date(2026, 8, 4)),
        ),
        TODAY,
    )
    assert memory.lookup("target", min_observations=2, min_confidence=0.75) is None
    evidence = memory.evidence("target")
    assert len(evidence) == 2
    assert sum(entry["share"] for entry in evidence) == 1.0


def test_recent_filing_outweighs_an_old_one():
    memory = MerchantMemory().add_transactions(
        history(
            ("costco", "c-house", "Household", datetime.date(2024, 1, 1)),
            ("costco", "c-house", "Household", datetime.date(2024, 2, 1)),
            ("costco", "c-food", "Groceries", datetime.date(2026, 8, 1)),
            ("costco", "c-food", "Groceries", datetime.date(2026, 7, 1)),
        ),
        TODAY,
    )
    match = memory.lookup("costco", min_observations=2, min_confidence=0.6)
    assert match is not None
    assert match.category_id == "c-food"


def test_evidence_ages_but_sighting_count_does_not():
    """Old evidence should lose influence without losing its status as evidence."""
    memory = MerchantMemory().add_transactions(
        history(
            ("gym", "c-health", "Health", datetime.date(2024, 3, 1)),
            ("gym", "c-health", "Health", datetime.date(2024, 4, 1)),
        ),
        TODAY,
    )
    match = memory.lookup("gym", min_observations=2, min_confidence=0.75)
    assert match is not None
    assert match.observations == 2


def test_a_store_location_pools_onto_the_base_merchant():
    memory = MerchantMemory().add_transactions(
        history(
            ("starbucks", "c-coffee", "Coffee", datetime.date(2026, 8, 1)),
            ("starbucks", "c-coffee", "Coffee", datetime.date(2026, 7, 1)),
        ),
        TODAY,
    )
    pooled = memory.lookup("starbucks portland", min_observations=2, min_confidence=0.7)
    assert pooled is not None
    assert pooled.exact is False
    exact = memory.lookup("starbucks", min_observations=2, min_confidence=0.7)
    # A pooled match rests on a normalization guess as well, so it must not
    # outrank the exact evidence it was derived from.
    assert pooled.confidence < exact.confidence


def test_a_correction_outweighs_ordinary_sightings():
    memory = MerchantMemory().add_transactions(
        history(
            ("chipotle", "c-groceries", "Groceries", datetime.date(2026, 8, 1)),
            ("chipotle", "c-groceries", "Groceries", datetime.date(2026, 8, 2)),
        ),
        TODAY,
    )
    memory.add_stored(
        [
            {
                "merchant_key": "chipotle",
                "category_id": "c-dining",
                "category_name": "Dining",
                "hits": 1,
                "corrections": 1,
                "last_seen_date": datetime.date(2026, 8, 15),
            }
        ],
        TODAY,
    )
    match = memory.lookup("chipotle", min_observations=2, min_confidence=0.6)
    assert match is not None
    assert match.category_id == "c-dining"


def test_a_category_no_longer_in_the_budget_is_ignored():
    memory = MerchantMemory().add_transactions(
        history(
            ("shell", "c-deleted", "Old Gas", datetime.date(2026, 8, 1)),
            ("shell", "c-deleted", "Old Gas", datetime.date(2026, 8, 2)),
            ("shell", "c-gas", "Gas", datetime.date(2026, 7, 1)),
            ("shell", "c-gas", "Gas", datetime.date(2026, 6, 1)),
        ),
        TODAY,
    )
    match = memory.lookup(
        "shell", min_observations=2, min_confidence=0.7, allowed_categories={"c-gas"}
    )
    assert match is not None
    assert match.category_id == "c-gas"
    assert match.share == 1.0


def test_lookups_that_cannot_answer():
    memory = MerchantMemory()
    assert memory.lookup("") is None
    assert memory.lookup("unknown") is None
    assert memory.evidence("unknown") == []
    assert len(memory) == 0


def test_blank_rows_are_ignored():
    memory = MerchantMemory()
    memory.add("", "c-1", "One")
    memory.add("key", "", "One")
    memory.add("key", "c-1", "One", weight=0)
    assert len(memory) == 0


def test_saturation_and_decay_are_monotonic():
    assert saturation(0) == 0
    assert saturation(1) < saturation(3) < saturation(10) < 1
    assert decay_weight(TODAY, TODAY) == 1.0
    assert decay_weight(TODAY - datetime.timedelta(days=270), TODAY) == 0.5
    assert decay_weight(datetime.date(2000, 1, 1), TODAY) == 0.05


def test_token_similarity():
    assert token_similarity("blue bottle", "blue bottle") == 1.0
    assert token_similarity("blue bottle", "blue jar") == 1 / 3
    assert token_similarity("blue", "red") == 0.0
    assert token_similarity("", "red") == 0.0


def test_examples_lead_with_the_closest_merchant_then_broaden():
    rows = history(
        ("blue bottle coffee", "c-coffee", "Coffee", datetime.date(2026, 8, 1)),
        ("blue bottle coffee", "c-coffee", "Coffee", datetime.date(2026, 7, 1)),
        ("peets coffee", "c-coffee", "Coffee", datetime.date(2026, 6, 1)),
        ("shell", "c-gas", "Gas", datetime.date(2026, 5, 1)),
        ("rent office", "c-rent", "Rent", datetime.date(2026, 4, 1)),
    )
    chosen = similar_examples("blue bottle coffee", rows, limit=3)
    assert chosen[0]["merchant_key"] == "blue bottle coffee"
    # One example per merchant key, then unseen categories for breadth.
    assert len({row["merchant_key"] for row in chosen}) == len(chosen)
    assert len(chosen) == 3


def test_examples_respect_a_zero_limit():
    assert similar_examples("x", history(("x", "c", "C", TODAY)), limit=0) == []
