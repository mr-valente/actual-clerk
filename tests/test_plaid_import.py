"""The pure planner that turns a Plaid change stream into Actual operations."""

from __future__ import annotations

import datetime

import pytest

from actual_clerk.domain.plaid_import import (
    PlaidTransactionError,
    convert,
    looks_like_plaid_id,
    payee_for,
    plaid_amount_to_cents,
    plan_account,
    retidy,
    short_payee,
    starting_balance_cents,
    transaction_date,
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
        "notes": "SQ *BLUE BOTTLE 4471",
        "imported_id": "t1",
        "cleared": True,
    }
    pending = convert(plaid_txn("t2", 4.5, "2026-09-03", pending=True, merchant_name=None))
    assert pending["cleared"] is False
    assert pending["payee_name"] == "Sq *Blue Bottle"
    assert pending["imported_payee"] == "SQ *BLUE BOTTLE"
    assert pending["notes"] == "SQ *BLUE BOTTLE"
    with pytest.raises(PlaidTransactionError):
        convert(plaid_txn("t3", 1, None))


def test_a_line_without_a_merchant_gets_a_short_payee_and_keeps_the_line_in_notes():
    dividend = convert(plaid_txn(
        "t1", -23.84, "2026-09-30", merchant_name=None,
        name="Dividends Apy Earned 1.07% 07/01/26 to 09/30/26",
        original_description="DIVIDENDS APY Earned 1.07% 07/01/26 to 09/30/26",
    ))
    assert dividend["payee_name"] == "Dividend"
    assert dividend["notes"] == "DIVIDENDS APY Earned 1.07% 07/01/26 to 09/30/26"
    payroll = convert(plaid_txn(
        "t2", -5286.07, "2026-09-30", merchant_name=None,
        name="Oak Knoll School Type: Payroll Co: Oak Knoll School",
    ))
    assert payroll["payee_name"] == "Oak Knoll School"
    assert payroll["notes"] == "Oak Knoll School Type: Payroll Co: Oak Knoll School"
    assert convert(plaid_txn("t3", 1, "2026-09-30", merchant_name=None, name=""))["payee_name"] == "Unknown"
    assert convert(plaid_txn("t3", 1, "2026-09-30", merchant_name=None, name=""))["notes"] is None


def test_a_named_counterparty_beats_the_bank_line_but_not_a_payment_terminal():
    base = {"merchant_name": None, "name": "Ach Deposit 991 Acme Payroll"}
    assert payee_for({**base, "counterparties": [
        {"name": "Square", "type": "payment_terminal", "confidence_level": "VERY_HIGH"},
        {"name": "Venmo", "type": "payment_app", "confidence_level": "HIGH"},
        {"name": "Acme Corp", "type": "income_source", "confidence_level": "HIGH"},
    ]}) == "Acme Corp"
    assert payee_for({**base, "counterparties": [
        {"name": "Square", "type": "payment_terminal", "confidence_level": "VERY_HIGH"},
        {"name": "Acme Corp", "type": "income_source", "confidence_level": "LOW"},
    ]}) == "Ach Deposit"
    assert payee_for({**base, "merchant_name": "Acme", "counterparties": [
        {"name": "Acme Corp", "type": "income_source"},
    ]}) == "Acme"


@pytest.mark.parametrize(
    ("line", "payee"),
    [
        ("OAK KNOLL SCHOOL TYPE: PAYROLL  CO: OAK KNOLL SCHOOL", "Oak Knoll School"),
        ("INTEREST CHARGE:PURCHASES", "Interest Charge"),
        ("KINGS ##3634", "Kings"),
        ("ELECTRONIC BALANCE TRANSFER R1232829", "Electronic Balance Transfer"),
        ("TRADER JOE'S #604", "Trader Joe's"),
        ("Check 1234", "Check"),
        ("7-ELEVEN 36883", "7-Eleven"),
        ("12 34", "12 34"),
        ("", ""),
    ],
)
def test_a_short_payee_keeps_who_the_line_names(line, payee):
    assert short_payee(line) == payee


def early_import(**extra):
    return {
        "id": "r1", "date": datetime.date(2026, 9, 30), "amount_cents": 2384,
        "imported_id": PLAID_ID,
        "payee_name": "Dividends Apy Earned 1.07% 07/01/26 to 09/30/26",
        "imported_description": "DIVIDENDS APY Earned 1.07% 07/01/26 to 09/30/26",
        "notes": "#clerk",
        **extra,
    }


def test_an_early_import_is_retidied_like_a_new_one():
    assert retidy(early_import()) == {
        "payee_name": "Dividend",
        "notes": "DIVIDENDS APY Earned 1.07% 07/01/26 to 09/30/26 #clerk",
    }
    # A merchant payee is right already; only the notes are filled.
    assert retidy(early_import(payee_name="Trader Joe's", imported_description="TRADER JOE S #604", notes="")) == {
        "notes": "TRADER JOE S #604",
    }


def test_retidy_leaves_the_users_own_words_and_other_providers_alone():
    # Renamed by the user (or an Actual rule): the payee no longer echoes the bank line.
    assert "payee_name" not in retidy(early_import(payee_name="Credit Union Dividend"))
    # Notes the user wrote are kept, and so are notes already filled.
    assert "notes" not in retidy(early_import(notes="quarterly #clerk"))
    assert retidy(early_import(payee_name="Dividend", notes="DIVIDENDS APY Earned 1.07% 07/01/26 to 09/30/26")) == {}
    assert retidy(early_import(imported_id="TRN-0886b2ec-bbed-4c38-8f2b-f66a5dcd43e0")) == {}
    assert retidy(early_import(is_child=True)) == {}
    assert retidy(early_import(imported_description="")) == {}
    assert "payee_name" not in retidy(early_import(is_transfer=True))


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


def test_a_row_is_dated_on_the_day_of_purchase_not_the_day_it_posted():
    # Bought on the last evening of September, posted on October 2.
    late = plaid_txn("t-late", 63.38, "2026-10-02", authorized_date="2026-09-30")
    assert transaction_date(late) == datetime.date(2026, 9, 30)
    assert convert(late)["date"] == datetime.date(2026, 9, 30)
    # A bank that gives no authorisation date leaves the posting date.
    assert convert(plaid_txn("t", 1, "2026-10-02", authorized_date=None))["date"] == datetime.date(2026, 10, 2)
    # Credited on the 30th, "authorised" on the 1st: it still happened on the 30th.
    dividend = plaid_txn("t-div", -23.84, "2026-09-30", authorized_date="2026-10-01")
    assert transaction_date(dividend) == datetime.date(2026, 9, 30)


def test_posting_does_not_move_a_pending_row_into_the_next_month():
    existing = [row("row-p", -6338, "2026-09-30", imported_id="t-pending")]
    result = plan(
        added=[plaid_txn("t-posted", 63.38, "2026-10-02", authorized_date="2026-09-30",
                         pending_transaction_id="t-pending")],
        removed=[{"transaction_id": "t-pending", "account_id": "acc-1"}],
        existing=existing,
    )
    [adoption] = result.adoptions
    assert "date" not in adoption
    assert adoption["cleared"] is True


def test_a_row_the_phone_dated_keeps_its_date():
    existing = [row("row-1", -6338, "2026-09-30", imported_id="t1")]
    modified = [plaid_txn("t1", 63.38, "2026-10-02", authorized_date="2026-10-01")]
    assert plan(modified=modified, existing=existing).adoptions[0]["date"] == datetime.date(2026, 10, 1)
    pinned = plan(modified=modified, existing=existing, pinned_dates={"row-1"})
    [adoption] = pinned.adoptions
    assert "date" not in adoption and adoption["cleared"] is True


def test_the_cutover_still_reads_the_posting_date():
    # Made before the cutover but posted after it: the previous provider never
    # saw it posted, so Plaid delivers it, dated on the day it was made.
    result = plan(added=[plaid_txn("t1", 4.5, "2026-09-02", authorized_date="2026-08-31")])
    assert [item["date"] for item in result.imports] == [datetime.date(2026, 8, 31)]


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


def test_a_reconciled_row_only_takes_the_id():
    existing = [row("row-r", -450, "2026-09-03", imported_id="t-pending", cleared=True, reconciled=True)]
    result = plan(
        added=[plaid_txn("t-posted", 4.75, "2026-09-05", pending_transaction_id="t-pending")],
        removed=[{"transaction_id": "t-pending", "account_id": "acc-1"}],
        existing=existing,
    )
    [adoption] = result.adoptions
    assert adoption == {"transaction_id": "row-r", "previous_imported_id": "t-pending",
                        "reason": "posted", "imported_id": "t-posted"}


def test_a_hand_cleared_row_is_not_uncleared_by_a_pending_update():
    existing = [row("row-1", -450, "2026-09-03", imported_id="t1", cleared=True)]
    result = plan(modified=[plaid_txn("t1", 4.5, "2026-09-03", pending=True)], existing=existing)
    assert result.empty


def test_a_split_parent_settles_everything_but_its_amount():
    existing = [
        row("parent", -450, "2026-09-03", imported_id="t-pending", is_parent=True),
        row("child-a", -200, "2026-09-03", is_child=True),
        row("child-b", -250, "2026-09-03", is_child=True),
    ]
    result = plan(
        added=[plaid_txn("t-posted", 4.75, "2026-09-04", pending_transaction_id="t-pending")],
        removed=[{"transaction_id": "t-pending", "account_id": "acc-1"}],
        existing=existing,
    )
    assert result.imports == [] and result.deletions == []
    [adoption] = result.adoptions
    assert adoption["transaction_id"] == "parent"
    assert adoption["imported_id"] == "t-posted" and adoption["cleared"] is True
    assert "amount_cents" not in adoption


def test_a_transaction_removed_later_in_the_same_read_is_not_imported():
    result = plan(
        added=[plaid_txn("t-pending", 4.5, "2026-09-03", pending=True),
               plaid_txn("t-posted", 4.5, "2026-09-04", pending_transaction_id="t-pending")],
        removed=[{"transaction_id": "t-pending", "account_id": "acc-1"}],
    )
    assert [item["imported_id"] for item in result.imports] == ["t-posted"]
    assert result.unknown_removed == 1


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
