"""Whether each bank connection is actually still working.

A SimpleFIN connection does not announce that it has broken. It simply stops
returning fresh data, and Actual keeps showing the last balance it saw as if
nothing were wrong. Clerk compares three independent signals -- what SimpleFIN
reports right now, how old that report is, and what Actual holds -- so a silent
failure shows up as a status change instead of as a slowly staler budget.
"""

from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

# Worst first: an account's overall status is the most serious signal it has,
# and this order is also the order accounts are listed in. A manual account is
# the least urgent thing on the page, so `not_linked` sorts below `ok`.
STATUS_ORDER = (
    "error",
    "missing",
    "stale",
    "drifted",
    "no_transactions",
    "unknown",
    "ok",
    "not_linked",
    "muted",
)
_SEVERITY = {status: index for index, status in enumerate(STATUS_ORDER)}
# Statuses that mean the connection itself needs attention, as opposed to
# accounts the user has deliberately left unlinked.
ALERTING_STATUSES = frozenset({"error", "missing", "stale", "drifted"})

STATUS_LABELS = {
    "ok": "Connected",
    "muted": "Not monitored",
    "error": "Connection error",
    "missing": "Not returned by SimpleFIN",
    "stale": "Stale data",
    "drifted": "Balance mismatch",
    "no_transactions": "No recent transactions",
    "not_linked": "Not linked to bank sync",
    "unknown": "Not checked yet",
}

# A dormant account can sit for months without a transaction and without that
# meaning anything is wrong, so silence has to be opt-out per account.
MUTED = "muted"


@dataclass(frozen=True)
class ActualAccountInfo:
    id: str
    name: str
    sync_source: str = ""
    external_id: str = ""
    bank_name: str = ""
    balance_cents: int = 0
    # What the bank has actually posted, which is the only figure comparable to
    # a bank's own balance. Defaults to the total for callers that do not
    # distinguish the two.
    cleared_balance_cents: int | None = None
    last_sync: datetime.datetime | None = None
    last_transaction_date: datetime.date | None = None
    off_budget: bool = False
    closed: bool = False

    @property
    def cleared_cents(self) -> int:
        return (
            self.balance_cents
            if self.cleared_balance_cents is None
            else self.cleared_balance_cents
        )

    @property
    def uncleared_cents(self) -> int:
        return self.balance_cents - self.cleared_cents


@dataclass(frozen=True)
class SimpleFinAccountInfo:
    id: str
    name: str
    org_name: str = ""
    connection_id: str = ""
    balance_cents: int = 0
    balance_date: datetime.datetime | None = None
    available_cents: int | None = None
    last_transaction_date: datetime.date | None = None
    currency: str = "USD"


@dataclass
class AccountHealth:
    account_id: str
    account_name: str
    status: str
    detail: str
    signals: list[str] = field(default_factory=list)
    institution: str = ""
    external_id: str = ""
    sync_source: str = ""
    actual_balance_cents: int = 0
    actual_cleared_balance_cents: int = 0
    uncleared_balance_cents: int = 0
    remote_balance_cents: int | None = None
    drift_cents: int | None = None
    monitored: bool = True
    # What the account would have been scored as if it were being monitored.
    underlying_status: str = ""
    balance_age_hours: float | None = None
    last_sync: str | None = None
    last_transaction_date: str | None = None
    days_since_transaction: int | None = None
    off_budget: bool = False

    def as_dict(self) -> dict[str, Any]:
        data = {key: getattr(self, key) for key in self.__dataclass_fields__}
        data["status_label"] = STATUS_LABELS.get(self.status, self.status)
        # Whether this account should raise the alarm anywhere. Decided here so
        # the dashboard, the sidebar badge, and the digest cannot disagree
        # about what counts as a problem.
        data["alerting"] = self.status in ALERTING_STATUSES
        return data


def _money(cents: int) -> str:
    whole, remainder = divmod(abs(int(cents)), 100)
    return f"{'-' if cents < 0 else ''}{whole:,}.{remainder:02d}"


def severity(status: str) -> int:
    return _SEVERITY.get(status, len(STATUS_ORDER))


def worst_status(statuses: Sequence[str]) -> str:
    if not statuses:
        return "unknown"
    return min(statuses, key=severity)


def evaluate_accounts(
    *,
    accounts: Sequence[ActualAccountInfo],
    remote_accounts: Sequence[SimpleFinAccountInfo] = (),
    errors: Sequence[dict[str, Any]] = (),
    now: datetime.datetime | None = None,
    simplefin_configured: bool = True,
    balance_stale_hours: int = 36,
    balance_tolerance_cents: int = 100,
    transaction_stale_days: int = 4,
    unmonitored_ids: set[str] | None = None,
) -> list[AccountHealth]:
    """Score every Actual account against what SimpleFIN reports right now.

    Accounts named in `unmonitored_ids` are still measured -- the numbers stay
    visible -- but report as `muted` so they never raise an alert or count as
    degraded. A legacy account that sees one transaction a year is not broken.
    """

    now = now or datetime.datetime.now(datetime.UTC)
    unmonitored_ids = unmonitored_ids or set()
    today = now.date()
    remote_by_id = {account.id: account for account in remote_accounts}
    errors_by_account, errors_by_connection, general_errors = _index_errors(errors)

    results: list[AccountHealth] = []
    for account in accounts:
        if account.closed:
            continue
        health = _evaluate_one(
            account,
            remote_by_id.get(account.external_id) if account.external_id else None,
            errors_by_account=errors_by_account,
            errors_by_connection=errors_by_connection,
            general_errors=general_errors,
            now=now,
            today=today,
            simplefin_configured=simplefin_configured,
            have_remote_payload=bool(remote_accounts) or bool(errors),
            balance_stale_hours=balance_stale_hours,
            balance_tolerance_cents=balance_tolerance_cents,
            transaction_stale_days=transaction_stale_days,
        )
        if account.id in unmonitored_ids:
            health.monitored = False
            health.underlying_status = health.status
            health.status = MUTED
            health.detail = (
                "Monitoring is off for this account, so Clerk will not alert on it."
            )
        results.append(health)
    results.sort(key=lambda item: (severity(item.status), item.account_name.casefold()))
    return results


def _index_errors(
    errors: Sequence[dict[str, Any]],
) -> tuple[dict[str, list[str]], dict[str, list[str]], list[str]]:
    by_account: dict[str, list[str]] = {}
    by_connection: dict[str, list[str]] = {}
    general: list[str] = []
    for error in errors:
        message = str(error.get("message") or error.get("msg") or error.get("code") or "").strip()
        if not message:
            continue
        account_id = str(error.get("account_id") or "")
        connection_id = str(error.get("conn_id") or error.get("connection_id") or "")
        if account_id:
            by_account.setdefault(account_id, []).append(message)
        elif connection_id:
            by_connection.setdefault(connection_id, []).append(message)
        else:
            general.append(message)
    return by_account, by_connection, general


def _evaluate_one(
    account: ActualAccountInfo,
    remote: SimpleFinAccountInfo | None,
    *,
    errors_by_account: dict[str, list[str]],
    errors_by_connection: dict[str, list[str]],
    general_errors: list[str],
    now: datetime.datetime,
    today: datetime.date,
    simplefin_configured: bool,
    have_remote_payload: bool,
    balance_stale_hours: int,
    balance_tolerance_cents: int,
    transaction_stale_days: int,
) -> AccountHealth:
    signals: list[str] = []
    statuses: list[str] = []
    days_since = (
        (today - account.last_transaction_date).days if account.last_transaction_date else None
    )

    health = AccountHealth(
        account_id=account.id,
        account_name=account.name,
        status="unknown",
        detail="",
        institution=remote.org_name if remote else account.bank_name,
        external_id=account.external_id,
        sync_source=account.sync_source,
        actual_balance_cents=account.balance_cents,
        actual_cleared_balance_cents=account.cleared_cents,
        uncleared_balance_cents=account.uncleared_cents,
        remote_balance_cents=remote.balance_cents if remote else None,
        last_sync=account.last_sync.isoformat() if account.last_sync else None,
        last_transaction_date=(
            account.last_transaction_date.isoformat() if account.last_transaction_date else None
        ),
        days_since_transaction=days_since,
        off_budget=account.off_budget,
    )

    if not account.sync_source or not account.external_id:
        health.status = "not_linked"
        health.detail = "This account is not connected to bank sync, so Clerk cannot verify it."
        health.signals = ["Manual account"]
        return health

    account_errors = errors_by_account.get(account.external_id, [])
    connection_errors = (
        errors_by_connection.get(remote.connection_id, []) if remote else []
    )
    reported = account_errors + connection_errors
    if reported:
        statuses.append("error")
        signals.extend(reported)
    elif general_errors and not remote:
        statuses.append("error")
        signals.extend(general_errors)

    if not simplefin_configured:
        signals.append("SimpleFIN is not configured in Clerk, so only Actual's own data is checked.")
    elif remote is None and have_remote_payload and not reported:
        statuses.append("missing")
        signals.append(
            "SimpleFIN did not return this account. The bank link was most likely removed or revoked."
        )

    if remote is not None:
        if remote.balance_date is not None:
            age_hours = max(0.0, (now - remote.balance_date).total_seconds() / 3600)
            health.balance_age_hours = round(age_hours, 1)
            if age_hours > balance_stale_hours:
                statuses.append("stale")
                signals.append(
                    f"SimpleFIN's balance is {age_hours / 24:.1f} days old; the bank has stopped "
                    "refreshing this account."
                )
        # Compare like with like: the bank reports what it has posted, so the
        # cleared side of Actual is the only fair comparison. Anything still
        # waiting to clear is normal and is reported separately rather than
        # counted as a fault.
        drift = account.cleared_cents - remote.balance_cents
        health.drift_cents = drift
        if account.uncleared_cents:
            signals.append(
                f"{_money(abs(account.uncleared_cents))} of this account's balance has not "
                "cleared the bank yet. Clerk counts it as spent for the budget and leaves it "
                "out of this comparison."
            )
        if abs(drift) > balance_tolerance_cents:
            statuses.append("drifted")
            signals.append(
                "Actual's cleared balance and the bank's balance disagree, so a posted "
                "transaction is missing on one side."
            )

    if days_since is not None and days_since > transaction_stale_days:
        statuses.append("no_transactions")
        signals.append(f"No transaction has arrived in Actual for {days_since} days.")
    elif account.last_transaction_date is None:
        statuses.append("no_transactions")
        signals.append("Actual holds no transactions for this account yet.")

    health.status = worst_status(statuses) if statuses else "ok"
    health.signals = signals
    health.detail = signals[0] if signals else "Balances agree and data is current."
    return health


def summarize(results: Sequence[AccountHealth]) -> dict[str, Any]:
    linked = [item for item in results if item.status not in ("not_linked", MUTED)]
    counts: dict[str, int] = {}
    for item in results:
        counts[item.status] = counts.get(item.status, 0) + 1
    return {
        "overall": worst_status([item.status for item in linked]) if linked else "unknown",
        "counts": counts,
        "total": len(results),
        "linked": len(linked),
        "muted": sum(1 for item in results if item.status == MUTED),
        "degraded": sum(1 for item in linked if item.status in ALERTING_STATUSES),
    }
