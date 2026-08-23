"""Merging a category or payee in Actual leaves the transactions untouched.

Actual records a redirect in `category_mapping` / `payee_mapping` and resolves
it on every read -- its own `v_transactions` view and its query layer both join
through those tables. Reading the raw column instead yields an id that names
nothing, which is how categorized spending can silently lose its category.

These tests run against a real SQLite file using Actual's own schema, because
the whole point is what the database does, not what a fake says it does.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest
from actual.database import (
    Accounts,
    Categories,
    CategoryGroups,
    CategoryMapping,
    PayeeMapping,
    Payees,
    ReflectBudgets,
    Transactions,
)
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

from actual_clerk.clients.actual import read_redirects, transaction_dict, write_updates


@pytest.fixture
def session():
    path = pathlib.Path(tempfile.mkdtemp()) / "budget.sqlite"
    engine = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(CategoryGroups(id="grp", name="Bills", is_income=0, tombstone=0))
        session.add(Categories(id="cat-old", name="Old Rent", cat_group="grp", is_income=0, tombstone=0))
        session.add(Categories(id="cat-new", name="Rent", cat_group="grp", is_income=0, tombstone=0))
        session.add(Payees(id="pay-old", name="LANDLORD LLC", tombstone=0))
        session.add(Payees(id="pay-new", name="Landlord", tombstone=0))
        session.add(Accounts(id="acct", name="Checking", offbudget=0, closed=0, tombstone=0))
        # Actual maps every category and payee to itself when it is created.
        for identifier in ("cat-old", "cat-new"):
            session.add(CategoryMapping(id=identifier, transfer_id=identifier))
        for identifier in ("pay-old", "pay-new"):
            session.add(PayeeMapping(id=identifier, target_id=identifier))
        session.add(
            Transactions(
                id="t1", acct="acct", category_id="cat-old", payee_id="pay-old",
                amount=-255000, date=20260815, tombstone=0, is_parent=0, is_child=0,
            )
        )
        session.commit()
        yield session


def merge_category(session, *, old: str, new: str) -> None:
    """What Actual does when a category is deleted into a replacement."""
    session.exec(select(CategoryMapping).where(CategoryMapping.id == old)).one().transfer_id = new
    # Actual writes SQL directly and never lets an ORM cascade null the column.
    session.exec(text(f"DELETE FROM categories WHERE id = '{old}'"))
    session.commit()
    session.expire_all()


def merge_payee(session, *, old: str, new: str) -> None:
    session.exec(select(PayeeMapping).where(PayeeMapping.id == old)).one().target_id = new
    session.commit()
    session.expire_all()


def flatten(session, *, resolve: bool) -> dict:
    transaction = session.exec(select(Transactions).where(Transactions.id == "t1")).one()
    return transaction_dict(transaction, read_redirects(session) if resolve else None)


def test_without_a_merge_both_readings_agree(session):
    assert flatten(session, resolve=False) == flatten(session, resolve=True)


def test_a_merged_category_is_resolved_to_its_replacement(session):
    merge_category(session, old="cat-old", new="cat-new")
    resolved = flatten(session, resolve=True)
    assert resolved["category_id"] == "cat-new"
    assert resolved["category_name"] == "Rent"


def test_the_raw_column_is_what_made_the_spending_look_uncategorized(session):
    """The exact failure: an id that survives with nothing to resolve it to."""
    merge_category(session, old="cat-old", new="cat-new")
    raw = flatten(session, resolve=False)
    assert raw["category_id"] == "cat-old"
    assert raw["category_name"] == ""
    live_ids = {row.id for row in session.exec(select(Categories)).all()}
    assert raw["category_id"] not in live_ids


def test_actual_leaves_the_transaction_row_alone(session):
    merge_category(session, old="cat-old", new="cat-new")
    row = session.exec(select(Transactions).where(Transactions.id == "t1")).one()
    assert row.category_id == "cat-old", "the redirect is the only record of the merge"


def test_a_merged_payee_is_resolved_to_its_replacement(session):
    merge_payee(session, old="pay-old", new="pay-new")
    resolved = flatten(session, resolve=True)
    assert resolved["payee_name"] == "Landlord"
    # The merchant key feeds memory and recurring detection, so it must follow.
    assert resolved["merchant_key"] == "landlord"


def test_categories_and_payees_are_resolved_independently(session):
    merge_category(session, old="cat-old", new="cat-new")
    resolved = flatten(session, resolve=True)
    assert resolved["category_id"] == "cat-new"
    assert resolved["payee_name"] == "LANDLORD LLC", "an unmerged payee is left alone"


def test_a_chain_of_redirects_terminates(session):
    """Actual flattens chains, but a corrupt file must not spin forever."""
    session.add(CategoryMapping(id="cat-older", transfer_id="cat-old"))
    session.commit()
    merge_category(session, old="cat-old", new="cat-new")
    redirects = read_redirects(session)
    assert redirects.category("cat-older") == "cat-new"


def test_a_cycle_in_the_redirects_terminates(session):
    session.exec(select(CategoryMapping).where(CategoryMapping.id == "cat-old")).one().transfer_id = "cat-new"
    session.exec(select(CategoryMapping).where(CategoryMapping.id == "cat-new")).one().transfer_id = "cat-old"
    session.commit()
    redirects = read_redirects(session)
    assert redirects.category("cat-old") in {"cat-old", "cat-new"}


def test_an_uncategorized_transaction_stays_uncategorized(session):
    redirects = read_redirects(session)
    assert redirects.category(None) is None
    assert redirects.category("") == ""


# --------------------------------------------------------------- writing back


def test_a_write_to_a_category_that_does_not_exist_is_refused(session):
    """Actual does not enforce the column, so a stale id would be written."""
    result = write_updates(
        session, [{"transaction_id": "t1", "category_id": "cat-vanished"}], overwrite=True
    )
    assert result["applied"] == []
    assert result["skipped"] == [{"id": "t1", "reason": "unknown_category"}]
    row = session.exec(select(Transactions).where(Transactions.id == "t1")).one()
    assert row.category_id == "cat-old", "the transaction must be left exactly as it was"


def test_a_write_to_a_real_category_still_goes_through(session):
    result = write_updates(
        session, [{"transaction_id": "t1", "category_id": "cat-new"}], overwrite=True
    )
    assert result["applied"] == ["t1"]
    row = session.exec(select(Transactions).where(Transactions.id == "t1")).one()
    assert row.category_id == "cat-new"


def test_a_merged_away_category_can_no_longer_be_written(session):
    """Exactly the id a stale memory would offer after a reorganization."""
    merge_category(session, old="cat-old", new="cat-new")
    result = write_updates(
        session, [{"transaction_id": "t1", "category_id": "cat-old"}], overwrite=True
    )
    assert result["skipped"] == [{"id": "t1", "reason": "unknown_category"}]


def test_tags_are_still_applied_alongside_a_valid_category(session):
    result = write_updates(
        session,
        [{"transaction_id": "t1", "category_id": "cat-new", "add_tags": ["clerk"]}],
        overwrite=True,
    )
    assert result["applied"] == ["t1"]
    row = session.exec(select(Transactions).where(Transactions.id == "t1")).one()
    assert "#clerk" in (row.notes or "")


# ------------------------------------------------------------------ budgets


def budget_rows(session) -> dict[str, int]:
    """What collect_snapshot's budget resolution does, against real rows."""
    from actual_clerk.clients.actual import read_redirects

    redirects = read_redirects(session)
    resolved: dict[str, int] = {}
    inherited: dict[str, int] = {}
    for row in session.exec(select(ReflectBudgets)).all():
        if not row.category_id:
            continue
        target = redirects.category(row.category_id) or row.category_id
        if target == row.category_id:
            resolved[target] = int(row.amount or 0)
        else:
            inherited[target] = int(row.amount or 0)
    for category_id, amount in inherited.items():
        resolved.setdefault(category_id, amount)
    return resolved


def test_a_budget_left_behind_by_a_merge_follows_the_category(session):
    """The rent case: budget and spending both keyed to the pre-merge id."""
    session.add(ReflectBudgets(id="b1", month=202608, category_id="cat-old", amount=255000))
    session.commit()
    merge_category(session, old="cat-old", new="cat-new")
    assert budget_rows(session) == {"cat-new": 255000}


def test_an_unmerged_budget_is_untouched(session):
    session.add(ReflectBudgets(id="b1", month=202608, category_id="cat-new", amount=255000))
    session.commit()
    assert budget_rows(session) == {"cat-new": 255000}


def test_a_merge_never_invents_money(session):
    """If the surviving category already has its own row, that row wins."""
    session.add(ReflectBudgets(id="b1", month=202608, category_id="cat-old", amount=255000))
    session.add(ReflectBudgets(id="b2", month=202608, category_id="cat-new", amount=300000))
    session.commit()
    merge_category(session, old="cat-old", new="cat-new")
    assert budget_rows(session) == {"cat-new": 300000}, "amounts must not be summed"


def test_the_budget_and_the_spending_land_on_the_same_category(session):
    """Both halves must agree, or the category looks budgeted but unspent."""
    session.add(ReflectBudgets(id="b1", month=202608, category_id="cat-old", amount=255000))
    session.commit()
    merge_category(session, old="cat-old", new="cat-new")
    spending = flatten(session, resolve=True)
    assert spending["category_id"] == "cat-new"
    assert set(budget_rows(session)) == {"cat-new"}
