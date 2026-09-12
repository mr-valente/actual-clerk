"""Carry what the lab Clerk learned from the phone over to another Clerk database.

The lab (`lab/compose.yml`) ran the phone companion for real: it registered
the card app as a notification source, recorded the charges the phone saw,
and was taught an alias or two. None of that can be re-derived from Actual,
so it is copied into the live Clerk database before the new image starts.

What moves, and only what cannot be re-learned from Actual:

- `notification_sources`: the phone's registration (keyed by the phone's own
  device id, so the app carries on without re-registering).
- `anticipated_charges`: every charge the phone forwarded, open or settled.
- `merchant_aliases` with source `taught`: the user's word. Aliases read from
  the payee catalogue are re-derived on the first filing run and are skipped.
- `merchant_rules` with source `user`: rules declared by hand in the lab.
  Rules imported from Actual are NOT copied: the live budget still holds
  those rules, and the Intelligence page imports them from the live Actual
  itself (docs/intelligence/stage-2.md), so Clerk's record of what is in
  Actual stays true.
- The phone-app settings (`anticipated_*`, including the device token), so
  the phone only needs its server URL changed.

Everything is inserted by primary key and skipped when already present, so
the script can be run twice. A category or account the target budget does
not know is dropped from the copied row, not invented.

    uv run python scripts/transfer-lab-intelligence.py \
        --source lab/data/clerk/clerk.db \
        --target "$MNT_HOME/.local/share/actual-budget/clerk/data/clerk.db" \
        --dry-run

The target Clerk MUST be stopped: SQLite cannot share a database across
hosts. Pass --i-stopped-clerk to say so. A consistent backup of the target is
written beside it first.

Do not point --target at a database on an SMB/CIFS share: SQLite's locking
and backup calls hang there. Copy the file to local disk (with Clerk stopped
its write-ahead log is empty, so a plain copy is consistent), run this
against the copy, and copy the result back; see docs/going-live.md.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from actual_clerk.db import Database  # noqa: E402

SETTINGS_TO_COPY = (
    "anticipated_enabled",
    "anticipated_device_token",
    "anticipated_match_window_days",
    "anticipated_expire_days",
)


def connect(path: Path, *, readonly: bool) -> sqlite3.Connection:
    uri = f"file:{path}?mode={'ro' if readonly else 'rw'}"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def rows(connection: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


def backup(target: Path) -> Path:
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    folder = target.parent.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / f"clerk-before-lab-transfer-{stamp}.db"
    source = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    copy = sqlite3.connect(destination)
    with copy:
        source.backup(copy)
    copy.close()
    source.close()
    return destination


def insert_missing(connection: sqlite3.Connection, table: str, items: list[dict], key: str) -> int:
    inserted = 0
    for item in items:
        columns = list(item)
        placeholders = ",".join("?" for _ in columns)
        cursor = connection.execute(
            f"INSERT OR IGNORE INTO {table}({','.join(columns)}) VALUES({placeholders})",
            [item[column] for column in columns],
        )
        inserted += cursor.rowcount
    return inserted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", required=True, type=Path, help="the lab clerk.db")
    parser.add_argument("--target", required=True, type=Path, help="the clerk.db to copy into")
    parser.add_argument("--dry-run", action="store_true", help="report what would move; write nothing")
    parser.add_argument(
        "--i-stopped-clerk",
        action="store_true",
        help="confirm no Clerk process has the target open (required to write)",
    )
    args = parser.parse_args()

    if not args.source.exists():
        parser.error(f"source database not found: {args.source}")
    if not args.target.exists():
        parser.error(f"target database not found: {args.target}")
    if not args.dry_run and not args.i_stopped_clerk:
        parser.error("refusing to write while Clerk may be running; stop it and pass --i-stopped-clerk")

    lab = connect(args.source, readonly=True)
    sources = rows(lab, "SELECT * FROM notification_sources ORDER BY created_at")
    charges = rows(lab, "SELECT * FROM anticipated_charges ORDER BY noticed_at")
    aliases = rows(lab, "SELECT * FROM merchant_aliases WHERE source='taught' ORDER BY created_at")
    rules = rows(lab, "SELECT * FROM merchant_rules WHERE source='user' ORDER BY created_at")
    lab_settings = json.loads(
        (lab.execute("SELECT value FROM settings WHERE key='runtime'").fetchone() or {"value": "{}"})["value"]
    )
    lab.close()

    # What the target budget knows, from the overview Clerk last built there.
    peek = connect(args.target, readonly=True)
    snapshot_row = peek.execute("SELECT payload_json FROM snapshots WHERE key='overview'").fetchone()
    target_settings = json.loads(
        (peek.execute("SELECT value FROM settings WHERE key='runtime'").fetchone() or {"value": "{}"})["value"]
    )
    peek.close()
    snapshot = json.loads(snapshot_row["payload_json"]) if snapshot_row else {}
    known_accounts = {a["id"]: a["name"] for a in snapshot.get("accounts") or []}
    known_categories = {c["id"]: c["name"] for c in snapshot.get("categories") or []}

    print(f"Source: {args.source}\nTarget: {args.target}\n")
    print(f"Target budget snapshot knows {len(known_accounts)} account(s) and {len(known_categories)} categories.")

    for source in sources:
        name = known_accounts.get(source["actual_account_id"])
        note = f"→ {name}" if name else "→ ACCOUNT NOT IN TARGET SNAPSHOT (the source will show as unlinked)"
        print(f"  source   {source['app_label']} on {source['device_name']} {note}")
    for charge in charges:
        category = charge.get("category_id") or ""
        if category and category not in known_categories and known_categories:
            charge["category_id"] = ""
            charge["category_name"] = ""
            charge["category_source"] = ""
            charge["category_confidence"] = 0.0
            note = " (category not in target; cleared, Clerk re-derives it)"
        elif charge.get("category_source") in ("rule", "memory"):
            note = " (provisional; Clerk re-derives it from live rules and history)"
        else:
            note = ""
        print(
            f"  charge   {charge['noticed_date']} {charge['merchant'] or charge['title']!r} "
            f"{charge['amount_cents'] / 100:+.2f} [{charge['status']}] {charge.get('category_name') or '-'}{note}"
        )
    for alias in aliases:
        print(f"  alias    {alias['alias_key']} → {alias['merchant_key']} ({alias['source']})")
    for rule in rules:
        name = known_categories.get(rule["category_id"])
        if not name and known_categories:
            print(f"  rule     {rule['merchant_key']} → {rule['category_name']}: CATEGORY NOT IN TARGET, skipped")
            rule["skip"] = True
        else:
            print(f"  rule     {rule['merchant_key']} → {rule['category_name']} (made by hand in the lab)")
    rules = [rule for rule in rules if not rule.get("skip")]

    settings_changes = {}
    for key in SETTINGS_TO_COPY:
        if key in lab_settings and key not in target_settings:
            settings_changes[key] = lab_settings[key]
    for key, value in settings_changes.items():
        shown = "<set>" if "token" in key else value
        print(f"  setting  {key} = {shown}")

    if args.dry_run:
        print("\nDry run: nothing written.")
        return 0

    saved = backup(args.target)
    print(f"\nBackup written to {saved}")

    # Bring the target up to the current schema (creates the new tables), then copy.
    Database(args.target).initialize()
    live = connect(args.target, readonly=False)
    with live:
        live.execute("BEGIN IMMEDIATE")
        n_sources = insert_missing(live, "notification_sources", sources, "id")
        n_charges = insert_missing(live, "anticipated_charges", charges, "id")
        n_aliases = insert_missing(live, "merchant_aliases", aliases, "alias_key")
        n_rules = insert_missing(live, "merchant_rules", rules, "id")
        if settings_changes:
            merged = {**target_settings, **settings_changes}
            live.execute(
                "INSERT INTO settings(key,value,updated_at) VALUES('runtime',?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (json.dumps(merged), datetime.datetime.now(datetime.UTC).timestamp()),
            )
    live.close()
    print(
        f"Copied {n_sources} source(s), {n_charges} charge(s), {n_aliases} alias(es), "
        f"{n_rules} rule(s), {len(settings_changes)} setting(s)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
