"""A single self-contained report explaining every number Clerk shows.

The Overview is built from a stored snapshot, and the figures inside it pass
through several independent decisions -- which budget table Actual keeps, which
categories count as committed, which income basis wins -- before they reach the
screen. When the result disagrees with Actual, the useful question is *which*
of those decisions differs, and that is not visible from the dashboard.

This module answers that question in one pass, as plain text meant to be copied
out of the browser. It reads; it never writes. Names can be replaced with stable
pseudonyms so the report can be shared without disclosing the budget.
"""

from __future__ import annotations

import datetime
import os
from typing import Any

from actual_clerk import __version__
from actual_clerk.config import Settings, tz_database_available
from actual_clerk.domain.budget import build_budget_report, month_bounds
from actual_clerk.processing import DIGEST_WINDOW_HOURS
from actual_clerk.reporting import to_category_infos, to_transaction_infos

WIDTH = 78
# Tracking budgets keep their numbers in reflect_budgets; envelope budgets in
# zero_budgets. The official API selects between them from this preference.
TRACKING_VALUES = ("report", "tracking")


class Redactor:
    """Stable pseudonyms, so a shared report still reads consistently."""

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self._seen: dict[tuple[str, str], str] = {}
        self._counts: dict[str, int] = {}

    def __call__(self, kind: str, name: str | None) -> str:
        text = (name or "").strip()
        if not self.enabled or not text:
            return text or "(unnamed)"
        key = (kind, text.casefold())
        if key not in self._seen:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            self._seen[key] = f"{kind} {self._counts[kind]}"
        return self._seen[key]


def money(cents: int | float | None, currency: str = "") -> str:
    if cents is None:
        return "        --"
    suffix = f" {currency}" if currency else ""
    return f"{cents / 100:>12,.2f}{suffix}"


class _Report:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.findings: list[str] = []

    def head(self, title: str) -> None:
        self.lines.append("")
        self.lines.append("=" * WIDTH)
        self.lines.append(title.upper())
        self.lines.append("=" * WIDTH)

    def sub(self, title: str) -> None:
        self.lines.append("")
        self.lines.append(f"-- {title} " + "-" * max(0, WIDTH - len(title) - 4))

    def row(self, label: str, value: Any) -> None:
        self.lines.append(f"  {label:<34}: {value}")

    def text(self, value: str = "") -> None:
        self.lines.append(value)

    def finding(self, severity: str, message: str) -> None:
        """Something that explains, or could explain, a wrong number."""
        self.findings.append(f"[{severity}] {message}")
        self.lines.append(f"  >> [{severity}] {message}")

    def render(self) -> str:
        header = [
            "=" * WIDTH,
            "ACTUAL CLERK DIAGNOSTIC".center(WIDTH),
            "=" * WIDTH,
        ]
        if self.findings:
            summary = ["", f"{len(self.findings)} finding(s):"]
            summary += [f"  {item}" for item in self.findings]
        else:
            summary = ["", "No findings: every check below agreed."]
        return "\n".join(header + summary + self.lines) + "\n"


def _local_clock(timestamp: Any, zone: datetime.tzinfo) -> str:
    """A stored epoch as a local wall clock, or "" if there is none."""
    if not timestamp:
        return ""
    moment = datetime.datetime.fromtimestamp(float(timestamp), datetime.UTC).astimezone(zone)
    return f"{moment:%H:%M} {moment.tzname()}"


def _digest_section(
    r: _Report,
    *,
    settings: Settings,
    digests: list[dict[str, Any]],
    digest_jobs: list[dict[str, Any]],
    timezone_chosen: bool,
    hide: Redactor,
) -> None:
    """Why the morning digest did or did not go out.

    Every gate the scheduler passes through, in the order it checks them, with
    the two clocks side by side. "It did not fire" is almost always one of:
    a zone that did not resolve, a window measured against the wrong clock, a
    claim already taken for the day, or a job that failed after being queued.
    """

    r.head("2b. morning digest schedule")

    zone_ok = True
    try:
        zone = settings.zone
    except Exception as exc:  # noqa: BLE001 - a diagnostic must never fail to render
        zone_ok = False
        zone = datetime.UTC
        r.finding("ERROR", f"The configured time zone could not be loaded: {exc}")

    utc_now = datetime.datetime.now(datetime.UTC)
    local_now = utc_now.astimezone(zone)
    offset = local_now.utcoffset() or datetime.timedelta(0)
    scheduled = settings.digest_clock
    start = datetime.datetime.combine(local_now.date(), scheduled, tzinfo=zone)
    close = start + datetime.timedelta(hours=DIGEST_WINDOW_HOURS)
    due = start <= local_now < close
    today = local_now.date().isoformat()
    today_row = next(
        (
            row
            for row in digests
            if row.get("local_date") == today
            and row.get("scheduled_for") == settings.digest_time
        ),
        None,
    )
    today_claimed = today_row is not None

    r.sub("the gates the scheduler checks, in order")
    r.row("1. Digest enabled", settings.digest_enabled)
    r.row("2. Notifications enabled", settings.notifications_enabled)
    r.row("3. Inside today's window", f"{due}  ({start:%H:%M} .. {close:%H:%M} local)")
    r.row(
        "4. This delivery not yet claimed",
        f"{not today_claimed}  (one per date and time, so {today} {settings.digest_time})",
    )
    r.text(
        "    All four must hold on the same 20-second tick for a digest to be queued."
    )

    r.sub("clocks")
    r.row("Digest time (local)", settings.digest_time)
    r.row("Time zone setting", settings.timezone)
    if zone_ok:
        sign = "+" if offset >= datetime.timedelta(0) else "-"
        hours, minutes = divmod(abs(int(offset.total_seconds())) // 60, 60)
        r.row("Resolves to", f"{local_now.tzname()}  UTC{sign}{hours:02d}:{minutes:02d}")
    else:
        r.row("Resolves to", "(failed to load)")
    r.row("Local time now", local_now.isoformat(timespec="seconds"))
    r.row("UTC time now", utc_now.isoformat(timespec="seconds"))
    r.row(
        "Digest time in UTC today",
        f"{start.astimezone(datetime.UTC):%H:%M} UTC"
        + ("   (the same clock: this container runs on UTC)" if not offset else ""),
    )
    upcoming = start if local_now < start else start + datetime.timedelta(days=1)
    remaining = upcoming - local_now
    ahead = f"in {int(remaining.total_seconds()) // 3600}h "
    ahead += f"{int(remaining.total_seconds()) % 3600 // 60}m"
    if not due:
        r.row("Next window opens", f"{upcoming.isoformat(timespec='minutes')}   ({ahead})")

    # A day is spent once it is claimed, so a digest time moved to later the
    # same day cannot fire until tomorrow. That is by design and invisible
    # from the outside, which makes it the likeliest reason a digest people
    # are actively waiting on does not arrive.
    if today_claimed and settings.digest_enabled and settings.notifications_enabled and due:
        went_out = _local_clock(today_row.get("created_at"), zone)
        state = "went out" if today_row.get("delivered") else "was claimed but not delivered"
        r.finding(
            "NOTE",
            f"The digest for {today} {today_row.get('scheduled_for')} "
            f"{state}{f' at {went_out}' if went_out else ''}, so this delivery is already "
            f"spent. The next is {upcoming.isoformat(timespec='minutes')} ({ahead}); "
            "moving the digest time asks for a fresh one at the new time.",
        )

    r.sub("time zone database")
    available = tz_database_available()
    r.row("IANA zones resolvable", "yes" if available else "NO")
    if not available:
        r.finding(
            "ERROR",
            "This container has no time zone database, so every IANA name is "
            "rejected and the digest can only keep UTC time. Install the tzdata "
            "package (Clerk declares it as a dependency; a hand-built image may "
            "have dropped it).",
        )

    r.sub("where the time zone came from")
    environment_tz = os.environ.get("TZ", "")
    r.row("TZ in the environment", environment_tz or "(not set)")
    r.row("CLERK_TIMEZONE in the environment", os.environ.get("CLERK_TIMEZONE") or "(not set)")
    r.row("Chosen in the interface", timezone_chosen)
    if environment_tz and environment_tz != settings.timezone:
        if os.environ.get("CLERK_TIMEZONE"):
            r.text("    CLERK_TIMEZONE outranks TZ, which is why TZ is not in effect.")
        elif timezone_chosen:
            r.finding(
                "WARN",
                f"TZ is {environment_tz} but the time zone is {settings.timezone}, chosen in "
                "the interface. A choice made in the interface outranks TZ; clear it there "
                "if you want the container's TZ back.",
            )
        else:
            r.finding(
                "ERROR",
                f"TZ is {environment_tz} but Clerk is running on {settings.timezone}. "
                "TZ should have been applied and was not.",
            )
    # An ntfy topic without notifications means they were set up and then went
    # off, which is worth saying. No topic at all just means this install does
    # not use notifications, and a fresh one should not be scolded for it.
    if settings.digest_enabled and not settings.notifications_enabled and settings.ntfy_topic:
        r.finding(
            "WARN",
            "The digest is enabled but notifications are off, so nothing is ever "
            "queued or delivered. Both switches are required.",
        )

    r.sub("recent digests")
    if not digests:
        r.text("    (Clerk has never claimed a digest date)")
        if settings.digest_enabled and settings.notifications_enabled:
            r.finding(
                "WARN",
                "No digest has ever been claimed, so the scheduler has not once "
                "found all four gates open at the same time.",
            )
    for row in digests:
        state = "delivered" if row.get("delivered") else "NOT DELIVERED"
        # The hour is the point: a digest that arrived at 03:30 rather than
        # 07:30 was delivered on a clock nobody was reading.
        clock = _local_clock(row.get("created_at"), zone) or "(no time)"
        slot = row.get("scheduled_for") or "(no time)"
        r.text(f"    {row['local_date']} {slot:<10} sent {clock:<12} {state}")
        receipt = row.get("receipt") or {}
        if receipt.get("topic"):
            # The one fact "delivered" cannot carry: which topic accepted it.
            r.text(
                f"       -> {receipt.get('server') or '?'} topic "
                f"{hide('Topic', receipt['topic'])!r} message id "
                f"{receipt.get('id') or '(none returned)'}"
            )
        elif row.get("delivered"):
            r.text("       -> (delivered before Clerk began recording where)")
        if row.get("error"):
            r.text(f"       -> {str(row['error'])[:64]}")

    r.sub("recent digest jobs")
    if not digest_jobs:
        r.text("    (no digest job has ever been queued)")
    for job in digest_jobs:
        r.text(
            f"    {str(job.get('status')):<12} {str(job.get('trigger')):<10} "
            f"{str(job.get('completed_at') or job.get('created_at') or '')[:19]:<20}"
            f" {str(job.get('error_message') or '')[:40]}"
        )


def build_report(
    *,
    settings: Settings,
    today: datetime.date,
    snapshot: dict[str, Any] | None,
    stored_overview: dict[str, Any] | None,
    probe: dict[str, Any] | None,
    gateway_status: dict[str, Any],
    jobs: list[dict[str, Any]],
    last_sync: dict[str, Any] | None,
    health: list[dict[str, Any]],
    unmonitored: set[str],
    counts: dict[str, int],
    digests: list[dict[str, Any]] | None = None,
    digest_jobs: list[dict[str, Any]] | None = None,
    timezone_chosen: bool = False,
    snapshot_error: str = "",
    redact: bool = False,
) -> str:
    r = _Report()
    hide = Redactor(redact)
    cur = settings.budget_currency
    start, end = month_bounds(today)

    # ---------------------------------------------------------------- 1
    r.head("1. environment")
    r.row("Clerk version", __version__)
    r.row("Report generated", datetime.datetime.now(settings.zone).isoformat(timespec="seconds"))
    r.row("Budget 'today'", f"{today.isoformat()}  (month {start} .. {end})")
    r.row("Time zone", settings.timezone)
    r.row("Names redacted", "yes" if redact else "no")
    r.row("Gateway connected", gateway_status.get("connected"))
    if gateway_status.get("last_error"):
        r.finding("ERROR", f"Actual gateway last error: {gateway_status['last_error']}")

    # ---------------------------------------------------------------- 2
    r.head("2. snapshot freshness")
    stored_budget = (stored_overview or {}).get("budget") or {}
    updated = (stored_overview or {}).get("snapshot_updated_at")
    if updated:
        age = datetime.datetime.now(datetime.UTC).timestamp() - float(updated)
        r.row("Stored snapshot age", f"{age / 60:.1f} minutes")
        r.row(
            "Stored snapshot written",
            datetime.datetime.fromtimestamp(float(updated), settings.zone).isoformat(
                timespec="seconds"
            ),
        )
        if age > 6 * 3600:
            r.finding(
                "WARN",
                f"The Overview you are looking at was built {age / 3600:.1f} hours ago. "
                "Edits made in Actual since then are not in it.",
            )
    else:
        r.finding("ERROR", "No stored overview snapshot exists; the Overview has nothing to show.")
    r.row("Stored report month", stored_budget.get("month", "(none)"))
    if stored_budget.get("month") and stored_budget["month"] != start.strftime("%Y-%m"):
        r.finding(
            "ERROR",
            f"The stored Overview is for {stored_budget['month']} but today is "
            f"{start.strftime('%Y-%m')}. It has not been rebuilt this month.",
        )
    r.row("Sync on a schedule", settings.sync_enabled)
    r.row("Sync interval", f"{settings.sync_interval_minutes} minutes")
    if last_sync:
        r.row("Last completed sync", last_sync.get("completed_at") or last_sync.get("started_at"))
    else:
        r.finding("WARN", "No sync job has ever completed.")
    r.sub("recent jobs")
    for job in jobs[:10]:
        r.text(
            f"    {str(job.get('kind')):<12} {str(job.get('status')):<10} "
            f"{str(job.get('completed_at') or job.get('started_at') or '')[:19]:<20}"
            f" {str(job.get('error_message') or '')[:60]}"
        )
    # ---------------------------------------------------------------- 2b
    # Placed before the live read is required: a digest that never fired is a
    # scheduling question, and it must stay answerable when Actual is down.
    _digest_section(
        r,
        settings=settings,
        digests=digests or [],
        digest_jobs=digest_jobs or [],
        timezone_chosen=timezone_chosen,
        hide=hide,
    )

    if snapshot is None:
        r.head("live read failed")
        r.finding("ERROR", f"Could not read Actual live: {snapshot_error}")
        r.text("Every section below needs a live read, so the report stops here.")
        return r.render()

    # ---------------------------------------------------------------- 3
    r.head("3. actual budget file")
    if probe:
        r.row("Budget name", hide("Budget", probe.get("budget_name")))
        r.row("budgetType preference", repr(probe.get("budget_type_preference")))
        r.row("Budget style", "TRACKING" if probe.get("is_tracking") else "ENVELOPE")
        r.row("Official API reads table", probe.get("reads_table"))
        if probe.get("api_version"):
            r.row("Actual API / server", f"{probe.get('api_version')} / {probe.get('server_version') or '(unknown)'}")
        merged = probe.get("redirects") or {}
        if merged:
            r.row(
                "Merged categories / payees",
                f"{merged.get('categories', 0)} / {merged.get('payees', 0)}  "
                "(resolved through Actual's redirect tables)",
            )
        r.sub("budget tables")
        for label, info in (probe.get("tables") or {}).items():
            marker = "  <-- read" if label == probe.get("reads_table") else ""
            r.text(
                f"    {label:<18} rows {info['rows']:>5}   non-zero {info['non_zero']:>5}"
                f"   total {money(info['total_cents'], cur)}{marker}"
            )
        tables = probe.get("tables") or {}
        read = tables.get(probe.get("reads_table"), {})
        other_label = "zero_budgets" if probe.get("is_tracking") else "reflect_budgets"
        other = tables.get(other_label, {})
        if read.get("non_zero", 0) == 0 and other.get("non_zero", 0) > 0:
            r.finding(
                "ERROR",
                f"Every budgeted amount lives in {other_label}, but the budgetType preference "
                f"({probe.get('budget_type_preference')!r}) makes Clerk read "
                f"{probe.get('reads_table')}, which is empty. Clerk sees a budget of zero "
                "everywhere. Set the budget type in Actual, or switch it and switch it back.",
            )
        elif read.get("non_zero", 0) == 0:
            r.finding("ERROR", "No non-zero budgeted amount exists in either budget table.")

    # ---------------------------------------------------------------- 4
    categories = to_category_infos(snapshot)
    known_ids = {category.id for category in categories}
    budgeted: dict[str, int] = snapshot["budgeted"]
    budgeted_history: dict[str, dict[str, int]] = snapshot.get("budgeted_history") or {}
    r.head("4. budget months clerk can see")
    r.row("Months with budget rows", len(budgeted_history))
    r.row("This month's key", start.strftime("%Y-%m"))
    r.row("Rows for this month", len(budgeted))
    if not budgeted:
        r.finding(
            "ERROR",
            f"Actual holds no budgeted amounts for {start.strftime('%Y-%m')}. "
            "Nothing can be committed, so free money equals your whole expected income.",
        )
    unclaimed = {
        category_id: amount
        for category_id, amount in budgeted.items()
        if category_id not in known_ids and amount
    }
    if unclaimed:
        r.finding(
            "ERROR",
            f"{money(sum(unclaimed.values()), cur).strip()} of this month's budget is addressed "
            f"to {len(unclaimed)} category id(s) that no live category claims, so it is missing "
            "from committed spending and free money is overstated by that much.",
        )
        redirect_map = (probe or {}).get("redirect_map") or {}
        for category_id, amount in sorted(unclaimed.items(), key=lambda item: -item[1]):
            target = redirect_map.get(category_id)
            r.text(
                f"    {category_id}  {money(amount, cur)}"
                + (f"  redirects to {target}" if target else "  (no redirect found)")
            )
    rows = (probe or {}).get("budget_rows")
    if rows is not None:
        by_id = {category.id: category for category in categories}
        redirect_map = (probe or {}).get("redirect_map") or {}
        month_int = int(start.strftime("%Y%m"))
        r.sub("every budget row, as stored  (month / category / amount)")
        counted = dropped = 0
        for row in sorted(rows, key=lambda item: (item["month"] or 0, -item["amount_cents"])):
            resolved = redirect_map.get(row["category_id"], row["category_id"])
            category = by_id.get(resolved)
            if category is not None:
                label = f"{category.group_name} / {category.name}"
                kind = "income" if category.is_income else "expense"
            else:
                raw = row["category_id"] or "(null)"
                label = f"(no category claims this row: {raw})"
                kind = "?????"
            note = ""
            if row["month"] is None:
                note = "  <-- NO MONTH: Clerk drops this row entirely"
                dropped += row["amount_cents"]
            elif not row["category_id"]:
                note = "  <-- NO CATEGORY: Clerk drops this row entirely"
                dropped += row["amount_cents"]
            elif row["month"] == month_int:
                counted += row["amount_cents"]
            if resolved != row["category_id"]:
                note += "  [redirected]"
            r.text(
                f"    {str(row['month'] or 'none'):>7}  {kind:<7} {money(row['amount_cents'], cur)}"
                f"  {hide('Category', label) if category else label}{note}"
            )
        r.row("counted for this month", money(counted, cur))
        r.text("")
        r.text("  Actual's budget screen does not sum these rows: it reads cached")
        r.text("  spreadsheet cells. If its total disagrees with the sum above, run")
        r.text("  Settings -> Reset budget cache in Actual and compare again.")
        if dropped:
            r.finding(
                "WARN",
                f"{money(dropped, cur).strip()} of budget is stored with no month or no "
                "category, so nothing can place it in a report. Actual's own budget code is "
                "gated the same way, so this is most likely an orphaned row rather than "
                "budget either side is counting.",
            )
    for key in sorted(budgeted_history)[-14:]:
        rows = budgeted_history[key]
        nonzero = sum(1 for value in rows.values() if value)
        marker = "  <-- reported month" if key == start.strftime("%Y-%m") else ""
        r.text(
            f"    {key}   rows {len(rows):>4}   non-zero {nonzero:>4}"
            f"   total {money(sum(rows.values()), cur)}{marker}"
        )

    # ---------------------------------------------------------------- 5
    month_transactions = to_transaction_infos(snapshot, start=start, end=end)
    spent_by_category: dict[str, int] = {}
    for transaction in month_transactions:
        if transaction.off_budget or transaction.is_transfer or transaction.is_starting_balance:
            continue
        if transaction.spend_cents > 0:
            key = transaction.category_id or ""
            spent_by_category[key] = spent_by_category.get(key, 0) + transaction.spend_cents

    committed_setting = list(settings.committed_groups)
    committed_set = {name.casefold() for name in committed_setting}
    group_names = sorted({category.group_name for category in categories if category.group_name})

    r.head("5. category groups")
    r.row("Committed-groups setting", committed_setting or "(none checked)")
    r.row(
        "Effective commitment rule",
        "group name is checked above" if committed_set else "budgeted > 0 this month",
    )
    unmatched = [
        name for name in committed_setting if name.casefold() not in {g.casefold() for g in group_names}
    ]
    if unmatched:
        r.finding(
            "ERROR",
            f"Committed group(s) {unmatched} match no group in Actual. Every category in them "
            "is treated as uncommitted, so free money is overstated by their whole budget.",
        )
    r.sub("group  /  cats  funded  hidden  budgeted-this-month  spent-this-month")
    stranded = 0
    for name in group_names:
        members = [category for category in categories if category.group_name == name]
        funded = sum(1 for category in members if budgeted.get(category.id, 0) > 0)
        gross = sum(max(0, budgeted.get(category.id, 0)) for category in members)
        spent = sum(spent_by_category.get(category.id, 0) for category in members)
        income = any(category.is_income for category in members)
        committed = name.casefold() in committed_set if committed_set else bool(funded)
        if income:
            flag = "INCOME"
        elif committed:
            flag = "COMMITTED"
        elif gross > 0:
            # Budgeted, yet not committed: its budget silently stops reducing
            # free money, which is the single loudest way the Overview drifts.
            flag = "<-- BUDGETED, NOT COMMITTED"
            stranded += gross
        else:
            flag = ""
        r.text(
            f"    {hide('Group', name)[:26]:<26} {len(members):>4} {funded:>7} "
            f"{sum(1 for c in members if c.hidden):>7}  {money(gross, cur)}  {money(spent, cur)}"
            f"  {flag}"
        )
    if stranded and committed_set:
        r.finding(
            "ERROR",
            f"{money(stranded, cur).strip()} is budgeted in groups you did not check as "
            "committed. Checking any box switches Clerk from 'anything budgeted counts' to "
            "'only these groups count', so that budget stopped reducing free money and the "
            "Overview now overstates it by exactly that amount. Check those groups too, or "
            "uncheck every box to go back to the budgeted-this-month rule.",
        )

    # --------------------------------------------------------------- 5b
    # Laid out the way Actual's budget screen is, so the two can be read side
    # by side. A category Actual shows a figure for and Clerk does not is a
    # budget row Clerk never received, and this is where it becomes visible.
    r.head("5b. every category, as actual's budget screen lays it out")
    r.text("  Compare group totals with Actual. A group that differs contains the answer.")
    expense_total = 0
    for name in group_names:
        members = sorted(
            (category for category in categories if category.group_name == name),
            key=lambda category: (-budgeted.get(category.id, 0), category.name),
        )
        income = any(category.is_income for category in members)
        gross = sum(max(0, budgeted.get(category.id, 0)) for category in members)
        if not income:
            expense_total += gross
        r.sub(f"{hide('Group', name)}   budgeted {money(gross, cur)}{'   [INCOME]' if income else ''}")
        for category in members:
            amount = budgeted.get(category.id, 0)
            spent = spent_by_category.get(category.id, 0)
            marks = []
            if category.hidden:
                marks.append("hidden")
            if category.id not in budgeted:
                marks.append("no budget row")
            r.text(
                f"    {hide('Category', category.name)[:30]:<30} {money(amount, cur)}"
                f"  spent {money(spent, cur)}"
                + (f"   ({', '.join(marks)})" if marks else "")
            )
    r.text("")
    r.row("TOTAL across expense groups", money(expense_total, cur))
    r.text("  If Actual's own total differs, the gap is a budget row Clerk never saw;")
    r.text("  the group subtotals above say which group to look in.")

    # ---------------------------------------------------------------- 6
    def is_committed(category: Any) -> bool:
        if category.is_income:
            return False
        if committed_set:
            return category.group_name.casefold() in committed_set
        return budgeted.get(category.id, 0) > 0

    committed_ids = {category.id for category in categories if is_committed(category)}
    r.head("6. commitment classification")
    r.row("Categories total", len(categories))
    r.row("Committed categories", len(committed_ids))
    r.row("Income categories", sum(1 for category in categories if category.is_income))
    zero_committed = [
        category
        for category in categories
        if category.id in committed_ids and budgeted.get(category.id, 0) <= 0
    ]
    if zero_committed:
        r.finding(
            "WARN",
            f"{len(zero_committed)} committed categories have no budget this month. Their "
            "spending is counted as committed overspend rather than discretionary, which moves "
            "money between the Overview's lines without changing the total.",
        )
        for category in zero_committed[:15]:
            r.text(
                f"    zero-budget committed: {hide('Group', category.group_name)} / "
                f"{hide('Category', category.name)}  spent {money(spent_by_category.get(category.id, 0), cur)}"
            )
    spent_uncommitted = [
        (category, spent_by_category.get(category.id, 0))
        for category in categories
        if category.id not in committed_ids
        and not category.is_income
        and spent_by_category.get(category.id, 0) > 0
    ]
    r.sub("uncommitted categories with spending this month (these consume free money)")
    for category, spent in sorted(spent_uncommitted, key=lambda item: -item[1])[:25]:
        r.text(
            f"    {hide('Group', category.group_name)[:22]:<22} "
            f"{hide('Category', category.name)[:26]:<26} {money(spent, cur)}"
        )
    if spent_by_category.get(""):
        r.finding(
            "WARN",
            f"{money(spent_by_category[''], cur).strip()} of spending this month is "
            "uncategorized, and all of it counts against free money.",
        )
        # Naming them is the whole point: "4 uncategorized" is unactionable when
        # Actual shows none, and the rows themselves say which is right.
        r.sub("the uncategorized transactions themselves")
        for transaction in sorted(month_transactions, key=lambda item: item.date):
            if transaction.category_id:
                continue
            if transaction.off_budget or transaction.is_transfer:
                continue
            if transaction.is_starting_balance or transaction.spend_cents <= 0:
                continue
            r.text(
                f"    {transaction.date}  {money(transaction.spend_cents, cur)}  "
                f"{hide('Account', transaction.account_name)[:22]:<22} "
                f"{hide('Payee', transaction.payee_name) if transaction.payee_name else '(no payee)'}"
            )
            r.text(f"      id {transaction.id}")

    # --------------------------------------------------------------- 6b
    # Every figure on the Overview is a partition of one month's spending. If
    # the parts do not add back up to the whole, some of it is being counted
    # under a heading nothing on screen accounts for -- which is exactly how
    # the numbers can each look plausible and still be wrong.
    income_ids_all = {category.id for category in categories if category.is_income}
    buckets = {"committed": 0, "discretionary": 0, "orphaned": 0, "uncategorized": 0}
    orphans: dict[str, int] = {}
    for transaction in month_transactions:
        if transaction.is_starting_balance:
            continue
        raw_id = transaction.category_id or ""
        if raw_id and raw_id in income_ids_all:
            continue
        if transaction.off_budget or transaction.is_transfer:
            continue
        # Spending is money leaving, so a refund is a negative contribution.
        delta = -transaction.amount_cents
        if raw_id and raw_id in committed_ids:
            buckets["committed"] += delta
        elif raw_id and raw_id in known_ids:
            buckets["discretionary"] += delta
        elif raw_id:
            buckets["orphaned"] += delta
            orphans[raw_id] = orphans.get(raw_id, 0) + delta
        else:
            buckets["uncategorized"] += delta

    r.head("6b. does this month's spending add up")
    for label, value in buckets.items():
        r.row(label, money(value, cur))
    r.row("total", money(sum(buckets.values()), cur))
    if orphans:
        deleted_names = (probe or {}).get("deleted_categories") or {}
        r.finding(
            "ERROR",
            f"{money(buckets['orphaned'], cur).strip()} of this month's spending sits on "
            f"{len(orphans)} category id(s) that Actual cannot resolve -- present in neither "
            "the category list nor its redirect table. Recategorize those transactions in "
            "Actual; the ids below are what to search for.",
        )
        r.sub("spending on categories that no longer exist")
        for category_id, cents in sorted(orphans.items(), key=lambda item: -item[1]):
            name = deleted_names.get(category_id)
            target = ((probe or {}).get("redirect_map") or {}).get(category_id)
            if name:
                shown = hide("Category", name)
            elif target:
                shown = f"redirects to {target}, which the snapshot did not resolve"
            else:
                shown = "(no category row and no redirect: nothing in Actual claims it)"
            r.text(f"    {category_id}  {money(cents, cur)}  {shown}")
        if not deleted_names:
            r.text("    (re-run after a rebuild to resolve these ids to names)")

    # ---------------------------------------------------------------- 7
    r.head("7. income")
    income_categories = [category for category in categories if category.is_income]
    income_ids = {category.id for category in income_categories}
    received_by_category: dict[str, int] = {}
    for transaction in month_transactions:
        if (
            transaction.category_id in income_ids
            and not transaction.off_budget
            and not transaction.is_transfer
        ):
            key = transaction.category_id or ""
            received_by_category[key] = received_by_category.get(key, 0) + max(
                0, transaction.amount_cents
            )
    r.sub("income category  /  budgeted  received-this-month")
    for category in income_categories:
        r.text(
            f"    {hide('Category', category.name)[:30]:<30} "
            f"{money(budgeted.get(category.id, 0), cur)} {money(received_by_category.get(category.id, 0), cur)}"
        )
    if not income_categories:
        r.finding("WARN", "Actual has no income categories, so Clerk cannot read budgeted income.")
    r.sub("income history Clerk holds")
    for when, cents in list(snapshot.get("income_history") or [])[-14:]:
        r.text(f"    {when.strftime('%Y-%m')}   {money(cents, cur)}")

    # ---------------------------------------------------------------- 8
    live = build_budget_report(
        today=today,
        categories=categories,
        budgeted=budgeted,
        transactions=to_transaction_infos(snapshot, start=datetime.date.min, end=end),
        income_history=snapshot["income_history"],
        budgeted_history=budgeted_history,
        committed_groups=settings.committed_groups,
        income_override_cents=settings.monthly_income_override_cents,
        income_lookback_months=settings.income_lookback_months,
    ).as_dict()

    r.head("8. budget report, recomputed live")
    basis_help = {
        "override": "the Monthly income setting overrides everything else",
        "budgeted": "income budgeted in Actual, which is what Actual's own Projected Savings uses",
        "received": "income actually received so far this month",
        "average": f"a {settings.income_lookback_months}-month trailing average",
        "unknown": "nothing to go on",
    }
    r.row("Income basis chosen", f"{live['income_basis']}  ({basis_help.get(live['income_basis'], '')})")
    if live["income_basis"] in ("received", "average"):
        r.finding(
            "WARN",
            f"Expected income is inferred ({live['income_basis']}), not read from Actual. "
            "Budget income into an income category in Actual, or set the Monthly income "
            "override, and Clerk's free money will line up with Actual's Projected Savings.",
        )
    for key in (
        "income_budgeted_cents",
        "income_received_cents",
        "income_average_cents",
        "expected_income_cents",
        "committed_cents",
        "committed_carried_cents",
        "committed_spent_cents",
        "committed_overspend_cents",
        "free_cents",
        "discretionary_spent_cents",
        "uncategorized_cents",
        "spent_cents",
        "remaining_cents",
        "projected_spend_cents",
    ):
        r.row(key, money(live[key], cur))
    r.row("uncategorized_count", live["uncategorized_count"])
    r.row("configured", live["configured"])
    r.text("")
    r.text("  free    = expected_income - committed")
    r.text("  spent   = discretionary_spent + committed_overspend")
    r.text("  remain  = free - spent")

    r.sub("stored snapshot vs live recompute")
    drifted = 0
    for key, value in live.items():
        if not isinstance(value, int | float | str | bool):
            continue
        if key not in stored_budget:
            continue
        if stored_budget[key] != value:
            drifted += 1
            shown_old = money(stored_budget[key], cur) if key.endswith("_cents") else stored_budget[key]
            shown_new = money(value, cur) if key.endswith("_cents") else value
            r.text(f"    {key:<32} stored {shown_old}   live {shown_new}")
    if drifted:
        r.finding(
            "ERROR",
            f"{drifted} figures differ between the stored Overview and a live read. "
            "The Overview is out of date; run a sync or press Refresh.",
        )
    else:
        r.text("    (identical)")

    # ---------------------------------------------------------------- 9
    r.head("9. what actual itself would show")
    income_budgeted_total = sum(
        max(0, budgeted.get(category.id, 0)) for category in categories if category.is_income
    )
    expense_budgeted_total = sum(
        max(0, budgeted.get(category.id, 0)) for category in categories if not category.is_income
    )
    r.row("Budgeted income (all income cats)", money(income_budgeted_total, cur))
    r.row("Budgeted expense (all other cats)", money(expense_budgeted_total, cur))
    r.row("Actual's Projected Savings", money(income_budgeted_total - expense_budgeted_total, cur))
    r.row("Clerk's free money", money(live["free_cents"], cur))
    gap = live["free_cents"] - (income_budgeted_total - expense_budgeted_total)
    r.row("Difference", money(gap, cur))
    if gap:
        r.text("")
        r.text("  These agree only when income is budgeted in Actual AND every budgeted")
        r.text("  category counts as committed. The difference above is exactly the budget")
        r.text("  of categories Clerk did not treat as committed, plus any income gap.")
        uncommitted_budget = sum(
            max(0, budgeted.get(category.id, 0))
            for category in categories
            if not category.is_income and category.id not in committed_ids
        )
        r.row("  budget of uncommitted cats", money(uncommitted_budget, cur))
        r.row("  income gap", money(live["expected_income_cents"] - income_budgeted_total, cur))

    # ---------------------------------------------------------------- 10
    r.head("10. transactions")
    all_transactions = snapshot["transactions"]
    r.row("History window starts", snapshot.get("history_start"))
    r.row("History lookback setting", f"{settings.history_lookback_days} days")
    r.row("Transactions in window", len(all_transactions))
    r.row("Transactions this month", len(month_transactions))
    for label, predicate in (
        ("transfers", lambda item: item.get("is_transfer")),
        ("off-budget", lambda item: item.get("off_budget")),
        ("starting balance", lambda item: item.get("is_starting_balance")),
        ("uncategorized", lambda item: not item.get("category_id")),
    ):
        total = sum(1 for item in all_transactions if predicate(item))
        month = sum(
            1 for item in all_transactions if predicate(item) and start <= item["date"] <= end
        )
        r.row(f"  {label}", f"{total} in window, {month} this month")
    if snapshot.get("history_start") and snapshot["history_start"] > start:
        r.finding(
            "ERROR",
            "The history window starts after this month began, so the report is missing "
            "part of the month. Raise 'History read from Actual (days)'.",
        )

    # ---------------------------------------------------------------- 11
    r.head("11. account freshness")
    r.row("transaction_stale_days", settings.transaction_stale_days)
    r.row("Overview 'gone quiet' trips at", f"> {settings.transaction_stale_days} days")
    r.row("Connections 'no transactions' at", f"> {settings.transaction_stale_days * 2} days")
    r.row("balance_stale_hours", f"{settings.balance_stale_hours} (bank balance age only)")
    r.sub("account  /  days since txn  /  monitored  /  overview-quiet  /  connections-quiet")
    for account in snapshot["accounts"]:
        if account["closed"]:
            continue
        last = account.get("last_transaction_date")
        days = (today - last).days if last else None
        monitored = account["id"] not in unmonitored
        quiet_overview = bool(
            account["sync_source"] and monitored and (days is None or days > settings.transaction_stale_days)
        )
        quiet_health = days is not None and days > settings.transaction_stale_days * 2
        r.text(
            f"    {hide('Account', account['name'])[:26]:<26} "
            f"{'never' if days is None else str(days) + 'd':>7} "
            f"{'yes' if monitored else 'MUTED':>6} "
            f"{'QUIET' if quiet_overview else '-':>6} "
            f"{'QUIET' if quiet_health else '-':>6}"
        )

    # ---------------------------------------------------------------- 12
    r.head("12. connection health")
    for item in health:
        r.text(
            f"    {hide('Account', item.get('account_name'))[:26]:<26} "
            f"{str(item.get('status')):<16} {str(item.get('detail') or '')[:70]}"
        )
    if not health:
        r.text("    (no health snapshots recorded yet)")

    # ---------------------------------------------------------------- 13
    r.head("13. stored counts")
    for key, value in sorted(counts.items()):
        r.row(key, value)

    # ---------------------------------------------------------------- 14
    r.head("14. settings")
    public = settings.public_dict()
    locked = set(public.get("environment_overrides") or [])
    for key in sorted(public):
        if key == "environment_overrides":
            continue
        marker = "  [env]" if key in locked else ""
        r.row(key, f"{public[key]}{marker}")

    return r.render()
