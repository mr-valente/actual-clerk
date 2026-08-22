"""The cleared/uncleared split, tested against Actual's real schema.

`account_balances` runs SQL against the tables actualpy defines, so these tests
build those tables in memory rather than mocking the result. No Actual server is
involved.
"""

from __future__ import annotations

import itertools

import pytest
from actual.database import Accounts, Transactions
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from actual_clerk.clients.actual import account_balances

_ids = itertools.count(1)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def add_account(session, account_id="a1", name="Checking"):
    session.add(Accounts(id=account_id, name=name, offbudget=0, closed=0, tombstone=0))


def add_transaction(session, account_id="a1", *, amount, cleared=1, **kwargs):
    defaults = {
        "id": f"t{next(_ids)}",
        "acct": account_id,
        "amount": amount,
        "date": 20260801,
        "cleared": cleared,
        "is_parent": 0,
        "tombstone": 0,
    }
    defaults.update(kwargs)
    session.add(Transactions(**defaults))


def test_cleared_and_total_are_reported_separately(session):
    add_account(session)
    add_transaction(session, amount=10000, cleared=1)
    add_transaction(session, amount=-2500, cleared=1)
    add_transaction(session, amount=-1000, cleared=0)
    session.flush()

    total, cleared = account_balances(session)["a1"]
    assert total == 6500
    assert cleared == 7500
    assert total - cleared == -1000


def test_an_account_with_only_uncleared_spending(session):
    add_account(session)
    add_transaction(session, amount=-4200, cleared=0)
    session.flush()

    total, cleared = account_balances(session)["a1"]
    assert (total, cleared) == (-4200, 0)


def test_deleted_and_split_parent_rows_are_excluded(session):
    """A split's parent carries the same money as its children; counting both
    would double it, and a deleted row is not money at all."""
    add_account(session)
    add_transaction(session, amount=10000, cleared=1)
    session.flush()
    # Written as raw rows: Actual's ORM refuses to set `is_parent` on a
    # detached object, and these are exactly the shapes Clerk must ignore.
    session.exec(
        text(
            "INSERT INTO transactions (id, acct, amount, date, cleared, isParent, tombstone) "
            "VALUES ('deleted', 'a1', -9999, 20260801, 1, 0, 1),"
            "       ('parent',  'a1', -8888, 20260801, 1, 1, 0)"
        )
    )

    assert account_balances(session)["a1"] == (10000, 10000)


def test_balances_are_grouped_per_account(session):
    add_account(session, "a1", "Checking")
    add_account(session, "a2", "Card")
    add_transaction(session, "a1", amount=10000, cleared=1)
    add_transaction(session, "a2", amount=-3000, cleared=0)
    session.flush()

    balances = account_balances(session)
    assert balances["a1"] == (10000, 10000)
    assert balances["a2"] == (-3000, 0)


def test_an_account_with_no_transactions_is_simply_absent(session):
    add_account(session)
    session.flush()
    assert account_balances(session) == {}


def test_actuals_own_default_makes_an_unmarked_transaction_cleared(session):
    """Actual's schema defaults `cleared` to 1, and Clerk must not fight it."""
    add_account(session)
    add_transaction(session, amount=-500, cleared=None)
    session.flush()
    assert account_balances(session)["a1"] == (-500, -500)


def test_a_genuine_null_cleared_flag_is_treated_as_uncleared(session):
    """A row written outside the ORM could still hold NULL; err on uncleared."""
    add_account(session)
    session.flush()
    session.exec(
        text(
            "INSERT INTO transactions (id, acct, amount, date, cleared, isParent, tombstone) "
            "VALUES ('raw', 'a1', -500, 20260801, NULL, 0, 0)"
        )
    )
    assert account_balances(session)["a1"] == (-500, 0)


def test_the_split_matches_actuals_own_balance_property(session):
    """Actual's headline balance counts everything; ours must agree with it."""
    add_account(session)
    add_transaction(session, amount=10000, cleared=1)
    add_transaction(session, amount=-2500, cleared=1)
    add_transaction(session, amount=-1000, cleared=0)
    session.flush()

    account = session.get(Accounts, "a1")
    total, _ = account_balances(session)["a1"]
    assert total == round(account.balance * 100)
