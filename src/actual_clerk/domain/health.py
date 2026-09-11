"""Whether each bank connection is actually still working.

A bank connection does not announce that it has broken. It simply stops
returning fresh data, and Actual keeps showing the last balance it saw as if
nothing were wrong. Clerk compares three independent signals -- what the
provider reports right now, how old that report is, and what Actual holds -- so
a silent failure shows up as a status change instead of as a slowly staler
budget.

The provider is a property of the account, not of Clerk. An account Actual
links itself carries Actual's sync source (SimpleFIN); an account whose feed
Clerk delivers carries Clerk's (Plaid). Every remote reading and every error
names its provider, and an account is only ever compared with readings from
its own.
"""

from __future__ import annotations

import datetime
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

# Actual's own sync-source keys, plus the one Clerk adds.
SIMPLEFIN = "simpleFin"
PLAID = "plaid"
PROVIDER_LABELS = {
    SIMPLEFIN: "SimpleFIN",
    PLAID: "Plaid",
    "goCardless": "GoCardless",
    "pluggyai": "Pluggy.ai",
    "akahu": "Akahu",
    "enableBanking": "Enable Banking",
}


def provider_label(provider: str) -> str:
    return PROVIDER_LABELS.get(provider or "", provider or "the bank provider")

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
    "missing": "Not returned by the bank",
    "stale": "Stale data",
    "drifted": "Balance mismatch",
    "no_transactions": "No recent transactions",
    "not_linked": "Not linked to bank sync",
    "unknown": "Not checked yet",
}

# A dormant account can sit for months without a transaction and without that
# meaning anything is wrong, so silence has to be opt-out per account.
MUTED = "muted"


# A card charge posts within a few days. An imported row still uncleared well
# after that is usually one the bank dropped after Actual had already imported
# it: Actual's import is additive and never removes a transaction the bank
# stopped reporting, and the bank comparison deliberately looks only at cleared
# money, so nothing else Clerk checks would ever surface it. It keeps counting
# as spent in the meantime. Long enough to sit clear of any real pending window.
STUCK_IMPORT_DAYS = 14


@dataclass(frozen=True)
class UnconfirmedTransferInfo:
    amount_cents: int
    date: datetime.date
    transaction_id: str = ""


@dataclass(frozen=True)
class StuckImportInfo:
    """An imported transaction Actual still holds as uncleared."""

    amount_cents: int
    date: datetime.date
    transaction_id: str = ""


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
    # Cleared activity that Actual generated as the other half of an imported
    # transfer, before this account imported its own matching row.
    unconfirmed_transfers: tuple[UnconfirmedTransferInfo, ...] = ()
    # Every imported row this account still holds as uncleared, whatever its
    # age. Which of them count as stuck is decided during evaluation.
    uncleared_imports: tuple[StuckImportInfo, ...] = ()
    last_sync: datetime.datetime | None = None
    last_transaction_date: datetime.date | None = None
    off_budget: bool = False
    closed: bool = False
    # True when Clerk, not Actual, delivers this account's bank feed. The
    # sync_source then names Clerk's provider rather than Actual's.
    managed_by_clerk: bool = False

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
class RemoteAccountInfo:
    """One provider's current reading of one account."""

    id: str
    name: str
    org_name: str = ""
    connection_id: str = ""
    balance_cents: int = 0
    balance_date: datetime.datetime | None = None
    available_cents: int | None = None
    last_transaction_date: datetime.date | None = None
    currency: str = "USD"
    provider: str = SIMPLEFIN


# The original name, kept for callers and tests written against SimpleFIN.
SimpleFinAccountInfo = RemoteAccountInfo


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
    provider_label: str = ""
    managed_by_clerk: bool = False
    actual_balance_cents: int = 0
    actual_cleared_balance_cents: int = 0
    uncleared_balance_cents: int = 0
    unconfirmed_transfer_cents: int = 0
    unconfirmed_transfer_count: int = 0
    stuck_import_cents: int = 0
    stuck_import_count: int = 0
    oldest_stuck_import_date: str | None = None
    comparison_balance_cents: int = 0
    raw_drift_cents: int | None = None
    transfer_adjusted: bool = False
    remote_balance_cents: int | None = None
    drift_cents: int | None = None
    monitored: bool = True
    # What the account would have been scored as if it were being monitored.
    underlying_status: str = ""
    balance_age_hours: float | None = None
    remote_balance_date: str | None = None
    last_sync: str | None = None
    last_transaction_date: str | None = None
    days_since_transaction: int | None = None
    off_budget: bool = False
    # A balance mismatch is confirmed over several health checks before it is
    # allowed to become the account's public status. These fields preserve the
    # fair status and explanation to use while that confirmation is pending.
    status_without_drift: str = "ok"
    detail_without_drift: str = "Balances agree and data is current."
    signals_without_drift: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        data = {key: getattr(self, key) for key in self.__dataclass_fields__}
        data["status_label"] = STATUS_LABELS.get(self.status, self.status)
        # Whether this account should raise the alarm anywhere. Decided here so
        # the dashboard, the sidebar badge, and the digest cannot disagree
        # about what counts as a problem.
        data["alerting"] = self.status in ALERTING_STATUSES
        data["status_without_drift_label"] = STATUS_LABELS.get(
            self.status_without_drift, self.status_without_drift
        )
        data["alerting_without_drift"] = self.status_without_drift in ALERTING_STATUSES
        return data


def _money(cents: int) -> str:
    whole, remainder = divmod(abs(int(cents)), 100)
    return f"{'-' if cents < 0 else ''}{whole:,}.{remainder:02d}"


def severity(status: str) -> int:
    return _SEVERITY.get(status, len(STATUS_ORDER))


def _transfer_adjustment(
    transfers: Sequence[UnconfirmedTransferInfo],
    *,
    drift_cents: int,
    today: datetime.date,
    tolerance_cents: int,
) -> tuple[int, int]:
    """Find a small recent subset that fully explains a bank balance gap.

    Old generated payment rows can remain without destination-side import ids,
    so a lifetime aggregate is not safe. Bank-feed races are recent; search at
    most twelve candidates from the last fourteen days and prefer the smallest
    explaining set. The bound keeps corrupt or unusually busy ledgers cheap.
    """

    cutoff = today - datetime.timedelta(days=14)
    recent = sorted(
        (item for item in transfers if cutoff <= item.date <= today),
        key=lambda item: item.date,
        reverse=True,
    )[:12]
    for size in range(1, len(recent) + 1):
        for selected in combinations(recent, size):
            amount = sum(item.amount_cents for item in selected)
            if abs(drift_cents - amount) <= tolerance_cents:
                return amount, size
    return 0, 0


def worst_status(statuses: Sequence[str]) -> str:
    if not statuses:
        return "unknown"
    return min(statuses, key=severity)


def evaluate_accounts(
    *,
    accounts: Sequence[ActualAccountInfo],
    remote_accounts: Sequence[RemoteAccountInfo] = (),
    errors: Sequence[dict[str, Any]] = (),
    now: datetime.datetime | None = None,
    simplefin_configured: bool = True,
    providers: Mapping[str, bool] | None = None,
    balance_stale_hours: int = 36,
    balance_tolerance_cents: int = 100,
    transaction_stale_days: int = 4,
    stuck_import_days: int = STUCK_IMPORT_DAYS,
    unmonitored_ids: set[str] | None = None,
) -> list[AccountHealth]:
    """Score every Actual account against what its provider reports right now.

    `providers` says which providers Clerk can ask directly (provider key to
    configured flag); `simplefin_configured` is the older spelling of the
    SimpleFIN entry. A provider that returned any reading or error is treated
    as configured whatever the flags say. Readings and errors carry a
    `provider`, and an account is only compared with its own provider's.

    Accounts named in `unmonitored_ids` are still measured -- the numbers stay
    visible -- but report as `muted` so they never raise an alert or count as
    degraded. A legacy account that sees one transaction a year is not broken.
    """

    now = now or datetime.datetime.now(datetime.UTC)
    unmonitored_ids = unmonitored_ids or set()
    today = now.date()
    configured = {SIMPLEFIN: simplefin_configured, **(providers or {})}
    remote_by_key = {(account.provider, account.id): account for account in remote_accounts}
    errors_by_account, errors_by_connection, general_errors = _index_errors(errors)
    providers_with_payload = {account.provider for account in remote_accounts} | {
        _error_provider(error) for error in errors
    }
    for provider in providers_with_payload:
        configured.setdefault(provider, True)

    results: list[AccountHealth] = []
    for account in accounts:
        if account.closed:
            continue
        provider = account.sync_source
        health = _evaluate_one(
            account,
            remote_by_key.get((provider, account.external_id)) if account.external_id else None,
            errors_by_account=errors_by_account,
            errors_by_connection=errors_by_connection,
            general_errors=general_errors,
            now=now,
            today=today,
            provider_configured=bool(configured.get(provider, False)),
            have_remote_payload=provider in providers_with_payload,
            balance_stale_hours=balance_stale_hours,
            balance_tolerance_cents=balance_tolerance_cents,
            transaction_stale_days=transaction_stale_days,
            stuck_import_days=stuck_import_days,
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


def _error_provider(error: dict[str, Any]) -> str:
    return str(error.get("provider") or SIMPLEFIN)


ErrorKey = tuple[str, str]


def _index_errors(
    errors: Sequence[dict[str, Any]],
) -> tuple[dict[ErrorKey, list[str]], dict[ErrorKey, list[str]], dict[str, list[str]]]:
    """Group reported problems by (provider, account), (provider, connection), and provider."""

    by_account: dict[ErrorKey, list[str]] = {}
    by_connection: dict[ErrorKey, list[str]] = {}
    general: dict[str, list[str]] = {}
    for error in errors:
        message = str(error.get("message") or error.get("msg") or error.get("code") or "").strip()
        if not message:
            continue
        provider = _error_provider(error)
        account_id = str(error.get("account_id") or "")
        connection_id = str(error.get("conn_id") or error.get("connection_id") or "")
        if account_id:
            by_account.setdefault((provider, account_id), []).append(message)
        elif connection_id:
            by_connection.setdefault((provider, connection_id), []).append(message)
        else:
            general.setdefault(provider, []).append(message)
    return by_account, by_connection, general


def _evaluate_one(
    account: ActualAccountInfo,
    remote: RemoteAccountInfo | None,
    *,
    errors_by_account: dict[ErrorKey, list[str]],
    errors_by_connection: dict[ErrorKey, list[str]],
    general_errors: dict[str, list[str]],
    now: datetime.datetime,
    today: datetime.date,
    provider_configured: bool,
    have_remote_payload: bool,
    balance_stale_hours: int,
    balance_tolerance_cents: int,
    transaction_stale_days: int,
    stuck_import_days: int,
) -> AccountHealth:
    signals: list[str] = []
    signals_without_drift: list[str] = []
    statuses: list[str] = []
    status_details: dict[str, str] = {}

    def flag(status: str, message: str) -> None:
        statuses.append(status)
        signals.append(message)
        status_details.setdefault(status, message)
        if status != "drifted":
            signals_without_drift.append(message)

    def note(message: str) -> None:
        signals.append(message)
        signals_without_drift.append(message)

    days_since = (
        (today - account.last_transaction_date).days if account.last_transaction_date else None
    )
    provider = account.sync_source
    label = provider_label(provider)

    health = AccountHealth(
        account_id=account.id,
        account_name=account.name,
        status="unknown",
        detail="",
        institution=remote.org_name if remote else account.bank_name,
        external_id=account.external_id,
        sync_source=provider,
        provider_label=label if provider else "",
        managed_by_clerk=account.managed_by_clerk,
        actual_balance_cents=account.balance_cents,
        actual_cleared_balance_cents=account.cleared_cents,
        uncleared_balance_cents=account.uncleared_cents,
        comparison_balance_cents=account.cleared_cents,
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

    account_errors = errors_by_account.get((provider, account.external_id), [])
    connection_errors = (
        errors_by_connection.get((provider, remote.connection_id), []) if remote else []
    )
    reported = account_errors + connection_errors
    if reported:
        for message in reported:
            flag("error", message)
    elif general_errors.get(provider) and not remote:
        for message in general_errors[provider]:
            flag("error", message)

    if not provider_configured:
        note(f"{label} is not configured in Clerk, so only Actual's own data is checked.")
    elif remote is None and have_remote_payload and not reported:
        flag(
            "missing",
            f"{label} did not return this account. The bank link was most likely removed or revoked."
        )

    if remote is not None:
        if remote.balance_date is not None:
            health.remote_balance_date = remote.balance_date.isoformat()
            age_hours = max(0.0, (now - remote.balance_date).total_seconds() / 3600)
            health.balance_age_hours = round(age_hours, 1)
            if age_hours > balance_stale_hours:
                flag(
                    "stale",
                    f"{label}'s balance is {age_hours / 24:.1f} days old; the bank has stopped "
                    "refreshing this account."
                )
        # Compare like with like: the bank reports what it has posted, so the
        # cleared side of Actual is the only fair comparison. Anything still
        # waiting to clear is normal and is reported separately rather than
        # counted as a fault.
        raw_drift = account.cleared_cents - remote.balance_cents
        comparison_balance = account.cleared_cents
        transfer_adjustment, transfer_count = _transfer_adjustment(
            account.unconfirmed_transfers,
            drift_cents=raw_drift,
            today=today,
            tolerance_cents=balance_tolerance_cents,
        )
        adjusted_balance = account.cleared_cents - transfer_adjustment
        # Do not broadly distrust transfers or Actual's cleared flag. Apply the
        # provenance adjustment only when it explains the entire mismatch: the
        # linked source was imported from its bank, this account's generated
        # half was not, and the provider agrees with the ledger without that half.
        if (
            transfer_count > 0
            and abs(raw_drift) > balance_tolerance_cents
            and abs(adjusted_balance - remote.balance_cents) <= balance_tolerance_cents
        ):
            comparison_balance = adjusted_balance
            health.transfer_adjusted = True
            health.unconfirmed_transfer_cents = transfer_adjustment
            health.unconfirmed_transfer_count = transfer_count
            note(
                f"Actual generated {_money(abs(transfer_adjustment))} of "
                "cleared transfer activity from another account before this account imported "
                "its own side. Clerk leaves that inferred amount out of the bank comparison."
            )
        health.comparison_balance_cents = comparison_balance
        health.raw_drift_cents = raw_drift
        drift = comparison_balance - remote.balance_cents
        health.drift_cents = drift
        if account.uncleared_cents:
            note(
                f"{_money(abs(account.uncleared_cents))} of this account's balance has not "
                "cleared the bank yet. Clerk counts it as spent for the budget and leaves it "
                "out of this comparison."
            )
        if abs(drift) > balance_tolerance_cents:
            flag(
                "drifted",
                "Actual's cleared balance and the bank's balance disagree, so a posted "
                "transaction is missing on one side."
            )

    if days_since is not None and days_since > transaction_stale_days:
        flag("no_transactions", f"No transaction has arrived in Actual for {days_since} days.")
    elif account.last_transaction_date is None:
        flag("no_transactions", "Actual holds no transactions for this account yet.")

    stuck = [
        item
        for item in account.uncleared_imports
        if (today - item.date).days > stuck_import_days
    ]
    stuck_detail = ""
    if stuck:
        oldest = min(item.date for item in stuck)
        total = sum(item.amount_cents for item in stuck)
        health.stuck_import_cents = total
        health.stuck_import_count = len(stuck)
        health.oldest_stuck_import_date = oldest.isoformat()
        stuck_detail = (
            f"{_money(abs(total))} across {len(stuck)} imported "
            f"transaction{'' if len(stuck) == 1 else 's'} has sat uncleared since "
            f"{oldest.isoformat()}. Actual never withdraws a transaction the bank stopped "
            "reporting, so a dropped charge stays here and keeps counting as spent. Worth "
            "checking against the bank."
        )
        note(stuck_detail)

    healthy_detail = (
        "The bank balance agrees after holding out a transfer Actual generated from another "
        "account but this account has not imported yet."
        if health.transfer_adjusted
        else "Balances agree and data is current."
    )
    # Never a status of its own: the connection is working, and a status would
    # alert. Said on the row instead, so a healthy account still shows it.
    if stuck_detail:
        healthy_detail = stuck_detail
    health.status = worst_status(statuses) if statuses else "ok"
    health.signals = signals
    health.detail = status_details.get(health.status, healthy_detail)
    without_drift = [status for status in statuses if status != "drifted"]
    health.status_without_drift = worst_status(without_drift) if without_drift else "ok"
    health.detail_without_drift = status_details.get(
        health.status_without_drift, healthy_detail
    )
    health.signals_without_drift = signals_without_drift
    return health


def summarize(results: Sequence[AccountHealth | dict[str, Any]]) -> dict[str, Any]:
    """Summarize either freshly evaluated objects or stabilized snapshots."""

    def status_of(item: AccountHealth | dict[str, Any]) -> str:
        return item.status if isinstance(item, AccountHealth) else str(item.get("status") or "unknown")

    statuses = [status_of(item) for item in results]
    linked = [status for status in statuses if status not in ("not_linked", MUTED)]
    counts: dict[str, int] = {}
    for status in statuses:
        counts[status] = counts.get(status, 0) + 1
    return {
        "overall": worst_status(linked) if linked else "unknown",
        "counts": counts,
        "total": len(results),
        "linked": len(linked),
        "muted": sum(1 for status in statuses if status == MUTED),
        "degraded": sum(1 for status in linked if status in ALERTING_STATUSES),
    }
