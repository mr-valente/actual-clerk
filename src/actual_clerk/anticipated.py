"""Keep anticipated charges in step with what Actual actually holds.

Runs every time the overview is rebuilt -- after a sync, a filing run, a
connection check, the morning report, or a manual refresh -- so an
anticipation is settled the moment the bank's row is read, whichever job
happened to read it. Nothing here writes to Actual; the only state that
changes is Clerk's own ledger of purchases seen before the bank posted them.

That ledger has two origins. The phone announces a purchase the moment the
card authorises it. The bank feed itself can announce one too, and then take
it back: a pending charge withdrawn without its posted transaction, which
some banks post hours or days later. Such a charge is *held* -- counted as
spent the way its pending row was filed -- until the posted row arrives and
inherits that category, or until it has evidently been released.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import time
from collections import Counter
from collections.abc import Sequence
from typing import Any

from actual_clerk.categorize import build_memory
from actual_clerk.config import Settings
from actual_clerk.db import Database, pending_key
from actual_clerk.domain.anticipated import (
    KIND_CHARGE,
    KIND_CREDIT,
    AnticipatedCharge,
    CandidateTransaction,
    ChargeMatch,
    expired_charges,
    match_charges,
    parse_notification,
    same_merchant,
)
from actual_clerk.domain.intelligence import SOURCE_RULE, RuleBook, canonical_key, resolve
from actual_clerk.domain.merchants import normalize_merchant
from actual_clerk.domain.plaid_import import short_payee

log = logging.getLogger(__name__)

# Where a provisional category came from. An approved one is the user's answer
# for this one charge (Apply in Review); a taught one is the user's answer made
# into a rule (Always, or the old "Always file as..."). Neither is overwritten
# by memory. A rule is the user's word too, read from the rule book; a memory
# one is re-derived on every pass so it follows the evidence.
SOURCE_MEMORY = "memory"
SOURCE_TAUGHT = "taught"
SOURCE_APPROVED = "approved"
# A withdrawn pending charge's own category, as its row held it in Actual.
SOURCE_PENDING = "pending"
# SOURCE_RULE is shared with the resolver.
USER_SOURCES = (SOURCE_TAUGHT, SOURCE_APPROVED)
# Categories that memory never re-derives: the person's, and the bank row's.
FIXED_SOURCES = (*USER_SOURCES, SOURCE_PENDING)

# The decision source for a bank row filed with the user's answer about the
# phone charge it settled: the person decided, before the bank had a row.
SOURCE_PERSON = "person"
# The decision source for a posted row filed the way its withdrawn pending
# row was: the same purchase keeps its category.
SOURCE_HELD = "pending"

# Who saw a purchase before the bank posted it.
ORIGIN_PHONE = "phone"
ORIGIN_PENDING = "pending"

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


def hold_withdrawn(
    database: Database,
    settings: Settings,
    *,
    actual_account_id: str,
    label: str,
    deletions: Sequence[dict[str, Any]],
) -> int:
    """Hold the pending charges the bank withdrew, so they count until they post.

    Each deletion is a row Clerk just removed from Actual because the bank
    took its pending transaction back. Most come straight back as posted in
    the same read and never get here; these did not. The hold carries the
    row's own category, so the posted row is filed the same way when it
    lands, under whatever name the bank gives it then. Money coming in, a
    transfer, and a split row are not held: only plain spending competes
    for the budget, and a split's categories live on rows already gone.
    Returns how many holds were opened.
    """

    if settings.plaid_hold_withdrawn_days <= 0:
        return 0
    held = 0
    now = time.time()
    for item in deletions:
        amount = int(item.get("amount_cents") or 0)
        imported_id = str(item.get("imported_id") or "")
        date = item.get("date")
        if (
            amount >= 0
            or not imported_id
            or date is None
            or item.get("is_transfer")
            or item.get("is_parent")
        ):
            continue
        payee = str(item.get("payee_name") or "")
        description = str(item.get("imported_description") or "")
        category_id = str(item.get("category_id") or "")
        _, created = database.hold_withdrawn_pending(
            {
                "actual_account_id": actual_account_id,
                "notification_key": pending_key(imported_id),
                "amount_cents": amount,
                "merchant": payee or short_payee(description),
                "merchant_key": normalize_merchant(payee, description),
                "title": label,
                "text": description or payee,
                "noticed_at": now,
                "noticed_date": date.isoformat() if hasattr(date, "isoformat") else str(date),
                "category_id": category_id,
                "category_source": SOURCE_PENDING if category_id else "",
                "category_confidence": 1.0 if category_id else 0.0,
            }
        )
        held += int(created)
    return held


def is_phone(row: dict[str, Any]) -> bool:
    return str(row.get("origin") or ORIGIN_PHONE) == ORIGIN_PHONE


def live_rows(rows: Sequence[dict[str, Any]], settings: Settings) -> list[dict[str, Any]]:
    """The anticipations the settings let count: the phone's, and the bank's held ones."""
    return [
        row
        for row in rows
        if (settings.anticipated_enabled if is_phone(row) else settings.plaid_hold_withdrawn_days > 0)
    ]


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

    def still_open() -> list[dict[str, Any]]:
        return live_rows(database.list_anticipated_charges(status=OPEN, limit=1000), settings)

    names = {
        str(account.get("id") or ""): str(account.get("name") or "")
        for account in snapshot.get("accounts") or []
    }
    open_rows = still_open()
    summary: dict[str, Any] = {"open": [], "matched": 0, "expired": 0, "categorized": 0}
    if not open_rows:
        close_overtaken_reviews(database)
        summary["open"] = public_charges(database, open_rows, account_names=names)
        return summary

    _name_held_categories(database, snapshot, open_rows)
    aliases = database.alias_map()
    items = {str(item.get("id") or ""): item for item in snapshot.get("transactions") or []}
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
    by_id = {row["id"]: row for row in open_rows}
    by_transaction = {candidate.id: candidate for candidate in candidates}
    used = database.matched_transaction_ids()
    # A held charge whose posted transaction the bank has named settles
    # against that row by identity, whatever amount it posted for.
    matches = _posted_matches(open_rows, candidates, snapshot, used)
    used |= {match.transaction_id for match in matches}
    named = {match.charge_id for match in matches}
    # A hold is never its own pending row: a refresh that read Actual just
    # before the sync deleted that row must not settle the hold against it.
    own_rows: dict[str, set[str]] = {}
    for item in snapshot.get("transactions") or []:
        if item.get("imported_id"):
            own_rows.setdefault(pending_key(str(item["imported_id"])), set()).add(str(item["id"]))
    matches += match_charges(
        [
            _as_charge(
                row,
                also_rejected=(
                    set() if is_phone(row) else own_rows.get(str(row.get("notification_key")), set())
                ),
            )
            for row in open_rows
            if row["id"] not in named
        ],
        candidates,
        window_days=settings.anticipated_match_window_days,
        used_transaction_ids=used,
        aliases=aliases,
    )
    for match in matches:
        row = by_id[match.charge_id]
        if not database.resolve_anticipated_charge(
            match.charge_id,
            MATCHED,
            matched_transaction_id=match.transaction_id,
            matched_payee=match.payee_name,
            matched_date=match.date.isoformat(),
            match_reason=match.reason,
        ):
            continue
        summary["matched"] += 1
        item = items.get(match.transaction_id) or {}
        if not is_phone(row):
            carried = _carry_held(database, row, item)
        else:
            _learn_from_settlement(database, row, by_transaction[match.transaction_id], aliases)
            carried = _carry_decision(database, row, item)
        if carried:
            summary["carried"] = summary.get("carried", 0) + 1
    settled = {match.charge_id for match in matches}
    waiting = [row for row in open_rows if row["id"] not in settled]
    for charge_id in [
        *expired_charges(
            [_as_charge(row) for row in waiting if is_phone(row)],
            today=today,
            expire_days=settings.anticipated_expire_days,
        ),
        *_released_holds(
            [row for row in waiting if not is_phone(row)], settings, today=today
        ),
    ]:
        if database.resolve_anticipated_charge(charge_id, EXPIRED, match_reason="never posted"):
            summary["expired"] += 1
    if summary["matched"] or summary["expired"]:
        log.info(
            "Anticipated charges: %d settled against Actual, %d expired unposted",
            summary["matched"],
            summary["expired"],
        )
    summary["categorized"] = classify(database, snapshot, settings, still_open(), today=today)
    close_overtaken_reviews(database)
    summary["asking"] = open_reviews(database, still_open(), settings)
    summary["open"] = public_charges(database, still_open(), account_names=names)
    return summary


def _posted_matches(
    rows: Sequence[dict[str, Any]],
    candidates: Sequence[CandidateTransaction],
    snapshot: dict[str, Any],
    used: set[str],
) -> list[ChargeMatch]:
    """Held charges whose posted row is in Actual under the id the bank gave it."""
    by_imported = {
        str(item.get("imported_id") or ""): str(item.get("id") or "")
        for item in snapshot.get("transactions") or []
        if item.get("imported_id") and not item.get("is_child")
    }
    by_id = {candidate.id: candidate for candidate in candidates}
    matches = []
    for row in rows:
        posted = str(row.get("posted_imported_id") or "")
        candidate = by_id.get(by_imported.get(posted, "")) if posted else None
        rejected = set(str(row.get("rejected_transaction_ids") or "").split(","))
        # A row the person reopened the hold from is not its posted row,
        # whatever the bank says.
        if is_phone(row) or candidate is None or candidate.id in used or candidate.id in rejected:
            continue
        used.add(candidate.id)
        matches.append(
            ChargeMatch(
                charge_id=str(row["id"]),
                transaction_id=candidate.id,
                reason="posted by the bank",
                payee_name=candidate.payee_name,
                date=candidate.date,
            )
        )
    return matches


def _released_holds(
    rows: Sequence[dict[str, Any]], settings: Settings, *, today: datetime.date
) -> list[str]:
    """Held charges the bank has evidently released rather than posted.

    Counted from the day the bank withdrew the charge, not the day it was
    made: a pending charge can sit for days before being taken back. A hold
    the person reopened counts from then instead, so Reopen is not undone
    on the next refresh.
    """

    released = []
    for row in rows:
        since = max(float(row["noticed_at"]), float(row.get("updated_at") or 0))
        withdrawn = datetime.datetime.fromtimestamp(since, settings.zone).date()
        if (today - withdrawn).days > settings.plaid_hold_withdrawn_days:
            released.append(str(row["id"]))
    return released


def _name_held_categories(
    database: Database, snapshot: dict[str, Any], rows: Sequence[dict[str, Any]]
) -> None:
    """Give a held charge its category's name; the sync that held it knew only the id."""
    known = {
        str(category.get("id") or ""): str(category.get("name") or "")
        for category in snapshot.get("categories") or []
    }
    for row in rows:
        category_id = str(row.get("category_id") or "")
        if is_phone(row) or not category_id or row.get("category_name"):
            continue
        name = known.get(category_id, "")
        if name:
            database.set_anticipated_category(
                row["id"],
                category_id=category_id,
                category_name=name,
                source=str(row.get("category_source") or SOURCE_PENDING),
                confidence=float(row.get("category_confidence") or 1.0),
            )
            row["category_name"] = name


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
            {
                "charge_id": row["id"],
                "transaction_id": item["id"],
                "date": noticed,
                "from_date": item.get("date"),
            }
        )
    return updates, finished


def date_restoration(charge: dict[str, Any]) -> dict[str, Any] | None:
    """What putting a settled row back on the bank's date takes, when this charge moved it."""

    if (
        charge.get("status") != MATCHED
        or not charge.get("date_carried")
        or not charge.get("matched_transaction_id")
        or not charge.get("matched_date")
        or charge.get("matched_date") == charge.get("noticed_date")
    ):
        return None
    return {
        "transaction_id": charge["matched_transaction_id"],
        "date": charge["matched_date"],
        "from_date": charge["noticed_date"],
    }


def bank_name_unusable(item: dict[str, Any]) -> bool:
    """Whether a bank row names no merchant Clerk can read.

    Capital One's feed through Plaid replaces every word that mixes letters
    and digits with asterisks, so `PP*SPOTIFY*P46453222D` arrives as 21
    asterisks and `7-ELEVEN 36883` as `*-****** 36883`. Neither leaves a
    merchant key; a row whose payee or bank line does is left alone.
    """

    if item.get("merchant_key"):
        return False
    return not normalize_merchant(
        str(item.get("payee_name") or ""), str(item.get("imported_description") or "")
    )


def phone_payee(
    charge: dict[str, Any], transactions: list[dict[str, Any]], aliases: dict[str, str]
) -> str:
    """The payee a settled charge's merchant goes by in this budget.

    The name the budget already files the merchant under when it has one, so
    the row reads like the rest of its history and meets the same rules and
    memory; otherwise the notification's own name, tidied.
    """

    key = str(charge.get("merchant_key") or "")
    if not key:
        return ""
    names = Counter(
        str(item.get("payee_name") or "").strip()
        for item in transactions
        if item.get("payee_name")
        and not item.get("is_transfer")
        and same_merchant(key, str(item.get("merchant_key") or ""), aliases)
    )
    if names:
        return names.most_common(1)[0][0]
    return short_payee(str(charge.get("merchant") or ""))


def name_corrections(
    database: Database, snapshot: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Bank rows a phone charge settled whose bank sent no readable name.

    The phone's notification is Capital One's own record of the same
    purchase, already trusted for the day it happened; where the bank's line
    arrived masked it names the merchant too. Each settled charge is looked
    at once. Returns the renames to write (with the charge each belongs to
    and the bank's name, so a Reopen can put it back) and the charges that
    need nothing: their row names its merchant, is gone, or is not a plain
    purchase.
    """

    waiting = database.matched_charge_names(uncarried_only=True)
    if not waiting:
        return [], []
    transactions = list(snapshot.get("transactions") or [])
    items = {str(item.get("id") or ""): item for item in transactions}
    aliases = database.alias_map()
    updates: list[dict[str, Any]] = []
    finished: list[str] = []
    for row in waiting:
        item = items.get(str(row["matched_transaction_id"]))
        name = ""
        if (
            item is not None
            and not item.get("is_transfer")
            and not item.get("is_child")
            and bank_name_unusable(item)
        ):
            name = phone_payee(row, transactions, aliases)
        if not name:
            finished.append(row["id"])
            continue
        updates.append(
            {
                "charge_id": row["id"],
                "transaction_id": item["id"],
                "payee_name": name,
                "from_payee": str(item.get("payee_name") or ""),
            }
        )
    return updates, finished


def name_restoration(charge: dict[str, Any]) -> dict[str, Any] | None:
    """What putting the bank's own name back on a settled row takes, when this charge renamed it."""

    if (
        charge.get("status") != MATCHED
        or not charge.get("matched_transaction_id")
        or not charge.get("renamed_to")
        or not charge.get("replaced_payee")
    ):
        return None
    return {
        "transaction_id": charge["matched_transaction_id"],
        "payee_name": charge["replaced_payee"],
        "from_payee": charge["renamed_to"],
    }


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
    the history was filed under), then under its own key. A taught or
    approved category is left alone, and so is the category a withdrawn
    pending charge's own row held. Returns how many rows changed.
    """

    candidates = [row for row in rows if row.get("category_source") not in FIXED_SOURCES]
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
        # A held pending charge is not asked about: its posted row will be,
        # if it has nothing to inherit.
        if row.get("kind") != KIND_CHARGE or row.get("category_id") or not is_phone(row):
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


def _carry_held(database: Database, row: dict[str, Any], item: dict[str, Any]) -> bool:
    """File a posted row the way its withdrawn pending row was filed.

    The two are one purchase, so this is not a guess about the merchant: it
    is the same carry Plaid's own pending-to-posted link gets, made across
    the gap the bank left between them. Only the category the pending row
    itself held is carried; one memory or a rule gave the hold since is the
    posted row's own filing run to find. The answer goes in as an approved
    decision, which the next filing run writes and learns from under the
    posted row's name. A row already categorized, already decided, or
    skipped by the person keeps what it has; one still waiting in Review
    is answered.
    """

    transaction_id = str(item.get("id") or "")
    category_id = str(row.get("category_id") or "")
    if (
        not transaction_id
        or not category_id
        or row.get("category_source") != SOURCE_PENDING
        or item.get("category_id")
        or item.get("is_transfer")
    ):
        return False
    bank = database.latest_decision_for(transaction_id)
    if bank is not None and bank["status"] in ("applied", "approved", "skipped"):
        return False
    date = item.get("date")
    database.add_decision(
        {
            "transaction_id": transaction_id,
            "account_id": str(item.get("account_id") or ""),
            "account_name": str(item.get("account_name") or ""),
            "payee_name": str(item.get("payee_name") or row.get("merchant") or ""),
            "merchant_key": str(item.get("merchant_key") or ""),
            "transaction_date": date.isoformat() if hasattr(date, "isoformat") else str(date or ""),
            "amount_cents": int(item.get("amount_cents", row.get("amount_cents", 0))),
            "source": SOURCE_HELD,
            "status": "approved",
            "category_id": category_id,
            "category_name": str(row.get("category_name") or ""),
            "confidence": 1.0,
            "rationale": {
                "reason": (
                    "The bank withdrew this charge while it was pending and posted it again; "
                    "it is filed the way its pending row was."
                ),
                "from_pending": row["id"],
                "pending_payee": str(row.get("merchant") or ""),
                "pending_text": str(row.get("text") or ""),
                "earlier_key": str(row.get("merchant_key") or ""),
            },
        }
    )
    return True


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


def public_charges(
    database: Database,
    rows: list[dict[str, Any]],
    *,
    account_names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Charges with the state of their review, so the web can say what each one waits on."""

    waiting = {
        decision["anticipated_id"]: decision for decision in database.open_phone_decisions()
    }
    names = account_names or {}
    items = []
    for row in rows:
        item = public_charge(row)
        account = names.get(str(row.get("actual_account_id") or ""))
        if account:
            item["account_name"] = account
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


def _as_charge(row: dict[str, Any], *, also_rejected: set[str] | None = None) -> AnticipatedCharge:
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
        rejected=frozenset(
            filter(None, str(row.get("rejected_transaction_ids") or "").split(","))
        )
        | frozenset(also_rejected or ()),
    )
