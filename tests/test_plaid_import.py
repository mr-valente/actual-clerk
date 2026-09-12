"""The pure planner that turns a Plaid change stream into Actual operations."""

from __future__ import annotations

import datetime

import pytest

from actual_clerk.domain.plaid_import import (
    PlaidTransactionError,
    convert,
    looks_like_plaid_id,
    plaid_amount_to_cents,
    plan_account,
    starting_balance_cents,
)

CUTOVER = datetime.date(2026, 9, 1)
PLAID_ID = "lE31jJ5RgqSyGmk8jvvBfwzzzGowozhLN9B3P"


def plaid_txn(transaction_id, amount, date, *, pending=False, pending_transaction_id=None, **extra):
    return {
        "transaction_id": transaction_id,
        "account_id": "acc-1",
        "amount": amount,
        "date": date,
        "pending": pending,
        "pending_transaction_id": pending_transaction_id,
        "name": "SQ *BLUE BOTTLE",
        "merchant_name": "Blue Bottle Coffee",
        **extra,
    }


def row(row_id, amount_cents, date, *, imported_id="", cleared=False, reconciled=False, **extra):
    return {
        "id": row_id,
        "date": datetime.date.fromisoformat(date),
        "amount_cents": amount_cents,
        "imported_id": imported_id,
        "cleared": cleared,
        "reconciled": reconciled,
        **extra,
    }


def plan(added=(), modified=(), removed=(), existing=(), **kwargs):
    return plan_account(
        actual_account_id="acct-1",
        external_account_id="acc-1",
        cutover=CUTOVER,
        added=list(added),
        modified=list(modified),
        removed=list(removed),
        existing=list(existing),
        **kwargs,
    )


def test_amounts_flip_sign_and_go_through_decimal():
    assert plaid_amount_to_cents(12.34) == -1234
    assert plaid_amount_to_cents(-50) == 5000
    assert plaid_amount_to_cents("0.1") == -10
    with pytest.raises(PlaidTransactionError):
        plaid_amount_to_cents("many")


def test_conversion_keeps_the_bank_text_for_merchant_memory():
    converted = convert(plaid_txn("t1", 4.5, "2026-09-03", original_description="SQ *BLUE BOTTLE 4471"))
    assert converted == {
        "date": datetime.date(2026, 9, 3),
        "amount_cents": -450,
        "payee_name": "Blue Bottle Coffee",
        "imported_payee": "SQ *BLUE BOTTLE 4471",
        "imported_id": "t1",
        "cleared": True,
    }
    pending = convert(plaid_txn("t2", 4.5, "2026-09-03", pending=True, merchant_name=None))
    assert pending["cleared"] is False
    assert pending["payee_name"] == "SQ *BLUE BOTTLE"
    assert pending["imported_payee"] == "SQ *BLUE BOTTLE"
    with pytest.raises(PlaidTransactionError):
        convert(plaid_txn("t3", 1, None))


def test_plaid_ids_are_recognised_and_other_providers_are_not():
    assert looks_like_plaid_id(PLAID_ID)
    assert not looks_like_plaid_id("ACT-3f2a-9b1c-simplefin")
    assert not looks_like_plaid_id("")
    assert not looks_like_plaid_id("short")


def test_new_transactions_are_imported_and_old_ones_stay_out():
    result = plan(added=[
        plaid_txn("t1", 4.5, "2026-09-03"),
        plaid_txn("t0", 4.5, "2026-08-30"),
        {**plaid_txn("other", 1, "2026-09-03"), "account_id": "acc-2"},
    ])
    assert [item["imported_id"] for item in result.imports] == ["t1"]
    assert result.skipped_before_cutover == 1
    assert result.summary()["imports"] == 1


def test_a_pending_to_posted_swap_adopts_the_existing_row():
    existing = [row("row-p", -450, "2026-09-03", imported_id="t-pending", cleared=False)]
    result = plan(
        added=[plaid_txn("t-posted", 4.75, "2026-09-05", pending_transaction_id="t-pending")],
        removed=[{"transaction_id": "t-pending", "account_id": "acc-1"}],
        existing=existing,
    )
    assert result.imports == []
    assert result.deletions == []
    [adoption] = result.adoptions
    assert adoption == {
        "transaction_id": "row-p",
        "previous_imported_id": "t-pending",
        "reason": "posted",
        "imported_id": "t-posted",
        "cleared": True,
        "amount_cents": -475,
        "date": datetime.date(2026, 9, 5),
    }


def test_a_reversed_pending_charge_is_deleted_but_a_cleared_one_is_kept():
    existing = [
        row("row-p", -450, "2026-09-03", imported_id="t-pending"),
        row("row-c", -900, "2026-09-02", imported_id="t-cleared", cleared=True),
    ]
    result = plan(
        removed=[
            {"transaction_id": "t-pending", "account_id": "acc-1"},
            {"transaction_id": "t-cleared", "account_id": "acc-1"},
            {"transaction_id": "t-unknown", "account_id": "acc-1"},
        ],
        existing=existing,
    )
    assert [item["transaction_id"] for item in result.deletions] == ["row-p"]
    assert [item["transaction_id"] for item in result.kept] == ["row-c"]
    assert result.unknown_removed == 1


def test_a_modified_transaction_settles_the_matching_row_in_place():
    existing = [row("row-1", -450, "2026-09-03", imported_id="t1", cleared=False)]
    result = plan(modified=[plaid_txn("t1", 4.75, "2026-09-03")], existing=existing)
    [adoption] = result.adoptions
    assert adoption["reason"] == "settled"
    assert adoption["cleared"] is True
    assert adoption["amount_cents"] == -475
    assert "imported_id" not in adoption
    unchanged = plan(modified=[plaid_txn("t1", 4.5, "2026-09-03", pending=True)], existing=existing)
    assert unchanged.empty
    # A date correction alone is still a settlement: Actual's import would never move the row.
    moved = plan(modified=[plaid_txn("t1", 4.5, "2026-09-04", pending=True)], existing=existing)
    [adoption] = moved.adoptions
    assert adoption == {"transaction_id": "row-1", "previous_imported_id": "t1", "reason": "settled", "date": datetime.date(2026, 9, 4)}


def test_a_foreign_row_near_the_cutover_is_adopted_by_amount_and_date():
    existing = [
        row("sf-1", -450, "2026-09-02", imported_id="ACT-simplefin-1", cleared=True),
        row("sf-far", -450, "2026-10-20", imported_id="ACT-simplefin-2", cleared=True),
        row("plaid-old", -450, "2026-09-02", imported_id=PLAID_ID, cleared=True),
        row("manual", -450, "2026-09-02", imported_id="", cleared=True),
    ]
    result = plan(added=[plaid_txn("t1", 4.5, "2026-09-03")], existing=existing)
    assert result.imports == []
    [adoption] = result.adoptions
    assert adoption["transaction_id"] == "sf-1"
    assert adoption["reason"] == "cutover"
    assert adoption["imported_id"] == "t1"
    assert adoption["previous_imported_id"] == "ACT-simplefin-1"
    assert "amount_cents" not in adoption
    # The same row is not adopted twice: a second identical charge is imported.
    twice = plan(
        added=[plaid_txn("t1", 4.5, "2026-09-03"), plaid_txn("t2", 4.5, "2026-09-04")],
        existing=existing,
    )
    assert len(twice.adoptions) == 1
    assert [item["imported_id"] for item in twice.imports] == ["t2"]


def test_adoption_prefers_the_closest_date_and_respects_the_window():
    existing = [
        row("near", -450, "2026-09-04", imported_id="ACT-a", cleared=True),
        row("nearer", -450, "2026-09-03", imported_id="ACT-b", cleared=True),
    ]
    result = plan(added=[plaid_txn("t1", 4.5, "2026-09-03")], existing=existing)
    assert result.adoptions[0]["transaction_id"] == "nearer"
    outside = plan(added=[plaid_txn("t1", 4.5, "2026-09-25")], existing=existing, adopt_window_days=3)
    assert outside.adoptions == []
    assert len(outside.imports) == 1


def test_split_children_and_starting_balances_are_never_touched():
    existing = [
        row("child", -450, "2026-09-03", imported_id="t1", is_child=True),
        row("opening", 100000, "2026-08-31", imported_id="", is_starting_balance=True),
    ]
    result = plan(removed=[{"transaction_id": "t1", "account_id": "acc-1"}], existing=existing)
    assert result.deletions == []
    assert result.unknown_removed == 1


def test_the_starting_balance_makes_posted_history_add_up():
    imports = [
        {"amount_cents": -450, "cleared": True},
        {"amount_cents": -1000, "cleared": False},
        {"amount_cents": 20000, "cleared": True},
    ]
    assert starting_balance_cents(current_balance_cents=50000, imports=imports) == 50000 - 19550
    assert starting_balance_cents(current_balance_cents=None, imports=imports) is None
