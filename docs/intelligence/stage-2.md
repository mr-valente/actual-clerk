# Stage 2: taking the rules over from Actual

Checkpoint for the second stage of [the plan](plan.md). Clerk can now read
Actual's rule table, say which rules can move and which stay, replay each
translation over the retained history, copy the simple rules into its own
table, delete them from Actual, and put them back.

## What changed

**The worker** gains four methods, each pinned by a contract test:
`listRules` (every rule as the API serializes it: `id`, `stage`,
`conditions_op`, `conditions`, `actions`), `listPayees` (id, name, and the
transfer account for a transfer payee), `deleteRule`, and `restoreRule`
(recreates a rule from a stored copy through `createRule`). Against the
real budget the API presents condition fields under their public names
(`payee`, `account`, `imported_payee`) whatever the rule's age; the Python
side accepts the internal column names too.

**The classifier** (`domain/actual_rules.py`). A rule *moves* when it is in
the default stage, its only action sets a category, and every condition is
on the payee or the imported payee with `is`, `oneOf`, or `contains`,
optionally narrowed by one account condition under `and`. Everything else
is *kept*, with the reason in words: sets the payee, deletes the
transaction, depends on the amount, combines account and payee with `or`,
runs in another stage. A moving rule becomes one Clerk rule per payee it
names, keyed on the normalized merchant key, scoped to each account the
rule names. A translation can carry a *problem* that blocks it without
blocking the rule's other translations: the payee no longer exists, it is a
transfer payee, the name normalizes to nothing, the category is gone, or it
collides with another rule claiming the same key with a different category.

**Replay.** Every translation is run over the categorized history in the
snapshot: how many rows its key claims (on its account, or on any), how
many Actual filed in the same category, and where the rest went. This is
what makes the switch from Actual's `payee is` to Clerk's normalized key
safe to look at before it is made.

**Three steps** (`intelligence.py`), each with a dry run:

- *Import* copies the movable rules into `merchant_rules` with
  `source = imported`, the Actual rule id, the rule stored verbatim, and
  `actual_status = present`. A Clerk rule that already files the merchant
  the same way is adopted (it gains the Actual rule id and keeps its own
  source); one that files it differently blocks that translation. Actual is
  not touched. An open rule proposal for an imported merchant is withdrawn.
- *Retire* deletes each imported rule from Actual by id and marks the Clerk
  rules `retired` in Actual. Only rules whose every Clerk counterpart is
  active are retired: a paused Clerk rule keeps its Actual rule in place,
  because deleting it would leave nothing filing that merchant. A failed
  delete is reported and leaves the rule present.
- *Restore* recreates a retired rule from the stored copy and records the
  new id; the rule is present in Actual again and can be retired again.

**The API.** `GET /api/intelligence/actual` reads and classifies live;
`POST /api/intelligence/actual/{import,retire,restore}` take `rule_ids`
(empty means all that qualify) and `dry_run`.

**The page.** A *From Actual* panel at the foot of Intelligence. Nothing is
read until *Read Actual's rules* is pressed. Each rule shows its sentence,
its state (ready to move, partly, cannot move, in Clerk and still in
Actual, retired in Actual, stays in Actual), and per translation the key,
the category, the replay figures, and any problem. Import, retire, and
restore work on everything that qualifies or on one rule; retire and
restore confirm first; every action has a preview, and the outcome is
listed under the panel.

## What was verified

- `uv run --extra dev pytest` passes, with the classifier and replay under
  `tests/test_actual_rules.py` and the three steps under `tests/test_api.py`
  against a scripted gateway. `scripts/worker-tests.sh` passes with the new
  worker methods.
- Against the lab (a copy of the live budget): see below.

## The lab run

Against the lab (`docs/plaid-migration/lab.md`, a copy of the live Actual
and Clerk data), on 2026-09-12:

| Step | Result |
| --- | --- |
| Read | 60 rules in Actual: 52 movable, 8 kept (4 set a transfer payee, 4 delete a transaction) |
| Replay | Every one of the 71 translations agreed with history in full: 104 of 104 Amazon rows, 24 of 24 ShopRite, 21 of 21 CVS, and so on; no collisions, no problems |
| Import (dry, then real) | 71 Clerk rules from 52 Actual rules; the income rule became 18 account-scoped rules (6 payees × 3 checking accounts); Actual untouched |
| Read again | 0 movable, 52 imported and present |
| Retire (dry, then real) | 52 rules deleted from Actual; the 8 kept rules remain; Clerk marks 52 retired |
| Categorize | The lab's open reviews are Plaid sandbox merchants none of the real rules name, so a rule was declared by hand for one (`chipotle mexican grill` → Food and Drink) and the review queue retried: 2 transactions applied by `source = rule`, written into the lab's Actual, the rule's applied count at 2 |
| Restore one | The Spotify rule recreated in Actual under a new id; Actual back to 9 rules; Clerk records `restored` |
| Retire it again | The restored rule deleted; Actual back to 8 |

The lab budget is left with only the 8 kept rules in Actual and 72 rules in
Clerk. The live budget is untouched.

## Running it for real

1. Open Intelligence, press *Read Actual's rules*, and read the list. A
   rule marked *partly* has a translation with a problem; the problem says
   what to fix (usually a collision with another rule or an existing Clerk
   rule) before that merchant moves.
2. *Preview import*, then *Import*. Both systems now answer; nothing is
   lost by staying here for a while.
3. *Preview retire*, then *Retire in Actual*. From the next sync on, Actual's
   imports arrive uncategorized and the categorize job files them by rule.
4. If anything looks wrong, *Restore to Actual* puts the rules back from the
   copies Clerk kept.
