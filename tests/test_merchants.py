from __future__ import annotations

import pytest

from actual_clerk.domain.merchants import (
    keys_related,
    merchant_label,
    normalize_merchant,
    rule_match_value,
)

# The same merchant as three different processors write it.
BLUE_BOTTLE = [
    "SQ *BLUE BOTTLE COFFEE 4471",
    "TST* Blue Bottle Coffee - Oakland",
    "BLUE BOTTLE COFFEE #4471 OAKLAND CA",
]


def test_processor_decoration_collapses_to_one_key():
    keys = {normalize_merchant(descriptor) for descriptor in BLUE_BOTTLE}
    assert keys == {"blue bottle coffee"}


@pytest.mark.parametrize(
    ("descriptor", "expected"),
    [
        ("POS DEBIT 08/14 STARBUCKS #1234 SEATTLE WA", "starbucks seattle"),
        ("STARBUCKS STORE 01234", "starbucks"),
        ("NETFLIX.COM  866-579-7172 CA", "netflix"),
        ("ACH DEBIT PG&E WEB PMT 123456789", "pg&e"),
        ("CHECKCARD 0811 WHOLE FOODS MKT 10285 SAN FRAN CA 24492159223", "whole foods mkt"),
        ("7-ELEVEN 33445", "7eleven"),
        ("Spotify USA", "spotify"),
    ],
)
def test_known_descriptor_shapes(descriptor, expected):
    assert normalize_merchant(descriptor) == expected


def test_aliases_reconcile_names_normalization_cannot():
    assert normalize_merchant("AMZN Mktp US*2X4B95TY3") == "amazon"
    assert normalize_merchant("Amazon.com*RT4G91QP2") == "amazon"


def test_store_location_is_reconciled_by_relation_not_by_truncation():
    # Truncating harder would lose identity; relating the keys recovers it.
    assert normalize_merchant("POS DEBIT STARBUCKS SEATTLE WA") != normalize_merchant("STARBUCKS")
    assert keys_related("starbucks", "starbucks seattle")


def test_unrelated_short_keys_do_not_relate():
    assert not keys_related("bp", "bp fuel station")
    assert not keys_related("starbucks seattle", "starbucks portland")
    assert not keys_related("", "starbucks")


def test_related_requires_a_prefix_not_a_substring():
    assert not keys_related("bottle coffee", "blue bottle coffee")


def test_pure_noise_yields_no_key():
    assert normalize_merchant("") == ""
    assert normalize_merchant(None) == ""
    assert normalize_merchant("12345678") == ""
    assert normalize_merchant("   ") == ""


def test_an_opaque_alphanumeric_name_still_matches_itself_exactly():
    assert normalize_merchant("86st") == "86st"
    assert normalize_merchant("86st") == normalize_merchant("86st")


def test_first_usable_descriptor_wins():
    assert normalize_merchant(None, "", "SQ *BLUE BOTTLE 4471") == "blue bottle"


def test_rule_match_value_must_appear_verbatim():
    value = rule_match_value("SQ *BLUE BOTTLE COFFEE 4471")
    assert value == "BLUE BOTTLE COFFEE"
    assert value in "SQ *BLUE BOTTLE COFFEE 4471"


def test_rule_match_value_refuses_a_value_that_was_never_written():
    # The alias and the hyphen join both invent text the statement never held.
    assert rule_match_value("7-ELEVEN 33445") == ""


def test_rule_match_value_refuses_a_dangerously_broad_match():
    # `UBER` alone would swallow rides and meal delivery alike.
    assert rule_match_value("UBER *EATS 8005928996") == ""


def test_merchant_label_preserves_the_original_words():
    assert merchant_label("  Blue   Bottle Coffee ") == "Blue Bottle Coffee"
    assert merchant_label(None, "", "Netflix") == "Netflix"
    assert merchant_label(None, "") == ""


def test_accents_and_case_do_not_split_a_merchant():
    assert normalize_merchant("CAFÉ RIO 991") == normalize_merchant("cafe rio")
