"""The morning budget message.

One notification a day, built to be read on a lock screen in five seconds: how
much free money is left, whether that is ahead of or behind the month's pace,
what is safe to spend today, and anything that needs a human. Nothing else
belongs in it -- a digest that reports everything is a digest nobody reads.
"""

from __future__ import annotations

import datetime
from typing import Any

from actual_clerk.domain.budget import format_money
from actual_clerk.domain.health import ALERTING_STATUSES, STATUS_LABELS


def build_digest(
    *,
    report: dict[str, Any],
    health: list[dict[str, Any]],
    review_count: int,
    currency: str = "USD",
    today: datetime.date | None = None,
) -> dict[str, Any]:
    """Compose the digest payload, its ntfy title, body, and priority."""

    today = today or datetime.date.today()
    lines: list[str] = []
    tags: list[str] = ["moneybag"]
    priority = 3

    free = int(report.get("free_cents", 0))
    remaining = int(report.get("remaining_cents", 0))
    spent = int(report.get("spent_cents", 0))
    percent = float(report.get("remaining_percent", 0.0))
    days_remaining = int(report.get("days_remaining", 1)) or 1

    if not report.get("configured"):
        title = "Set up your budget in Clerk"
        lines.append(
            "Clerk cannot work out your monthly income yet. Enter it in Settings, or record "
            "this month's income in Actual, and the report starts tomorrow."
        )
        return _payload(title, lines, tags + ["warning"], 3, report, health, review_count, today)

    if free <= 0:
        title = f"No free money budgeted for {today.strftime('%B')}"
        lines.append(
            f"Committed spending of {format_money(report.get('committed_cents', 0), currency)} "
            f"is at or above expected income of "
            f"{format_money(report.get('expected_income_cents', 0), currency)}."
        )
        priority = 4
        tags.append("warning")
    else:
        # Past zero there is nothing "left"; there is an amount gone past.
        if remaining < 0:
            title = f"{format_money(abs(remaining), currency)} over budget ({abs(percent):.0%})"
        else:
            title = f"{format_money(remaining, currency)} free money left ({percent:.0%})"
        lines.append(
            f"Spent {format_money(spent, currency)} of "
            f"{format_money(free, currency)} since the 1st."
        )
        safe = int(report.get("daily_safe_to_spend_cents", 0))
        if remaining > 0:
            lines.append(
                f"{format_money(safe, currency)} a day keeps you level for the "
                f"{days_remaining} day(s) left."
            )
        else:
            lines.append("Free money for this month is already spent.")
            priority = 4
            tags.append("warning")

        delta = int(report.get("pace_delta_cents", 0))
        if report.get("on_track"):
            lines.append(
                f"You are {format_money(abs(delta), currency)} under an even pace for the month."
            )
        else:
            lines.append(
                f"You are {format_money(abs(delta), currency)} over an even pace for the month."
            )
            priority = max(priority, 4)

    overspend = int(report.get("committed_overspend_cents", 0))
    if overspend > 0:
        lines.append(
            f"Committed categories are over budget by {format_money(overspend, currency)}."
        )

    degraded = [item for item in health if item.get("status") in ALERTING_STATUSES]
    if degraded:
        priority = 5
        tags.append("rotating_light")
        names = ", ".join(
            f"{item.get('account_name')} ({STATUS_LABELS.get(item.get('status'), item.get('status'))})"
            for item in degraded[:3]
        )
        extra = f" and {len(degraded) - 3} more" if len(degraded) > 3 else ""
        lines.append(f"Bank connections needing attention: {names}{extra}.")

    if review_count:
        lines.append(f"{review_count} transaction(s) waiting for your review in Clerk.")

    uncategorized = int(report.get("uncategorized_count", 0))
    if uncategorized:
        lines.append(
            f"{uncategorized} transaction(s) this month are still uncategorized "
            f"({format_money(report.get('uncategorized_cents', 0), currency)})."
        )

    return _payload(title, lines, tags, priority, report, health, review_count, today)


def _payload(
    title: str,
    lines: list[str],
    tags: list[str],
    priority: int,
    report: dict[str, Any],
    health: list[dict[str, Any]],
    review_count: int,
    today: datetime.date,
) -> dict[str, Any]:
    return {
        "local_date": today.isoformat(),
        "title": title,
        "message": "\n".join(lines),
        "tags": tags,
        "priority": priority,
        "report": report,
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
