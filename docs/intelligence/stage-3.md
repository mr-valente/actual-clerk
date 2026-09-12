# Stage 3: one identity

Checkpoint for the third stage of [the plan](plan.md). A merchant now has
one identity in Clerk however it is named: the alias table serves every
source, Actual's own payee catalogue feeds it, the Intelligence page shows
what Clerk has learned, and a category set by hand on a phone charge is a
rule like any other.

## What changed

**Aliases from Actual.** Every filing run reads the snapshot's transactions
for pairs where the bank's descriptor and the payee the user settled on
normalize to different keys (`domain/intelligence.py`, `payee_aliases`).
`SQ *BLUE BOTTLE 4471` against `Blue Bottle Coffee` is an alias the user
curated in Actual without calling it one, and it is what keeps a rule
firing after a payee is renamed. Only unambiguous pairs are read: a
descriptor settled on two different payees says nothing. The table never
chains: a pair whose alias is already a target, or whose target is already
an alias, is left alone, both in the reader and in `learn_aliases`. A taught
alias is never overwritten by a learned one. Sources are now `taught`,
`settled`, and `actual_payee`.

**The resolver** already canonicalized through the alias table in Stage 1;
both the filing cascade and anticipated charges pass it the live map, and
the key preview and the rule form work on the canonical key.

**Merchants and aliases on the page.** A new panel lists every merchant in
Clerk's own memory (decisions applied, approvals, corrections) with its
label, the categories it has been filed under and how often, whether it
already has a rule, and three actions: *Make it a rule* (when the evidence
is unanimous), *Posts as…* (declare an alias), and *Forget*. Below it, the
alias list with its sources and a form to add one by hand. The alias list
that used to sit on the Connections page now points here.

**Teaching is a rule.** *Teach a category* on an anticipated charge is now
*Always file as…*: it sets the charge's category, declares a rule for the
notification's merchant key and, when the bank's name is known, for that
key too, withdraws any open rule proposal for either, and records a
correction in memory so the evidence agrees. When the charge later settles
against a row with a new name, the rule is carried to that key as well.
Clearing the category retires the rule the notification's key carried.

**The API.** `GET /api/intelligence` now also returns `merchants` and an
alias count; `POST /api/intelligence/aliases` declares an alias from two
names (refusing a pair that would chain); `DELETE
/api/intelligence/aliases/{key}` forgets one. The charge endpoints keep
working and the category one returns the rule it declared. A categorize
job reports `aliases_learned`.

## What was verified

- `uv run --extra dev pytest` passes, with new tests for the payee reader
  (ambiguity, chains), bulk learning (taught wins, no chains), the memory
  listing, the alias endpoints, the job learning aliases from a snapshot,
  and teaching declaring, carrying, and retiring rules.
- Against a local Clerk with seeded memory and aliases: the Merchants and
  aliases panel renders with evidence, a unanimous merchant offers *Make it
  a rule*, a split one shows both categories, and the alias sources read
  as sentences.

## Not in this stage

- Corrections observed in Actual, disputes, and category repair (Stage 4).
- Same-merchant questions to the model, which would arrive as alias
  proposals (Stage 5).
