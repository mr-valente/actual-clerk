"""A quiet account re-delivers the same transactions on every sync.

The import asks each account for everything since the newest transaction it
already holds, so an account with no recent activity keeps re-delivering its
tail. A re-delivered transaction is matched to the stored one and has its payee
and notes overwritten in place -- and `reconcile_transaction` assigns
`payee_id` directly rather than through `set_transaction_payee`, so a payee a
rule had turned into a transfer silently reverts while `transferred_id` still
points at the counterpart.

Re-running that rule then finds an ordinary payee, skips the clean-up that
would have removed the old transfer, and builds a second counterpart. Once per
sync, forever. These tests run against a real database because that interaction
is the whole bug.
"""

from __future__ import annotations

import datetime
import pathlib
import tempfile

import pytest
from actual.database import Accounts, Payees, Transactions
from actual.queries import (
    create_transaction,
    get_or_create_payee,
    reconcile_transaction,
    set_transaction_payee,
)
from sqlmodel import Session, SQLModel, create_engine, select

from actual_clerk.clients.actual import restore_overwritten, transaction_fingerprints

WHEN = datetime.date(2026, 7, 10)
AMOUNT = -8793.50


@pytest.fixture
def session():
    path = pathlib.Path(tempfile.mkdtemp()) / "budget.sqlite"
    session = Session(create_engine(f"sqlite:///{path}"))
    SQLModel.metadata.create_all(session.get_bind())
    session.add(Accounts(id="wf", name="Wells Fargo", offbudget=0, closed=0, tombstone=0))
    session.add(Accounts(id="co", name="Capital One", offbudget=0, closed=0, tombstone=0))
    # Actual gives every account a payee that means "transfer to this account".
    session.add(Payees(id="p-wf", name="Wells Fargo", transfer_acct="wf", tombstone=0))
    session.add(Payees(id="p-co", name="Capital One", transfer_acct="co", tombstone=0))
    session.add(Payees(id="p-ebt", name="Electronic Balance Transfer", tombstone=0))
    session.commit()
    yield session


def first_import(session) -> Transactions:
    transaction = create_transaction(
        session, WHEN, session.get(Accounts, "wf"),
        get_or_create_payee(session, "Electronic Balance Transfer"),
        notes="", amount=AMOUNT, imported_id="SF-123", cleared=True,
    )
    session.commit()
    set_transaction_payee(session, transaction, session.get(Payees, "p-co"))
    session.commit()
    return transaction


def counterparts(session) -> int:
    rows = session.exec(select(Transactions).where(Transactions.acct == "co")).all()
    return len([row for row in rows if not row.tombstone])


def redeliver(session) -> Transactions:
    """SimpleFIN sends the same transaction again, because the account is quiet."""
    return reconcile_transaction(
        session, WHEN, session.get(Accounts, "wf"), "Electronic Balance Transfer", "",
        amount=AMOUNT, imported_id="SF-123", cleared=True,
    )


def sync(session, *, guarded: bool) -> None:
    before = transaction_fingerprints(session)
    match = redeliver(session)
    imported = [match]
    if guarded:
        fresh, _ = restore_overwritten(imported, before)
    else:
        fresh = imported  # what running rules over every import used to do
    for transaction in fresh:
        set_transaction_payee(session, transaction, session.get(Payees, "p-co"))
    session.commit()


# ------------------------------------------------------------- the mechanism


def test_a_redelivery_reverts_a_payee_a_rule_had_set(session):
    transaction = first_import(session)
    assert transaction.payee_id == "p-co"
    match = redeliver(session)
    session.commit()
    assert match.id == transaction.id, "it matches on imported_id, not a new row"
    assert match.payee_id == "p-ebt", "the import overwrote the payee in place"


def test_the_reverted_payee_leaves_the_transfer_link_dangling(session):
    """Why re-running the rule duplicates instead of replacing."""
    transaction = first_import(session)
    linked = transaction.transferred_id
    assert linked
    redeliver(session)
    session.commit()
    assert transaction.transferred_id == linked, "still points at the counterpart"
    assert session.get(Payees, transaction.payee_id).transfer_acct is None


def test_without_the_guard_every_sync_adds_a_counterpart(session):
    first_import(session)
    assert counterparts(session) == 1
    for expected in (2, 3, 4):
        sync(session, guarded=False)
        assert counterparts(session) == expected


# ----------------------------------------------------------------- the guard


def test_with_the_guard_the_count_never_grows(session):
    first_import(session)
    for _ in range(5):
        sync(session, guarded=True)
        assert counterparts(session) == 1


def test_the_guard_puts_the_transfer_payee_back(session):
    transaction = first_import(session)
    before = transaction_fingerprints(session)
    redeliver(session)
    restore_overwritten([transaction], before)
    assert transaction.payee_id == "p-co"


def test_the_guard_puts_clerk_s_tags_back(session):
    """The import overwrites notes, which is where Clerk writes its tags."""
    transaction = first_import(session)
    transaction.notes = "Balance transfer #clerk #unusual"
    session.commit()
    before = transaction_fingerprints(session)
    match = redeliver(session)
    session.commit()
    assert match.notes == "", "the import wiped them"
    _, protected = restore_overwritten([match], before)
    assert protected == 1
    assert match.notes == "Balance transfer #clerk #unusual"


def test_a_transaction_that_was_not_touched_is_not_counted_as_protected(session):
    transaction = first_import(session)
    before = transaction_fingerprints(session)
    fresh, protected = restore_overwritten([transaction], before)
    assert (fresh, protected) == ([], 0)


def test_a_genuinely_new_transaction_is_reported_as_fresh(session):
    """Rules must still run on real imports, which is the point of the sync."""
    before = transaction_fingerprints(session)
    arrived = create_transaction(
        session, WHEN, session.get(Accounts, "wf"),
        get_or_create_payee(session, "Electronic Balance Transfer"),
        notes="", amount=AMOUNT, imported_id="SF-999", cleared=True,
    )
    session.commit()
    fresh, protected = restore_overwritten([arrived], before)
    assert fresh == [arrived]
    assert protected == 0


def test_new_and_redelivered_transactions_are_separated_in_one_pass(session):
    existing = first_import(session)
    before = transaction_fingerprints(session)
    arrived = create_transaction(
        session, WHEN, session.get(Accounts, "wf"),
        get_or_create_payee(session, "Electronic Balance Transfer"),
        notes="", amount=-100.0, imported_id="SF-777", cleared=True,
    )
    session.commit()
    redeliver(session)
    fresh, protected = restore_overwritten([existing, arrived], before)
    assert fresh == [arrived]
    assert protected == 1
    assert existing.payee_id == "p-co"
