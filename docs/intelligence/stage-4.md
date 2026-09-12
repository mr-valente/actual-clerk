# Stage 4: learning from Actual

Checkpoint for the fourth stage of [the plan](plan.md). Clerk now reads
what the user does in Actual after it has filed something, and lets that
change what it knows.

## What changed

**Observations.** Every applied decision carries an `observed` state on the
`decisions` table: empty until looked at, then `standing`, `corrected`,
`cleared`, or `gone`. Each filing run (`_learn_from_actual`) compares every
applied decision within the snapshot's window with the transaction as
Actual holds it now (`domain/intelligence.py`, `observe_decisions`). A
standing decision is looked at again next time, so a category changed
weeks later is still noticed; a correction is final. The decision drawer
shows what was seen, and Activity marks a corrected decision.

**Corrections and disputes.** A category changed by hand is recorded in
merchant memory with correction weight under the new category. When the
decision was a rule's, the rule's `disputed_count` goes up; at
`memory_dispute_threshold` (default 2) Clerk records a `rule_change`
proposal naming the category the user moved to, or a `rule_retire`
proposal when the user cleared the category instead. Accepting a change
updates the rule and resets its disputes; the user may pick a different
category on the way. The rule keeps applying until then: it is the user's
word, and only the user changes it.

**Repair.** A rule whose category has left the budget already filed
nothing (the resolver skips it); it now becomes a `repair` proposal, which
asks for the replacement with a category picker. Actual used to rewrite
its own rules when a category was deleted; that decision is now made on
the Intelligence page.

**The digest** lists open proposals under *Waiting for you*.

**Settings.** `memory_learn_from_actual` (on) and
`memory_dispute_threshold` (2), with environment variables
`CLERK_MEMORY_LEARN_FROM_ACTUAL` and `CLERK_MEMORY_DISPUTE_THRESHOLD`.

## What was verified

- `uv run --extra dev pytest` passes, with tests for the observation
  reader, the ledger migration, state changes recorded once, dispute
  counting, the job run (two corrections in Actual become two memory
  corrections, two disputes, and one rule-change proposal; nothing new on
  the next run; the switch turns it off; a missing category raises a
  repair), the proposal kinds through the API (a repair without a category
  is handed back open; an orphaned proposal is withdrawn), and the digest
  line.

## Not in this stage

- Any new question to the model (Stage 5).
