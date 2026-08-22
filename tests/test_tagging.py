from __future__ import annotations

from actual_clerk.domain.tagging import (
    MerchantStats,
    apply_tags,
    derive_tags,
    has_tag,
    normalize_tag,
    parse_tags,
    remove_tag,
    tag_catalog,
)


def test_tags_are_read_the_way_actual_writes_them():
    assert parse_tags("Coffee run #clerk #subscription") == ["clerk", "subscription"]
    assert parse_tags("#clerk") == ["clerk"]
    assert parse_tags("ends with a stop #clerk.") == ["clerk"]
    assert parse_tags("") == []
    assert parse_tags(None) == []
    assert has_tag("a #clerk b", "CLERK")
    assert not has_tag("a #clerks b", "clerk")


def test_applying_tags_twice_changes_nothing():
    once = apply_tags("Coffee run", ["clerk", "subscription"])
    twice = apply_tags(once, ["clerk", "subscription"])
    assert once == "Coffee run #clerk #subscription"
    assert twice == once


def test_existing_text_and_tags_are_preserved():
    assert apply_tags("Split with Alex #clerk", ["subscription"]) == "Split with Alex #clerk #subscription"
    assert apply_tags(None, ["clerk"]) == "#clerk"
    assert apply_tags("note", []) == "note"
    assert apply_tags(None, []) == ""


def test_a_tag_differing_only_in_case_is_not_added_twice():
    assert apply_tags("note #Clerk", ["clerk"]) == "note #Clerk"


def test_unusable_tag_names_are_refused():
    assert normalize_tag("#clerk") == "clerk"
    assert normalize_tag("two words") == ""
    assert apply_tags("note", ["two words", ""]) == "note"


def test_removing_a_tag_leaves_the_note_readable():
    assert remove_tag("Coffee run #clerk #subscription", "clerk") == "Coffee run #subscription"
    assert remove_tag("#clerk", "clerk") == ""
    assert remove_tag("Coffee run", "clerk") == "Coffee run"
    # A tag that merely starts with the same letters is left alone.
    assert remove_tag("note #clerks", "clerk") == "note #clerks"


def test_cadence_tags_follow_the_detected_schedule():
    assert derive_tags(amount_cents=-1599, merchant_key="netflix", recurring_kind="subscription", recurring_interval_days=30) == ["subscription"]
    assert derive_tags(amount_cents=-9800, merchant_key="pge", recurring_kind="recurring", recurring_interval_days=31) == ["recurring"]
    assert derive_tags(amount_cents=-9900, merchant_key="domain", recurring_kind="subscription", recurring_interval_days=365) == ["subscription", "annual"]
    assert derive_tags(amount_cents=-650, merchant_key="cafe") == []


def test_an_outsized_charge_needs_enough_history_to_be_called_unusual():
    thin = MerchantStats.from_amounts([-600, -650])
    thick = MerchantStats.from_amounts([-600, -650, -700, -620, -680])
    assert derive_tags(amount_cents=-2600, merchant_key="cafe", stats=thin) == []
    assert derive_tags(amount_cents=-2600, merchant_key="cafe", stats=thick) == ["unusual"]
    assert derive_tags(amount_cents=-700, merchant_key="cafe", stats=thick) == []


def test_incoming_money_is_a_refund_not_an_outsized_charge():
    stats = MerchantStats.from_amounts([-600, -650, -700, -620, -680])
    assert derive_tags(amount_cents=2600, merchant_key="cafe", stats=stats) == ["refund"]


def test_each_family_of_tags_can_be_switched_off():
    assert derive_tags(amount_cents=-1599, merchant_key="netflix", recurring_kind="subscription", recurring_interval_days=30, tag_cadence=False) == []
    assert derive_tags(amount_cents=1599, merchant_key="netflix", tag_anomalies=False) == []


def test_merchant_stats_ignore_inflows():
    assert MerchantStats.from_amounts([600, 700]).sample_size == 0
    assert MerchantStats.from_amounts([]).typical_cents == 0
    assert MerchantStats.from_amounts([-600, -700, 5000]).sample_size == 2


def test_the_catalog_carries_a_colour_for_every_tag_clerk_writes():
    catalog = tag_catalog("clerk")
    names = [entry["tag"] for entry in catalog]
    assert names[0] == "clerk"
    assert {"subscription", "recurring", "annual", "unusual", "refund"} <= set(names)
    assert all(entry["color"].startswith("#") and entry["description"] for entry in catalog)
    assert "clerk" not in [entry["tag"] for entry in tag_catalog("")]
