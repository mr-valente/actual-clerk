"""Keep anticipated charges in step with what Actual actually holds.

Runs every time the overview is rebuilt -- after a sync, a filing run, a
connection check, the morning report, or a manual refresh -- so an
anticipation is settled the moment the bank's row is read, whichever job
happened to read it. Nothing here writes to Actual; the only state that
changes is Clerk's own ledger of what the phone has seen.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
from typing import Any

from actual_clerk.categorize import build_memory
from actual_clerk.config import Settings
from actual_clerk.db import Database
from actual_clerk.domain.anticipated import (
    KIND_CHARGE,
    KIND_CREDIT,
    AnticipatedCharge,
    CandidateTransaction,
    expired_charges,
    match_charges,
    parse_notification,
)
from actual_clerk.domain.intelligence import SOURCE_RULE, RuleBook, canonical_key, resolve
from actual_clerk.domain.merchants import normalize_merchant

log = logging.getLogger(__name__)

# Where a provisional category came from. An approved one is the user's answer
# for this one charge (Apply in Review); a taught one is the user's answer made
# into a rule (Always, or the old "Always file as..."). Neither is overwritten
# by memory. A rule is the user's word too, read from the rule book; a memory
# one is re-derived on every pass so it follows the evidence.
SOURCE_MEMORY = "memory"
SOURCE_TAUGHT = "taught"
SOURCE_APPROVED = "approved"
# SOURCE_RULE is shared with the resolver.
USER_SOURCES = (SOURCE_TAUGHT, SOURCE_APPROVED)

# The decision source for a bank row filed with the user's answer about the
# phone charge it settled: the person decided, before the bank had a row.
SOURCE_PERSON = "person"

# A phone charge's review is a decision whose transaction id says so; the
# filing run and the observation channel only ever read ids from Actual.
PHONE_PREFIX = "phone:"

# Statuses an anticipation can be in. Only `open` charges count in the budget.
OPEN = "open"
MATCHED = "matched"
EXPIRED = "expired"
DISMISSED = "dismissed"
# A notification Clerk could read but never anticipates: a declined
# authorisation, or one with no amount in it. Kept so the phone log explains
# itself, closed from the start.
IGNORED = "ignored"

# The same words from the same app inside this window are one notification,
# whatever key they arrive under. Mirrors the phone's own repeat filter.
REPEAT_WINDOW_SECONDS = 10 * 60


def phone_transaction_id(charge_id: str) -> str:
    return f"{PHONE_PREFIX}{charge_id}"


def phone_item(row: dict[str, Any], *, account_name: str = "") -> dict[str, Any]:
    """An open charge in the shape the categorizer reads a transaction in."""

    try:
        noticed = datetime.date.fromisoformat(str(row.get("noticed_date") or ""))
    except ValueError:
        noticed = datetime.datetime.fromtimestamp(float(row["noticed_at"]), datetime.UTC).date()
    merchant = str(row.get("merchant") or "")
    return {
        "id": phone_transaction_id(str(row["id"])),
        "merchant_key": str(row.get("merchant_key") or ""),
        "merchant_label": merchant,
        "payee_name": merchant or str(row.get("title") or ""),
        "imported_description": merchant,
        "account_id": str(row.get("actual_account_id") or ""),
        "account_name": account_name,
        "date": noticed,
        "amount_cents": int(row.get("amount_cents", 0)),
    }


def notification_key(package_name: str, posted_at_ms: int, title: str, text: str) -> str:
    """A stable identity for one notification, so a re-delivery is not a second charge."""

    digest = hashlib.sha256(f"{title}\n{text}".encode()).hexdigest()[:16]
    return f"{package_name}:{posted_at_ms}:{digest}"


def record_notification(
    database: Database,
    settings: Settings,
    *,
    source: dict[str, Any],
    posted_at_ms: int,
    title: str,
    text: str,
    key: str = "",
) -> tuple[dict[str, Any], bool]:
    """Turn one forwarded notification into an anticipated charge, once."""

    parsed = parse_notification(title, text)
    noticed_at = posted_at_ms / 1000 if posted_at_ms > 0 else datetime.datetime.now(datetime.UTC).timestamp()
    noticed_date = datetime.datetime.fromtimestamp(noticed_at, settings.zone).date().isoformat()
    status = OPEN if parsed.kind in (KIND_CHARGE, KIND_CREDIT) else IGNORED
    repeat = database.recent_anticipated_duplicate(
        source["id"],
        title=title,
        text=text,
        noticed_at=noticed_at,
        window_seconds=REPEAT_WINDOW_SECONDS,
    )
    if repeat is not None:
        return repeat, False
    row, created = database.add_anticipated_charge(
        {
            "source_id": source["id"],
            "actual_account_id": source.get("actual_account_id") or "",
            "notification_key": key or notification_key(source["package_name"], posted_at_ms, title, text),
            "kind": parsed.kind,
            "amount_cents": parsed.amount_cents,
            "merchant": parsed.merchant,
            "merchant_key": parsed.merchant_key,
            "title": title,
            "text": text,
            "noticed_at": noticed_at,
            "noticed_date": noticed_date,
            "status": status,
        }
    )
    return row, created


def reconcile(
    database: Database,
    snapshot: dict[str, Any],
    settings: Settings,
    *,
    today: datetime.date,
) -> dict[str, Any]:
    """Settle open anticipations against the snapshot and retire stale ones.

    Returns the anticipations still open afterwards, plus counts of what
    changed, so the caller can build the report from the same list.
    """

    open_rows = database.list_anticipated_charges(status=OPEN, limit=1000)
    summary: dict[str, Any] = {"open": [], "matched": 0, "expired": 0, "categorized": 0}
    if not open_rows or not settings.anticipated_enabled:
        close_overtaken_reviews(database)
        summary["open"] = public_charges(database, open_rows) if settings.anticipated_enabled else []
        return summary

    aliases = database.alias_map()
    charges = [_as_charge(row) for row in open_rows]
    candidates = [
        CandidateTransaction(
            id=str(item.get("id") or ""),
            account_id=str(item.get("account_id") or ""),
            amount_cents=int(item.get("amount_cents", 0)),
            date=item["date"],
            merchant_key=str(item.get("merchant_key") or ""),
            payee_name=str(item.get("payee_name") or item.get("imported_description") or ""),
            imported=bool(str(item.get("imported_id") or "").strip()),
        )
        for item in snapshot.get("transactions") or []
        if item.get("date") is not None
        and not item.get("is_child")
        and not item.get("is_starting_balance")
    ]
    matches = match_charges(
        charges,
        candidates,
        window_days=settings.anticipated_match_window_days,
        used_transaction_ids=database.matched_transaction_ids(),
        aliases=aliases,
    )
    by_id = {row["id"]: row for row in open_rows}
    by_transaction = {candidate.id: candidate for candidate in candidates}
    items = {str(item.get("id") or ""): item for item in snapshot.get("transactions") or []}
    for match in matches:
        if database.resolve_anticipated_charge(
            match.charge_id,
            MATCHED,
            matched_transaction_id=match.transaction_id,
            matched_payee=match.payee_name,
            matched_date=match.date.isoformat(),
            match_reason=match.reason,
        ):
            summary["matched"] += 1
            _learn_from_settlement(
                database, by_id[match.charge_id], by_transaction[match.transaction_id], aliases
            )
            if _carry_decision(
                database, by_id[match.charge_id], items.get(match.transaction_id) or {}
            ):
                summary["carried"] = summary.get("carried", 0) + 1
    settled = {match.charge_id for match in matches}
    for charge_id in expired_charges(
        [charge for charge in charges if charge.id not in settled],
        today=today,
        expire_days=settings.anticipated_expire_days,
    ):
        if database.resolve_anticipated_charge(charge_id, EXPIRED, match_reason="never posted"):
            summary["expired"] += 1
    if summary["matched"] or summary["expired"]:
        log.info(
            "Anticipated charges: %d settled against Actual, %d expired unposted",
            summary["matched"],
            summary["expired"],
        )
    remaining = database.list_anticipated_charges(status=OPEN, limit=1000)
    summary["categorized"] = classify(database, snapshot, settings, remaining, today=today)
    close_overtaken_reviews(database)
    summary["asking"] = open_reviews(
        database, database.list_anticipated_charges(status=OPEN, limit=1000), settings
    )
    summary["open"] = public_charges(
        database, database.list_anticipated_charges(status=OPEN, limit=1000)
    )
    return summary


def date_corrections(
    database: Database, snapshot: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Bank rows a phone charge settled that do not yet carry the phone's day.

    The phone saw the purchase happen; the bank's date is when it got round
    to posting it, and across a month's end that decides which month the
    money counts in. Each settled charge's date is carried onto its row
    once. Returns the rows to move (with the charge each belongs to) and the
    charges that need nothing written: already on the right day, or their
    row is gone.
    """

    waiting = database.matched_charge_dates(uncarried_only=True)
    if not waiting:
        return [], []
    items = {str(item.get("id") or ""): item for item in snapshot.get("transactions") or []}
    updates: list[dict[str, Any]] = []
    finished: list[str] = []
    for row in waiting:
        item = items.get(str(row["matched_transaction_id"]))
        try:
            noticed = datetime.date.fromisoformat(str(row.get("noticed_date") or ""))
        except ValueError:
            noticed = None
        if item is None or noticed is None or item.get("date") == noticed:
            finished.append(row["id"])
            continue
        updates.append(
            {"charge_id": row["id"], "transaction_id": item["id"], "date": noticed}
        )
    return updates, finished


def classify(
    database: Database,
    snapshot: dict[str, Any],
    settings: Settings,
    rows: list[dict[str, Any]],
    *,
    today: datetime.date,
) -> int:
    """Give each open anticipation a provisional category from rules and memory.

    The same resolver as the filing cascade, short of the model: a rule the
    user declared first, then the user's own filed history plus Clerk's
    applied decisions under the same thresholds. A notification's merchant is
    looked up through its alias first (the bank's name for the shop is what
    the history was filed under), then under its own key. A taught category
    or approved category is left alone. Returns how many rows changed.
    """

    candidates = [row for row in rows if row.get("category_source") not in USER_SOURCES]
    if not candidates:
        return 0
    aliases = database.alias_map()
    rules = RuleBook.from_rows(database.active_rules())
    keys: set[str] = set()
    for row in candidates:
        key = str(row.get("merchant_key") or "")
        if key:
            keys.add(key)
            if key in aliases:
                keys.add(aliases[key])
    stored = []
    for key in keys:
        for entry in database.memory_for(key):
            stored.append(
                {
                    **entry,
                    "last_seen_date": datetime.datetime.fromtimestamp(
                        entry["last_seen"], datetime.UTC
                    ).date(),
                }
            )
    memory = build_memory(snapshot, stored, today=today)
    known = {
        str(category.get("id") or ""): str(category.get("name") or "")
        for category in snapshot.get("categories") or []
        if not category.get("is_income")
    }
    changed = 0
    for row in candidates:
        key = str(row.get("merchant_key") or "")
        match = (
            resolve(
                key,
                account_id=str(row.get("actual_account_id") or ""),
                rules=rules,
                memory=memory,
                aliases=aliases,
                min_observations=settings.memory_min_observations,
                min_confidence=settings.memory_min_confidence,
                allowed_categories=set(known),
                names=known,
            )
            if key
            else None
        )
        # A lone exact sighting is a suggestion for a person, not a category
        # to charge money against.
        if match is not None and not match.automatic:
            match = None
        if match is None:
            if row.get("category_id"):
                database.set_anticipated_category(
                    row["id"], category_id="", category_name="", source="", confidence=0.0
                )
                changed += 1
            continue
        source = SOURCE_RULE if match.is_rule else SOURCE_MEMORY
        if (
            row.get("category_id") != match.category_id
            or row.get("category_source") != source
        ):
            database.set_anticipated_category(
                row["id"],
                category_id=match.category_id,
                category_name=match.category_name,
                source=source,
                confidence=match.confidence,
            )
            changed += 1
    return changed


def apply_answer(
    database: Database,
    row: dict[str, Any],
    *,
    category_id: str,
    category_name: str,
    always: bool = False,
    correction: bool = False,
) -> dict[str, Any] | None:
    """The user answers a phone charge's review: this is where it goes.

    The charge counts against that category from now on, and the answer is
    evidence in memory the way an approved bank row is. With `always` it is
    also declared as a rule under the notification's key and, when the bank's
    name for the shop is known, under that key too -- so the real row files
    itself when it lands and the next notification is categorized on sight.
    Without it, only this charge (and the bank row it settles into) is filed.
    Returns the rule declared, if any.
    """

    database.set_anticipated_category(
        row["id"],
        category_id=category_id,
        category_name=category_name,
        source=SOURCE_TAUGHT if always else SOURCE_APPROVED,
        confidence=1.0,
    )
    key = str(row.get("merchant_key") or "")
    if not key:
        return None
    database.record_memory(key, category_id, category_name, correction=correction)
    if not always:
        return None
    label = str(row.get("merchant") or "")
    rule = database.upsert_rule(
        merchant_key=key, category_id=category_id, category_name=category_name,
        merchant_label=label, source="user",
    )
    database.close_proposals_for(key, kind="rule")
    target = database.alias_map().get(key)
    if target:
        database.upsert_rule(
            merchant_key=target, category_id=category_id, category_name=category_name,
            merchant_label=label, source="user",
        )
        database.close_proposals_for(target, kind="rule")
    return rule


def open_reviews(database: Database, rows: list[dict[str, Any]], settings: Settings) -> int:
    """Put every charge nothing has categorized into Review straight away.

    The model can take minutes on a large local server, and a charge must be
    answerable from the moment it is seen. So the review is opened at once,
    marked as still asking, and the phone job fills in the model's suggestion
    when it has one. Returns how many reviews are still waiting on the model,
    so the caller can make sure the phone job is queued.
    """

    asking = settings.categorization_enabled
    waiting = 0
    for row in rows:
        if row.get("kind") != KIND_CHARGE or row.get("category_id"):
            continue
        decision = database.phone_decision(str(row["id"]))
        if decision is not None:
            if decision["status"] == "needs_review" and decision["rationale"].get("asking"):
                waiting += 1
            continue
        source = database.get_notification_source(str(row.get("source_id") or "")) or {}
        database.add_decision(
            {
                "transaction_id": phone_transaction_id(str(row["id"])),
                "anticipated_id": row["id"],
                "account_id": str(row.get("actual_account_id") or ""),
                "account_name": str(source.get("account_name") or ""),
                "payee_name": str(row.get("merchant") or row.get("title") or ""),
                "merchant_key": str(row.get("merchant_key") or ""),
                "transaction_date": str(row.get("noticed_date") or ""),
                "amount_cents": int(row.get("amount_cents", 0)),
                "source": "unresolved",
                "status": "needs_review",
                "confidence": 0.0,
                "rationale": {
                    "reason": (
                        "Clerk is asking the model about this merchant."
                        if asking
                        else "Categorization is switched off, so Clerk has no suggestion."
                    ),
                    "asking": asking,
                    "phone": {"title": row.get("title") or "", "text": row.get("text") or ""},
                },
            }
        )
        waiting += int(asking)
    return waiting


def close_overtaken_reviews(database: Database) -> int:
    """Retire phone reviews that no longer need the person.

    A charge that expired, was dismissed, or stopped existing has nothing
    left to file; a charge a rule or reliable history has since categorized
    (the user made a rule on another row, say) no longer needs asking about.
    A settled charge's review was already handed to the bank's row.
    """

    closed = 0
    for decision in database.open_phone_decisions():
        charge = database.get_anticipated_charge(decision["anticipated_id"])
        reason = ""
        if charge is None:
            reason = "the charge is gone"
        elif charge["status"] != OPEN:
            reason = f"the charge was {charge['status']}"
        elif decision["status"] == "needs_review" and charge.get("category_id"):
            reason = f"filed by {charge.get('category_source') or 'Clerk'}"
        if reason and database.close_decision(decision["id"], "superseded", reason=reason):
            closed += 1
    return closed


def _carry_decision(database: Database, row: dict[str, Any], item: dict[str, Any]) -> bool:
    """Hand a settled charge's decision to the bank's row: one decision per purchase.

    An answer the user gave is carried as an approved decision the next
    filing run writes to Actual; a question still waiting, or one the user
    skipped, becomes the same question (or the same skip) about the bank's
    row. A bank row Clerk has already decided about keeps its own decision.
    Returns whether an answer was carried, so the caller can file it soon.
    """

    decision = database.phone_decision(str(row["id"]))
    transaction_id = str(item.get("id") or "")
    if decision is None or not transaction_id:
        return False
    database.note_decision(
        decision["id"],
        settled_transaction_id=transaction_id,
        settled_payee=str(item.get("payee_name") or ""),
    )
    bank = database.latest_decision_for(transaction_id)
    date = item.get("date")
    base = {
        "transaction_id": transaction_id,
        "account_id": str(item.get("account_id") or ""),
        "account_name": str(item.get("account_name") or ""),
        "payee_name": str(item.get("payee_name") or row.get("merchant") or ""),
        "merchant_key": str(item.get("merchant_key") or ""),
        "transaction_date": date.isoformat() if hasattr(date, "isoformat") else str(date or ""),
        "amount_cents": int(item.get("amount_cents", row.get("amount_cents", 0))),
    }
    if decision["status"] == "applied":
        if bank is not None and bank["status"] in ("applied", "approved"):
            return False
        database.add_decision(
            {
                **base,
                "source": SOURCE_PERSON,
                "status": "approved",
                "category_id": decision["category_id"],
                "category_name": decision["category_name"],
                "confidence": 1.0,
                "rationale": {
                    "reason": "You categorized this charge when your phone saw it.",
                    "from_phone": row["id"],
                    "phone_decision": decision["id"],
                    "phone_key": str(row.get("merchant_key") or ""),
                },
            }
        )
        return True
    database.close_decision(decision["id"], "superseded", reason="the bank posted it")
    if bank is not None and bank["status"] in ("applied", "approved", "needs_review", "skipped"):
        return False
    database.add_decision(
        {
            **base,
            "source": decision["source"],
            "status": decision["status"],
            "category_id": decision["category_id"],
            "category_name": decision["category_name"],
            "proposed_category": decision.get("proposed_category") or "",
            "confidence": decision["confidence"],
            "rationale": {**decision["rationale"], "from_phone": row["id"]},
        }
    )
    return False


def teach_alias(database: Database, row: dict[str, Any], *, payee: str) -> dict[str, Any] | None:
    """The user names the bank's payee for this notification's merchant."""

    key = str(row.get("merchant_key") or "")
    aliases = database.alias_map()
    # The bank's name may itself be an alias of the canonical key (a raw
    # descriptor the payee catalogue already maps); point at the canonical
    # key so the table never chains, and refuse to turn a target into an alias.
    target = canonical_key(normalize_merchant(payee), aliases)
    if not key or not target or key == target or key in set(aliases.values()):
        return None
    alias = database.upsert_alias(
        key,
        target,
        alias_label=str(row.get("merchant") or ""),
        merchant_label=payee,
        source=SOURCE_TAUGHT,
    )
    if alias and row.get("category_id") and row.get("category_source") == SOURCE_TAUGHT:
        _carry_rule(database, key, target, row)
    return alias


def _carry_rule(database: Database, key: str, target: str, row: dict[str, Any]) -> None:
    """A taught category is the user's word under the bank's key as well."""
    category_id = str(row.get("category_id") or "")
    category_name = str(row.get("category_name") or "")
    if not category_id or not target:
        return
    database.upsert_rule(
        merchant_key=target,
        category_id=category_id,
        category_name=category_name,
        merchant_label=str(row.get("matched_payee") or row.get("merchant") or ""),
        source="user",
    )
    database.close_proposals_for(target, kind="rule")
    database.record_memory(target, category_id, category_name, correction=True)


def _learn_from_settlement(
    database: Database,
    row: dict[str, Any],
    transaction: CandidateTransaction,
    aliases: dict[str, str],
) -> None:
    """What a settlement teaches: the bank's name for the shop, and a taught category."""

    charge_key = str(row.get("merchant_key") or "")
    # Learn the alias against the canonical key, so a bank descriptor the
    # payee catalogue already maps does not become a second hop; and never
    # turn a key other aliases point at into an alias itself.
    posted_key = canonical_key(transaction.merchant_key, aliases)
    chains = charge_key in set(aliases.values())
    if charge_key and posted_key and charge_key != posted_key and not chains:
        alias = database.upsert_alias(
            charge_key,
            posted_key,
            alias_label=str(row.get("merchant") or ""),
            merchant_label=transaction.payee_name,
        )
        if alias:
            aliases[charge_key] = alias["merchant_key"]
    if row.get("category_source") == SOURCE_TAUGHT and row.get("category_id") and posted_key:
        # The user said where this goes before the bank had a row for it. Say
        # so under the bank's key as well, so the filing run that follows this
        # sync files the real row the same way.
        _carry_rule(
            database, charge_key, posted_key, {**row, "matched_payee": transaction.payee_name}
        )


def public_charges(database: Database, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Charges with the state of their review, so the web can say what each one waits on."""

    waiting = {
        decision["anticipated_id"]: decision for decision in database.open_phone_decisions()
    }
    items = []
    for row in rows:
        item = public_charge(row)
        decision = waiting.get(str(row.get("id") or ""))
        item["review"] = (
            {
                "id": decision["id"],
                "status": decision["status"],
                "suggestion": decision.get("category_name") or "",
                "asking": bool(decision["rationale"].get("asking")),
            }
            if decision
            else None
        )
        items.append(item)
    return items


def public_charge(row: dict[str, Any]) -> dict[str, Any]:
    """The shape the overview, the web UI, and the phone all read."""

    item = dict(row)
    for field in ("noticed_at", "resolved_at", "created_at", "updated_at"):
        value = item.get(field)
        item[field] = (
            datetime.datetime.fromtimestamp(float(value), datetime.UTC).isoformat()
            if value
            else None
        )
    item["counts"] = item.get("status") == OPEN and item.get("kind") == KIND_CHARGE
    return item


def _as_charge(row: dict[str, Any]) -> AnticipatedCharge:
    try:
        noticed = datetime.date.fromisoformat(str(row.get("noticed_date") or ""))
    except ValueError:
        noticed = datetime.datetime.fromtimestamp(float(row["noticed_at"]), datetime.UTC).date()
    return AnticipatedCharge(
        id=str(row["id"]),
        account_id=str(row.get("actual_account_id") or ""),
        amount_cents=int(row.get("amount_cents", 0)),
        noticed_date=noticed,
        merchant_key=str(row.get("merchant_key") or ""),
    )
