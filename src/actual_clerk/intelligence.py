"""Taking the simple rules over from Actual, and giving them back.

Three explicit steps, each reversible and each with a dry run:

1. **Import** copies Actual's simple payee-to-category rules into Clerk's
   rule table, with the original rule stored verbatim. Actual is untouched,
   and both systems answer; Actual answers first at import, so Clerk sees
   categorized rows and applies nothing.
2. **Retire** deletes each imported rule from Actual by id. From here
   Actual's imports arrive uncategorized and the categorize job that
   follows every sync files them by Clerk's rule.
3. **Restore** recreates a retired rule in Actual from the stored copy.

Nothing here decides on its own; every step is asked for from the
Intelligence page.
"""

from __future__ import annotations

import datetime
import json
import logging
from typing import Any

from actual_clerk.clients.actual import ActualGateway
from actual_clerk.db import Database
from actual_clerk.domain.actual_rules import DISPOSITION_MOVE, classify_rules, replay

log = logging.getLogger(__name__)

SOURCE_IMPORTED = "imported"
ACTUAL_PRESENT = "present"
ACTUAL_RETIRED = "retired"
ACTUAL_RESTORED = "restored"


async def read_actual(
    gateway: ActualGateway, database: Database, *, today: datetime.date, snapshot: dict[str, Any]
) -> dict[str, Any]:
    """What Actual's rule table holds, classified and replayed, beside what Clerk manages."""

    rules = await gateway.list_rules()
    payees = {str(item["id"]): item for item in await gateway.list_payees()}
    accounts = {str(item["id"]): str(item["name"]) for item in snapshot.get("accounts") or []}
    categories = {str(item["id"]): str(item["name"]) for item in snapshot.get("categories") or []}
    classified = classify_rules(rules, payees=payees, accounts=accounts, categories=categories)
    replay(classified, snapshot.get("transactions") or [], categories=categories)

    managed = _managed_by_actual_rule(database)
    live = {
        (rule["merchant_key"], rule["account_id"]): rule
        for rule in database.list_rules(statuses=("active", "paused"))
    }
    entries = []
    for item in classified:
        entry = item.as_dict()
        entry["managed"] = [
            {"id": rule["id"], "status": rule["status"], "actual_status": rule["actual_status"]}
            for rule in managed.get(item.id, [])
        ]
        for translation in entry["translations"]:
            existing = live.get((translation["merchant_key"], translation["account_id"]))
            if existing is None:
                translation["existing"] = None
            else:
                translation["existing"] = {
                    "id": existing["id"],
                    "category_id": existing["category_id"],
                    "category_name": existing["category_name"],
                    "agrees": existing["category_id"] == translation["category_id"],
                }
                if not translation["problem"] and not translation["existing"]["agrees"]:
                    translation["problem"] = (
                        f"Clerk already files this merchant as {existing['category_name']}"
                    )
        entries.append(entry)

    retired = [
        rule
        for rule in database.list_rules(statuses=("active", "paused", "retired"))
        if rule["actual_status"] == ACTUAL_RETIRED
    ]
    movable = [
        entry
        for entry in entries
        if entry["disposition"] == DISPOSITION_MOVE and not entry["managed"]
    ]
    return {
        "read_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "today": today.isoformat(),
        "rules": entries,
        "counts": {
            "in_actual": len(entries),
            "movable": len(movable),
            "kept": sum(1 for entry in entries if entry["disposition"] != DISPOSITION_MOVE),
            "imported_present": sum(1 for entry in entries if entry["managed"]),
            "retired": len({rule["actual_rule_id"] for rule in retired}),
        },
    }


def _managed_by_actual_rule(database: Database) -> dict[str, list[dict[str, Any]]]:
    managed: dict[str, list[dict[str, Any]]] = {}
    for rule in database.list_rules(statuses=("active", "paused", "retired")):
        if rule["actual_rule_id"]:
            managed.setdefault(rule["actual_rule_id"], []).append(rule)
    return managed


async def import_rules(
    gateway: ActualGateway,
    database: Database,
    *,
    snapshot: dict[str, Any],
    today: datetime.date,
    rule_ids: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Copy simple Actual rules into Clerk; Actual itself is untouched."""

    reading = await read_actual(gateway, database, today=today, snapshot=snapshot)
    wanted = set(rule_ids or [])
    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    raw_by_id = {str(rule.get("id")): rule for rule in await gateway.list_rules()}
    for entry in reading["rules"]:
        if entry["disposition"] != DISPOSITION_MOVE:
            continue
        if wanted and entry["id"] not in wanted:
            continue
        if entry["managed"]:
            continue
        stored = json.dumps(raw_by_id.get(entry["id"], {}), separators=(",", ":"))
        for translation in entry["translations"]:
            record = {
                "actual_rule_id": entry["id"],
                "merchant_key": translation["merchant_key"],
                "merchant_label": translation["merchant_label"],
                "category_id": translation["category_id"],
                "category_name": translation["category_name"],
                "account_id": translation["account_id"],
            }
            if translation["problem"]:
                skipped.append({**record, "reason": translation["problem"]})
                continue
            existing = translation.get("existing")
            if dry_run:
                imported.append({**record, "already_in_clerk": bool(existing)})
                continue
            if existing:
                database.update_rule(
                    existing["id"],
                    actual_rule_id=entry["id"],
                    actual_rule_json=stored,
                    actual_status=ACTUAL_PRESENT,
                )
                imported.append({**record, "already_in_clerk": True, "id": existing["id"]})
                continue
            rule = database.upsert_rule(
                merchant_key=translation["merchant_key"],
                category_id=translation["category_id"],
                category_name=translation["category_name"],
                account_id=translation["account_id"],
                merchant_label=translation["merchant_label"],
                source=SOURCE_IMPORTED,
                actual_rule_id=entry["id"],
                actual_rule_json=stored,
                actual_status=ACTUAL_PRESENT,
            )
            if rule:
                database.close_proposals_for(rule["merchant_key"], kind="rule")
                imported.append({**record, "already_in_clerk": False, "id": rule["id"]})
    if not dry_run and imported:
        log.info("Imported %d rule(s) from Actual into Clerk", len(imported))
    return {"dry_run": dry_run, "imported": imported, "skipped": skipped}


def _retirable(database: Database) -> dict[str, list[dict[str, Any]]]:
    """Actual rules Clerk has imported and every Clerk rule for them is active."""
    groups = _managed_by_actual_rule(database)
    ready: dict[str, list[dict[str, Any]]] = {}
    for actual_id, rules in groups.items():
        if all(
            rule["actual_status"] in (ACTUAL_PRESENT, ACTUAL_RESTORED) for rule in rules
        ) and all(rule["status"] == "active" for rule in rules):
            ready[actual_id] = rules
    return ready


async def retire_rules(
    gateway: ActualGateway,
    database: Database,
    *,
    rule_ids: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Delete imported rules from Actual, now that Clerk answers for them."""

    wanted = set(rule_ids or [])
    retired: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for actual_id, rules in _retirable(database).items():
        if wanted and actual_id not in wanted:
            continue
        record = {
            "actual_rule_id": actual_id,
            "merchants": [rule["merchant_label"] or rule["merchant_key"] for rule in rules],
        }
        if dry_run:
            retired.append(record)
            continue
        try:
            await gateway.delete_rule(actual_id)
        except Exception as exc:  # noqa: BLE001 - one failure must not stop the rest
            failed.append({**record, "error": str(exc)})
            continue
        for rule in rules:
            database.update_rule(rule["id"], actual_status=ACTUAL_RETIRED)
        retired.append(record)
    if not dry_run and retired:
        log.info("Retired %d rule(s) in Actual", len(retired))
    return {"dry_run": dry_run, "retired": retired, "failed": failed}


async def restore_rules(
    gateway: ActualGateway,
    database: Database,
    *,
    rule_ids: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Recreate retired rules in Actual from the copies Clerk kept."""

    wanted = set(rule_ids or [])
    restored: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for actual_id, rules in _managed_by_actual_rule(database).items():
        if wanted and actual_id not in wanted:
            continue
        if not all(rule["actual_status"] == ACTUAL_RETIRED for rule in rules):
            continue
        stored = next((rule["actual_rule_json"] for rule in rules if rule["actual_rule_json"]), "")
        record = {
            "actual_rule_id": actual_id,
            "merchants": [rule["merchant_label"] or rule["merchant_key"] for rule in rules],
        }
        if not stored:
            failed.append({**record, "error": "no copy of the original rule was kept"})
            continue
        if dry_run:
            restored.append(record)
            continue
        try:
            created = await gateway.restore_rule(json.loads(stored))
        except Exception as exc:  # noqa: BLE001
            failed.append({**record, "error": str(exc)})
            continue
        new_id = str(created.get("id") or actual_id)
        for rule in rules:
            database.update_rule(
                rule["id"], actual_status=ACTUAL_RESTORED, actual_rule_id=new_id
            )
        restored.append({**record, "new_actual_rule_id": new_id})
    if not dry_run and restored:
        log.info("Restored %d rule(s) to Actual", len(restored))
    return {"dry_run": dry_run, "restored": restored, "failed": failed}

