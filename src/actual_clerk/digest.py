"""The morning budget message.

One notification a day. The header never changes -- it is the name you gave
the report, so it is recognisable on a lock screen before a word is read --
and everything the morning actually holds goes in the body, as a headline
figure followed by short blocks. Each block can be switched off, because a
report that says everything is a report nobody reads, and which parts matter
is a matter of taste rather than something Clerk can work out.
"""

from __future__ import annotations

import datetime
from typing import Any

from actual_clerk.domain.budget import format_money
from actual_clerk.domain.health import ALERTING_STATUSES, STATUS_LABELS

# A middle dot separates a figure from its qualifier. It renders on every
# platform ntfy reaches more cleanly than box-drawing characters do on a phone.
BULLET = "\u00b7"
DASH = "\u2014"

# These are the parts of the report that represent the budget itself. Daily
# pace and safe-to-spend figures move with the calendar even when no money did,
# so they must not turn a quiet morning into a supposed budget change. Returned
# money is here because a refund and a matching charge on the same day leave
# what is left unmoved while two real things happened to the month.
_BUDGET_STATE_FIELDS = (
    "free_cents",
    "returned_cents",
    "spent_cents",
    "remaining_cents",
)


def plural(count: int, noun: str) -> str:
    """"1 transaction", "3 transactions" -- a report reads, it does not log."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


class _Sections:
    """Which blocks to include, defaulting to on for anything unnamed."""

    def __init__(self, sections: dict[str, bool] | None):
        self.sections = sections or {}

    def __call__(self, name: str) -> bool:
        return bool(self.sections.get(name, True))


class _Body:
    """Markdown-compatible blocks that remain tidy when rendered as plain text."""

    def __init__(self) -> None:
        self.blocks: list[list[str]] = []

    def block(self, *lines: str) -> None:
        kept = [line for line in lines if line]
        if kept:
            self.blocks.append(kept)

    def listing(self, heading: str, items: list[str]) -> None:
        if items:
            # ntfy currently renders Markdown only in its web app. A conventional
            # hyphen list and the small amount of bold markup improve that
            # client while remaining understandable as raw text in phone apps.
            self.blocks.append([f"**{heading}**", ""] + [f"- {item}" for item in items])

    def render(self) -> str:
        return "\n\n".join("\n".join(block) for block in self.blocks)


def _bank_snapshot_dates(health: list[dict[str, Any]]) -> dict[str, str]:
    """The latest bank-side balance timestamp retained for each watched account."""

    return {
        str(item.get("account_id")): str(item.get("remote_balance_date"))
        for item in health
        if item.get("account_id")
        and item.get("remote_balance_date")
        and item.get("monitored", True)
        and item.get("status") != "not_linked"
    }


def _as_datetime(value: Any) -> datetime.datetime | None:
    try:
        parsed = datetime.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.UTC)


def report_change(
    *,
    report: dict[str, Any],
    previous: dict[str, Any] | None,
    health: list[dict[str, Any]],
    today: datetime.date,
) -> dict[str, Any]:
    """Describe whether the spendable budget changed since the last digest.

    Each provider attaches a timestamp to the balance it reports (SimpleFIN's
    ``balance-date``, Plaid's last successful update). An advance proves that
    newer bank data arrived. A date that did not advance proves only that no
    provider exposed a newer balance timestamp; it cannot prove whether the
    bank was polled again and found the same value.
    """

    result: dict[str, Any] = {"unchanged": False, "reason": "no_baseline"}
    if not previous or not isinstance(previous.get("report"), dict):
        return result
    prior_report = previous["report"]
    try:
        prior_date = datetime.date.fromisoformat(str(previous.get("local_date") or ""))
    except ValueError:
        return result
    if prior_date >= today or prior_report.get("month") != report.get("month"):
        return result
    if any(prior_report.get(field) != report.get(field) for field in _BUDGET_STATE_FIELDS):
        return {"unchanged": False, "reason": "budget_changed", "since": prior_date.isoformat()}

    result = {"unchanged": True, "reason": "unknown", "since": prior_date.isoformat()}
    current_dates = _bank_snapshot_dates(health)
    previous_dates = previous.get("bank_snapshot_dates")
    if not current_dates or not isinstance(previous_dates, dict) or not previous_dates:
        return result

    comparable = []
    for account_id, current_value in current_dates.items():
        current = _as_datetime(current_value)
        prior = _as_datetime(previous_dates.get(account_id))
        if current is not None and prior is not None:
            comparable.append((current, prior))
    if not comparable:
        return result
    result["reason"] = (
        "newer_bank_data"
        if any(current > prior for current, prior in comparable)
        else "no_newer_bank_snapshot"
    )
    return result


def build_digest(
    *,
    report: dict[str, Any],
    health: list[dict[str, Any]],
    review_count: int,
    accounts: list[dict[str, Any]] | None = None,
    currency: str = "USD",
    today: datetime.date | None = None,
    title: str = "The Morning Report",
    sections: dict[str, bool] | None = None,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose the digest payload: its fixed title, body, tags, and priority."""

    today = today or datetime.date.today()
    show = _Sections(sections)
    body = _Body()
    # ntfy turns a recognized tag into the emoji preceding the fixed title.
    # The budget block already owns the money bag, so the report itself is news.
    tags: list[str] = ["newspaper"]
    priority = 3

    free = int(report.get("free_cents", 0))
    returned = int(report.get("returned_cents", 0))
    # What the month is measured against: free money plus anything an earlier
    # month handed back. Older stored reports carry no such figure.
    available = int(report.get("available_cents", free))
    remaining = int(report.get("remaining_cents", 0))
    spent = int(report.get("spent_cents", 0))
    percent = float(report.get("remaining_percent", 0.0))
    days_remaining = int(report.get("days_remaining", 1)) or 1
    change = report_change(report=report, previous=previous, health=health, today=today)

    body.block(f"{today:%A}, {today.day} {today:%B}")

    if not report.get("configured"):
        body.listing(
            "💰 Budget",
            [
                "Clerk cannot work out your monthly income yet.",
                "Enter it in Settings, or record this month's income in Actual, "
                "and the report starts tomorrow.",
            ],
        )
        return _payload(
            title, body, tags + ["warning"], 3, report, health, review_count, today, change
        )

    budget_items: list[str] = []
    if change["unchanged"]:
        if change["reason"] == "newer_bank_data":
            quiet_message = (
                "Nothing to report — your bank provider has newer bank data, but no new "
                "discretionary spending changed your budget."
            )
        elif change["reason"] == "no_newer_bank_snapshot":
            quiet_message = (
                "Nothing to report — your bank provider has not exposed a newer bank balance "
                "timestamp since the last report."
            )
        else:
            quiet_message = (
                "Nothing to report — your discretionary budget is unchanged since the last report."
            )
        budget_items.append(quiet_message)
    elif available <= 0:
        # Nothing is free, so there is no headline figure to lead with.
        budget_items.extend(
            [
                f"No free money budgeted for {today.strftime('%B')}.",
                f"Committed spending of "
                f"{format_money(report.get('committed_cents', 0), currency)} "
                f"is at or above expected income of "
                f"{format_money(report.get('expected_income_cents', 0), currency)}.",
            ]
        )
        priority = 4
        tags.append("warning")
    else:
        if show("headline"):
            # Past zero there is nothing "left"; there is an amount gone past.
            if remaining < 0:
                headline = f"{format_money(abs(remaining), currency)} over budget"
            else:
                headline = f"{format_money(remaining, currency)} free money left"
            budget_items.append(f"{headline} {BULLET} {abs(percent):.0%}")

        if show("spending"):
            budget_items.append(
                f"Spent {format_money(spent, currency)} of "
                f"{format_money(available, currency)} since the 1st"
            )
            if returned > 0:
                budget_items.append(
                    f"Plus {format_money(returned, currency)} refunded from an "
                    "earlier month"
                )
        if show("safe_to_spend") and remaining > 0:
            safe = int(report.get("daily_safe_to_spend_cents", 0))
            budget_items.append(
                f"{format_money(safe, currency)} a day keeps you level for the "
                f"{plural(days_remaining, 'day')} left"
            )
        if show("pace"):
            delta = format_money(abs(int(report.get("pace_delta_cents", 0))), currency)
            side = "under" if report.get("on_track") else "over"
            budget_items.append(f"{delta} {side} an even pace for the month")
        if show("projection"):
            projected = int(report.get("projected_remaining_cents", 0))
            ending = "with" if projected >= 0 else "over by"
            budget_items.append(
                f"On this pace the month ends {ending} "
                f"{format_money(abs(projected), currency)}"
            )

        if remaining <= 0:
            budget_items.append("Free money for this month is already spent.")
            priority = 4
            tags.append("warning")
        if not report.get("on_track"):
            priority = max(priority, 4)

    if not change["unchanged"] and show("commitments"):
        overspend = int(report.get("committed_overspend_cents", 0))
        if overspend > 0:
            budget_items.append(
                f"Committed categories are over budget by "
                f"{format_money(overspend, currency)}"
            )

    body.listing("💰 Budget", budget_items)

    if show("balances"):
        balances = [
            f"{account.get('name') or 'Account'}: "
            f"{format_money(int(account.get('balance_cents', 0)), currency)}"
            for account in (accounts or [])
            if account.get("monitored", True) and account.get("sync_source")
        ]
        body.listing("💳 Account balances", balances)

    degraded = [item for item in health if item.get("status") in ALERTING_STATUSES]
    if degraded:
        # The alarm is raised whether or not the block is shown: a broken bank
        # connection is not a formatting preference.
        priority = 5
        tags.append("rotating_light")
        if show("connections"):
            named = [
                f"{item.get('account_name')} {DASH} "
                f"{STATUS_LABELS.get(item.get('status'), item.get('status'))}"
                for item in degraded[:4]
            ]
            if len(degraded) > 4:
                named.append(f"and {len(degraded) - 4} more")
            body.listing("⚠️ Connections needing attention", named)

    if show("attention"):
        waiting = []
        if review_count:
            waiting.append(f"{plural(review_count, 'transaction')} to review in Clerk")
        uncategorized = int(report.get("uncategorized_count", 0))
        if uncategorized:
            waiting.append(
                f"{plural(uncategorized, 'transaction')} still uncategorized this month "
                f"({format_money(report.get('uncategorized_cents', 0), currency)})"
            )
        body.listing("👀 Waiting for you", waiting)

    return _payload(title, body, tags, priority, report, health, review_count, today, change)


def _payload(
    title: str,
    body: _Body,
    tags: list[str],
    priority: int,
    report: dict[str, Any],
    health: list[dict[str, Any]],
    review_count: int,
    today: datetime.date,
    change: dict[str, Any],
) -> dict[str, Any]:
    return {
        "local_date": today.isoformat(),
        "title": title,
        "message": body.render(),
        # ntfy's web app turns the conventional lists into proper Markdown.
        # Phone clients currently show the source, which is why the message
        # avoids formatting markers that would be noisy when left unrendered.
        "markdown": True,
        "tags": tags,
        "priority": priority,
        "report": report,
        "budget_change": change,
        "bank_snapshot_dates": _bank_snapshot_dates(health),
        "degraded_accounts": [
            {"account_name": item.get("account_name"), "status": item.get("status")}
            for item in health
            if item.get("status") in ALERTING_STATUSES
        ],
        "review_count": review_count,
    }


def health_alert(transitions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A message for connections that just changed state, or None when quiet.

    Alerting on transitions rather than on state is what makes this bearable:
    a broken connection is announced once when it breaks and once when it comes
    back, not every hour in between.
    """

    worsened = [item for item in transitions if item.get("status") in ALERTING_STATUSES]
    recovered = [
        item
        for item in transitions
        if item.get("status") == "ok" and item.get("previous_status") in ALERTING_STATUSES
    ]
    if not worsened and not recovered:
        return None

    if worsened:
        first = worsened[0]
        extra = f" and {len(worsened) - 1} more" if len(worsened) > 1 else ""
        title = f"Bank connection problem: {first.get('account_name')}{extra}"
        lines = [
            f"{item.get('account_name')}: "
            f"{STATUS_LABELS.get(item.get('status'), item.get('status'))} - "
            f"{item.get('detail', '')}".strip()
            for item in worsened[:4]
        ]
        priority, tags = 5, ["rotating_light", "bank"]
    else:
        first = recovered[0]
        extra = f" and {len(recovered) - 1} more" if len(recovered) > 1 else ""
        title = f"Bank connection restored: {first.get('account_name')}{extra}"
        lines = [f"{item.get('account_name')} is reporting normally again." for item in recovered]
        priority, tags = 3, ["white_check_mark", "bank"]

    if worsened and recovered:
        lines.append(
            f"{len(recovered)} other connection(s) recovered: "
            + ", ".join(str(item.get("account_name")) for item in recovered[:4])
        )
    return {"title": title, "message": "\n".join(lines), "priority": priority, "tags": tags}
