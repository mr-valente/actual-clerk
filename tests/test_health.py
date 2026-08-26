from __future__ import annotations

import datetime

from actual_clerk.domain.health import (
    ActualAccountInfo,
    SimpleFinAccountInfo,
    UnconfirmedTransferInfo,
    evaluate_accounts,
    summarize,
    worst_status,
)

NOW = datetime.datetime(2026, 8, 21, 12, 0, tzinfo=datetime.UTC)
TODAY = NOW.date()


def actual_account(name, **kwargs):
    defaults = {
        "id": f"acct-{name.lower()}",
        "name": name,
        "sync_source": "simpleFin",
        "external_id": f"sf-{name.lower()}",
        "balance_cents": 100000,
        "last_transaction_date": TODAY - datetime.timedelta(days=1),
    }
    defaults.update(kwargs)
    return ActualAccountInfo(**defaults)


def remote_account(name, **kwargs):
    defaults = {
        "id": f"sf-{name.lower()}",
        "name": name,
        "org_name": "Test Bank",
        "connection_id": "conn-1",
        "balance_cents": 100000,
        "balance_date": NOW - datetime.timedelta(hours=2),
    }
    defaults.update(kwargs)
    return SimpleFinAccountInfo(**defaults)


def evaluate(accounts, remote=(), errors=(), **kwargs):
    return evaluate_accounts(
        accounts=accounts, remote_accounts=remote, errors=errors, now=NOW, **kwargs
    )


def test_a_matching_current_account_is_healthy():
    [result] = evaluate([actual_account("Checking")], [remote_account("Checking")])
    assert result.status == "ok"
    assert result.drift_cents == 0
    assert result.balance_age_hours == 2.0
    assert result.remote_balance_date == (NOW - datetime.timedelta(hours=2)).isoformat()


def test_an_account_level_error_is_reported_verbatim():
    [result] = evaluate(
        [actual_account("Checking")],
        [remote_account("Checking")],
        [{"code": "act.failed", "msg": "Reauthorize your bank", "account_id": "sf-checking"}],
    )
    assert result.status == "error"
    assert result.detail == "Reauthorize your bank"


def test_a_connection_error_reaches_every_account_on_that_connection():
    results = evaluate(
        [actual_account("Checking"), actual_account("Savings")],
        [remote_account("Checking"), remote_account("Savings")],
        [{"code": "con.auth", "msg": "Connection expired", "conn_id": "conn-1"}],
    )
    assert {item.status for item in results} == {"error"}


def test_a_stale_balance_is_caught_even_though_nothing_reported_an_error():
    """The failure mode this whole page exists for: silence, not an error."""
    [result] = evaluate(
        [actual_account("Checking")],
        [remote_account("Checking", balance_date=NOW - datetime.timedelta(days=5))],
    )
    assert result.status == "stale"
    assert "5.0 days old" in result.detail


def test_a_balance_disagreement_means_transactions_are_missing():
    [result] = evaluate(
        [actual_account("Checking", balance_cents=100000)],
        [remote_account("Checking", balance_cents=92500)],
    )
    assert result.status == "drifted"
    assert result.drift_cents == 7500


def test_a_small_difference_is_pending_transactions_not_a_fault():
    [result] = evaluate(
        [actual_account("Checking", balance_cents=100000)],
        [remote_account("Checking", balance_cents=99950)],
        balance_tolerance_cents=100,
    )
    assert result.status == "ok"


def test_an_account_simplefin_stops_returning_is_flagged_as_missing():
    [result] = evaluate([actual_account("Checking")], [remote_account("Savings")])
    assert result.status == "missing"
    assert "removed or revoked" in result.detail


def test_a_manual_account_is_never_treated_as_a_broken_connection():
    [result] = evaluate([actual_account("Cash", sync_source="", external_id="")])
    assert result.status == "not_linked"
    assert summarize([result])["degraded"] == 0


def test_a_long_silence_is_reported_even_when_balances_agree():
    [result] = evaluate(
        [actual_account("Checking", last_transaction_date=TODAY - datetime.timedelta(days=30))],
        [remote_account("Checking")],
        transaction_stale_days=4,
    )
    assert result.status == "no_transactions"
    assert result.days_since_transaction == 30


def test_an_account_with_no_history_at_all_is_reported():
    [result] = evaluate(
        [actual_account("Checking", last_transaction_date=None)], [remote_account("Checking")]
    )
    assert result.status == "no_transactions"


def test_the_most_serious_signal_wins():
    [result] = evaluate(
        [
            actual_account(
                "Checking",
                balance_cents=1,
                last_transaction_date=TODAY - datetime.timedelta(days=90),
            )
        ],
        [remote_account("Checking", balance_date=NOW - datetime.timedelta(days=9))],
        [{"code": "con.auth", "msg": "Expired", "conn_id": "conn-1"}],
    )
    assert result.status == "error"
    # Every signal is still retained for the drawer, not just the worst one.
    assert len(result.signals) >= 3


def test_closed_accounts_are_left_out_entirely():
    assert evaluate([actual_account("Old", closed=True)]) == []


def test_without_simplefin_only_actual_side_signals_are_used():
    [result] = evaluate([actual_account("Checking")], simplefin_configured=False)
    assert result.status == "ok"
    assert any("not configured" in signal for signal in result.signals)


def test_results_are_ordered_worst_first():
    results = evaluate(
        [actual_account("Good"), actual_account("Bad"), actual_account("Cash", sync_source="", external_id="")],
        [remote_account("Good"), remote_account("Bad", balance_cents=1)],
    )
    assert [item.account_name for item in results] == ["Bad", "Good", "Cash"]


def test_summary_counts_only_linked_accounts_as_degraded():
    results = evaluate(
        [actual_account("Bad"), actual_account("Cash", sync_source="", external_id="")],
        [remote_account("Bad", balance_cents=1)],
    )
    summary = summarize(results)
    assert summary["overall"] == "drifted"
    assert summary["total"] == 2
    assert summary["linked"] == 1
    assert summary["degraded"] == 1


def test_worst_status_helper():
    assert worst_status([]) == "unknown"
    assert worst_status(["ok", "stale", "error"]) == "error"
    assert worst_status(["ok", "not_linked"]) == "ok"
    assert worst_status(["not_linked"]) == "not_linked"


# ------------------------------------------------------- cleared vs uncleared


def test_the_bank_is_compared_against_actuals_cleared_balance():
    """Money waiting to clear is not a mismatch, it is just not posted yet."""
    [result] = evaluate(
        [actual_account("Checking", balance_cents=92500, cleared_balance_cents=100000)],
        [remote_account("Checking", balance_cents=100000)],
    )
    assert result.status == "ok"
    assert result.drift_cents == 0
    assert result.actual_balance_cents == 92500
    assert result.actual_cleared_balance_cents == 100000
    assert result.uncleared_balance_cents == -7500
    assert any("has not cleared the bank yet" in signal for signal in result.signals)


def test_a_real_mismatch_is_still_caught_behind_uncleared_money():
    [result] = evaluate(
        [actual_account("Checking", balance_cents=90000, cleared_balance_cents=97000)],
        [remote_account("Checking", balance_cents=100000)],
    )
    assert result.status == "drifted"
    assert result.drift_cents == -3000
    assert any("cleared balance" in signal for signal in result.signals)
    assert result.status_without_drift == "ok"
    assert all("cleared balance" not in signal for signal in result.signals_without_drift)


def test_without_a_cleared_figure_the_total_is_used():
    [result] = evaluate(
        [actual_account("Checking", balance_cents=100000, cleared_balance_cents=None)],
        [remote_account("Checking", balance_cents=100000)],
    )
    assert result.status == "ok"
    assert result.uncleared_balance_cents == 0


# ------------------------------------------------ generated transfer halves


def test_an_inferred_transfer_that_exactly_explains_the_gap_is_held_out():
    """The Wells Fargo -> Capital One incident, expressed in cents."""

    [result] = evaluate(
        [
            actual_account(
                "Capital One",
                balance_cents=-166490,
                cleared_balance_cents=-166490,
                unconfirmed_transfers=(
                    UnconfirmedTransferInfo(263508, TODAY - datetime.timedelta(days=1)),
                ),
            )
        ],
        [remote_account("Capital One", balance_cents=-429998)],
    )

    assert result.status == "ok"
    assert result.raw_drift_cents == 263508
    assert result.comparison_balance_cents == -429998
    assert result.drift_cents == 0
    assert result.transfer_adjusted is True
    assert "generated 2,635.08" in result.signals[0]
    assert "holding out a transfer" in result.detail


def test_an_unconfirmed_transfer_cannot_hide_an_unrelated_mismatch():
    [result] = evaluate(
        [
            actual_account(
                "Capital One",
                balance_cents=-166490,
                cleared_balance_cents=-166490,
                unconfirmed_transfers=(
                    UnconfirmedTransferInfo(263508, TODAY - datetime.timedelta(days=1)),
                ),
            )
        ],
        [remote_account("Capital One", balance_cents=-400000)],
    )

    assert result.status == "drifted"
    assert result.comparison_balance_cents == -166490
    assert result.drift_cents == 233510
    assert result.transfer_adjusted is False


def test_no_transfer_adjustment_is_needed_once_the_bank_balance_catches_up():
    [result] = evaluate(
        [
            actual_account(
                "Capital One",
                balance_cents=-166490,
                cleared_balance_cents=-166490,
                unconfirmed_transfers=(
                    UnconfirmedTransferInfo(263508, TODAY - datetime.timedelta(days=1)),
                ),
            )
        ],
        [remote_account("Capital One", balance_cents=-166490)],
    )

    assert result.status == "ok"
    assert result.comparison_balance_cents == -166490
    assert result.transfer_adjusted is False


def test_an_old_unmatched_transfer_cannot_explain_a_new_balance_gap():
    [result] = evaluate(
        [
            actual_account(
                "Capital One",
                balance_cents=-166490,
                cleared_balance_cents=-166490,
                unconfirmed_transfers=(
                    UnconfirmedTransferInfo(263508, TODAY - datetime.timedelta(days=90)),
                ),
            )
        ],
        [remote_account("Capital One", balance_cents=-429998)],
    )

    assert result.status == "drifted"
    assert result.transfer_adjusted is False


def test_two_recent_generated_transfers_can_jointly_explain_the_gap():
    [result] = evaluate(
        [
            actual_account(
                "Capital One",
                balance_cents=-166490,
                cleared_balance_cents=-166490,
                unconfirmed_transfers=(
                    UnconfirmedTransferInfo(200000, TODAY - datetime.timedelta(days=1)),
                    UnconfirmedTransferInfo(63508, TODAY - datetime.timedelta(days=2)),
                ),
            )
        ],
        [remote_account("Capital One", balance_cents=-429998)],
    )

    assert result.status == "ok"
    assert result.unconfirmed_transfer_cents == 263508
    assert result.unconfirmed_transfer_count == 2
    assert result.transfer_adjusted is True


# ------------------------------------------------------------------- muting


def test_an_unmonitored_account_never_raises_a_problem():
    [result] = evaluate(
        [actual_account("Old Savings", balance_cents=1, last_transaction_date=None)],
        [remote_account("Old Savings", balance_date=NOW - datetime.timedelta(days=200))],
        unmonitored_ids={"acct-old savings"},
    )
    assert result.status == "muted"
    assert result.monitored is False
    # The real reading is kept, so the drawer can still explain it.
    assert result.underlying_status in ("stale", "drifted", "no_transactions")
    assert result.signals


def test_muted_accounts_are_excluded_from_the_summary_totals():
    results = evaluate(
        [actual_account("Watched"), actual_account("Dormant", balance_cents=1)],
        [remote_account("Watched"), remote_account("Dormant", balance_cents=999999)],
        unmonitored_ids={"acct-dormant"},
    )
    summary = summarize(results)
    assert summary["overall"] == "ok"
    assert summary["degraded"] == 0
    assert summary["muted"] == 1
    assert summary["linked"] == 1


def test_a_muted_account_sorts_to_the_bottom():
    results = evaluate(
        [actual_account("Dormant", balance_cents=1), actual_account("Watched")],
        [remote_account("Dormant", balance_cents=999999), remote_account("Watched")],
        unmonitored_ids={"acct-dormant"},
    )
    assert [item.account_name for item in results] == ["Watched", "Dormant"]


def test_monitoring_the_account_again_restores_its_real_status():
    account = actual_account("Dormant", balance_cents=1)
    remote = remote_account("Dormant", balance_cents=999999)
    assert evaluate([account], [remote], unmonitored_ids={"acct-dormant"})[0].status == "muted"
    assert evaluate([account], [remote])[0].status == "drifted"


def test_the_alerting_decision_travels_with_every_account():
    """One authority for what counts as a problem, shared by every screen."""
    broken = evaluate(
        [actual_account("Broken", balance_cents=1)], [remote_account("Broken", balance_cents=999999)]
    )[0].as_dict()
    quiet = evaluate(
        [actual_account("Dormant", last_transaction_date=TODAY - datetime.timedelta(days=200))],
        [remote_account("Dormant")],
    )[0].as_dict()
    healthy = evaluate([actual_account("Fine")], [remote_account("Fine")])[0].as_dict()
    manual = evaluate([actual_account("Cash", sync_source="", external_id="")])[0].as_dict()
    dormant = evaluate(
        [actual_account("Muted", balance_cents=1)],
        [remote_account("Muted", balance_cents=999999)],
        unmonitored_ids={"acct-muted"},
    )[0].as_dict()

    assert broken["alerting"] is True
    # A long-quiet account is not broken; it is quiet. It must not raise the
    # alarm on the dashboard when the digest and the badge stay silent.
    assert quiet["status"] == "no_transactions"
    assert quiet["alerting"] is False
    assert healthy["alerting"] is False
    assert manual["alerting"] is False
    assert dormant["alerting"] is False


def test_alerting_matches_what_the_summary_counts_as_degraded():
    results = evaluate(
        [
            actual_account("Broken", balance_cents=1),
            actual_account("Dormant", last_transaction_date=TODAY - datetime.timedelta(days=200)),
            actual_account("Fine"),
        ],
        [
            remote_account("Broken", balance_cents=999999),
            remote_account("Dormant"),
            remote_account("Fine"),
        ],
    )
    alerting = sum(1 for item in results if item.as_dict()["alerting"])
    assert alerting == summarize(results)["degraded"] == 1
