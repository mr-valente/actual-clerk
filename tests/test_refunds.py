from __future__ import annotations

import datetime

from actual_clerk.categorize import SOURCE_REFUND, STATUS_APPLIED, Categorizer
from actual_clerk.domain.refunds import REFUND_WINDOW_DAYS, refunded_charge

from .factories import FakeModel, snapshot, transaction

TODAY = datetime.date(2026, 10, 22)


def charge(days_ago=2, amount=-5602, *, payee="SP LEMONHEAD GLITTER", **kwargs):
    kwargs.setdefault("category_id", "cat-dining")
    return transaction(TODAY - datetime.timedelta(days=days_ago), amount, payee=payee, **kwargs)


def refund(amount=5602, *, payee="SP LEMONHEAD GLITTER", **kwargs):
    return transaction(TODAY, amount, payee=payee, **kwargs)


async def run(settings, snap, *, model=None):
    categorizer = Categorizer(settings, model_client=model)
    try:
        return await categorizer.run(snap, today=TODAY)
    finally:
        await categorizer.close()


# ------------------------------------------------------------------ pairing


def test_a_refund_for_exactly_a_filed_charge_names_it():
    bought = charge()
    assert refunded_charge(refund(), [bought]) is bought


def test_a_refund_decorated_differently_still_names_its_charge():
    bought = charge(payee="SP LEMONHEAD GLITTER")
    assert refunded_charge(refund(payee="LEMONHEAD GLITTER REFUND"), [bought]) is bought


def test_a_refund_reaches_its_charge_through_an_alias():
    bought = charge(payee="Steam")
    assert refunded_charge(refund(payee="Valve"), [bought], aliases={"valve": "steam"}) is bought


def test_a_partial_refund_names_no_charge():
    assert refunded_charge(refund(2000), [charge()]) is None


def test_a_refund_from_another_shop_names_no_charge():
    assert refunded_charge(refund(payee="Tailscale"), [charge()]) is None


def test_a_refund_on_another_account_names_no_charge():
    assert refunded_charge(refund(account_id="acct-card"), [charge()]) is None


def test_a_charge_outside_the_window_or_after_the_refund_is_not_named():
    old = charge(days_ago=REFUND_WINDOW_DAYS + 1)
    later = charge(days_ago=-1)
    assert refunded_charge(refund(), [old, later]) is None


def test_an_uncategorized_or_transfer_charge_is_not_named():
    loose = charge(category_id=None)
    moved = charge(is_transfer=True)
    assert refunded_charge(refund(), [loose, moved]) is None


def test_identical_charges_filed_the_same_way_are_one_answer():
    older, newer = charge(days_ago=9), charge(days_ago=3)
    assert refunded_charge(refund(), [older, newer]) is newer


def test_identical_charges_filed_different_ways_decide_nothing():
    gifts = charge(days_ago=9, category_id="cat-groceries")
    assert refunded_charge(refund(), [gifts, charge(days_ago=3)]) is None


# ------------------------------------------------------------------ filing


async def test_a_refund_files_where_its_charge_was_without_asking_the_model(settings):
    model = FakeModel()
    result = await run(settings, snapshot(transactions=[charge(), refund()]), model=model)
    assert model.calls == []
    [proposal] = result.proposals
    assert proposal.source == SOURCE_REFUND
    assert proposal.status == STATUS_APPLIED
    assert proposal.category_id == "cat-dining"
    assert "refund" in proposal.tags
    assert result.updates[0]["category_id"] == "cat-dining"


async def test_a_refund_waits_for_approval_when_the_apply_mode_says_so(settings):
    settings.apply_mode = "review"
    result = await run(settings, snapshot(transactions=[charge(), refund()]))
    [proposal] = result.proposals
    assert proposal.source == SOURCE_REFUND
    assert proposal.status != STATUS_APPLIED
    assert proposal.category_id == "cat-dining"


async def test_a_charge_in_a_deleted_category_lends_the_refund_nothing(settings):
    snap = snapshot(transactions=[charge(category_id="cat-gone"), refund()])
    result = await run(settings, snap, model=FakeModel())
    assert result.proposals[0].source != SOURCE_REFUND
