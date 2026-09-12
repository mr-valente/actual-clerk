# Intelligence: Clerk as the manager of what a transaction means

Working document for making Clerk the first-class home of the simple
payee-to-category knowledge that currently lives in Actual's rules, and for
turning Clerk's scattered learning (merchant memory, rule promotion, phone
aliases, taught categories) into one system that learns the whole category
tree. Stages are implemented one at a time on `feature/plaid`, each ending
with a checkpoint, in the same manner as [the Plaid migration](../plaid-migration/plan.md).

The short version:

- **Actual keeps only rules that do something other than pick a category**:
  transfer payee assignment, transaction deletion, payee renames. Everything
  that says "this payee belongs in that category" moves into Clerk and is
  deleted from Actual once Clerk has proven it holds the same answer.
- **A rule is an association Clerk is certain of.** Its confidence is 1.0 by
  definition, it applies without thresholds, and only a person creates it
  (by hand, by approving a proposal, or by importing it from Actual). Clerk
  proposes rules; it never asserts one on its own.
- **Below rules sits everything Clerk has learned**: merchant evidence from
  the user's own filed history, aliases between the names a merchant goes
  by, corrections observed in Actual, and the model's proposals. One
  resolver reads all of it in a fixed order and is used by the filing
  cascade and by anticipated charges alike.
- **A new page, Intelligence**, is where all of this is visible and editable:
  rules, proposals, merchants and their aliases, what was imported from
  Actual, and what Clerk is unsure about.

## 1. Where things stand

### What Clerk does today

Clerk does not read Actual's rules. It reads the user's categorized history
(the result of those rules having fired, plus hand filing) and rebuilds a
recency-weighted merchant memory from it on every run (`domain/memory.py`).
The cascade in `categorize.py` goes memory, then model, then review, and its
reverse direction promotes a merchant Clerk has filed consistently into a
**native Actual rule** (`promoted_rules`, `createCategoryRule`). Nothing
flows from Actual's rule table into Clerk.

Anticipated charges added a second, parallel learning path: `merchant_aliases`
maps a phone notification's merchant key to the bank's key for the same shop,
learned at settlement or taught by hand, and `teach_category` writes
correction-weighted rows into `merchant_memory` under both keys
(`anticipated.py`). The alias table is only consulted for notifications.

So there are three places knowledge accumulates (Actual's rules,
`merchant_memory`, `merchant_aliases`), two promotion directions, and no
single view of any of it.

### What the real budget holds

Read from the API cache of the live budget (`My-Finances`), read-only:

| Count | Shape | Disposition |
| --- | --- | --- |
| 48 | `payee is X` → `set category` | Move to Clerk |
| 1 | `payee is X or payee is Y` → `set category` | Move to Clerk (two rules) |
| 1 | `payee oneOf [X, Y]` → `set category` | Move to Clerk (two rules) |
| 1 | `imported_payee contains "Yardsale Cafe"` → `set category` | Move to Clerk |
| 1 | `account oneOf [3 checking accounts]` and `payee oneOf [6 deposit payees]` → `set category Income` | Move to Clerk as six account-scoped rules (see §3.2), or leave; the preview will say which |
| 4 | `account is X` and `payee is Y` → `set payee` to a transfer payee | **Stays in Actual** |
| 4 | `account is X` and `payee is Y` → `delete transaction` | **Stays in Actual** |

Every rule is in the `default` stage. 228 payees, 52 categories, 668
transactions, 38 currently uncategorized.

The rule table stores the payee condition under the field name
`description` and the account condition under `acct` (Actual's internal
column names); the public API presents them as `payee` and `account`. The
worker must accept both spellings.

### What the Actual API allows

All from the pinned `@actual-app/api` bundle, not documentation:

| Method | Handler | Use |
| --- | --- | --- |
| `getRules()` | `api/rules-get` | Read every rule, ranked as Actual ranks them |
| `getPayeeRules(id)` | `api/payee-rules-get` | Rules mentioning one payee |
| `createRule(rule)` | `api/rule-create` | Already used for promotion; kept for **restoring** a rule to Actual |
| `updateRule(rule)` | `api/rule-update` | Not needed |
| `deleteRule(id)` | `api/rule-delete` | **Retiring** a rule after Clerk has taken it over |
| `getPayees()` | `api/payees-get` | Resolve payee ids in rule conditions to names, and read the payee catalogue as alias evidence |

`importTransactions` and the native bank sync run whatever rules remain in
Actual, so the transfer and delete rules keep working exactly as now.

## 2. The model of memory

Everything Clerk knows about a transaction's meaning is one of four things.

### 2.1 Identity: which merchant is this

A raw descriptor becomes a **merchant key** through `normalize_merchant`
(deterministic, already exists). Keys that name the same merchant are joined
by **aliases**: `alias_key → merchant_key`, with a source. Today the only
aliases are notification-versus-bank pairs. They become general:

| Source | How it arises |
| --- | --- |
| `taught` | The user says "this posts as …" |
| `settled` | An anticipated charge matched a posted row with a different key |
| `actual_payee` | Actual's own payee catalogue: transactions whose `imported_payee` normalizes to one key but whose payee normalizes to another are the user's historic curation of the same fact |
| `proposed` | The model, or a similarity heuristic, thinks two keys are one merchant and a person has not yet agreed (never used for resolution until accepted) |

Resolution canonicalizes a key through the alias table (one hop, no cycles)
before anything else looks at it. Aliases carry no category; they only say
who a merchant is.

### 2.2 Rules: what the user has declared

A **rule** is `merchant_key → category` with confidence 1.0, created only by
a person. Optional `account_id` scope for the "deposits into checking are
income" case. Optional `match` mode: `exact` (default; the key must be equal)
or `family` (`keys_related`, so `starbucks` also claims `starbucks seattle`).
A rule applies regardless of `apply_mode`: that setting governs what Clerk
may do with *learned* evidence, and a rule is precisely the thing the user
has taken out of Clerk's judgment.

A rule is never overwritten by evidence. When the evidence disagrees with a
rule (§2.4), Clerk raises a proposal; the rule keeps applying until the user
changes it.

### 2.3 Evidence: what Clerk has learned

Unchanged in substance: `MerchantMemory` rebuilt each run from the user's
filed history, merged with Clerk's own applied decisions and corrections
(`merchant_memory`), with the existing thresholds. Evidence answers merchants
without a rule and is what rule proposals are built from.

### 2.4 Observations: what Actual shows Clerk about its own work

A new channel. Every decision Clerk applies is stored with its category. On
each snapshot, Clerk compares those against the transaction's *current*
category in Actual:

- Same → the decision stands (nothing to record).
- Different, set by hand → a **correction**: recorded in `merchant_memory`
  with correction weight under the new category, and, if a rule produced the
  original, a **dispute** counted against that rule.
- Transaction deleted or turned into a transfer → nothing learned (the
  existing `resolved_external` logic already covers open reviews; this
  extends it to applied decisions).

Two disputes against one rule produce a proposal to change or retire it. One
dispute produces nothing but a mark on the Intelligence page. This is how Clerk
learns from work done inside Actual without the user ever opening Clerk.

### 2.5 The resolver

One pure function, in `domain/`, replaces the ad hoc memory lookups in
`categorize.py` and `anticipated.py`:

```text
resolve(key, account_id, memory, rules, aliases, settings) -> Resolution
  1. canonical = aliases.get(key, key)
  2. rule for (canonical, account_id), then (canonical, any account)
        -> source "rule", confidence 1.0, apply
  3. memory.lookup(canonical) meeting thresholds
        -> source "memory", apply when apply_mode is automatic
  4. memory.exact_suggestion(canonical)
        -> source "memory", review only
  5. (caller may ask the model)
        -> source "model", review only
  6. unresolved -> review
```

Steps 1 to 4 need no network and complete even when the model and the bank
are both down. Anticipated charges use steps 1 to 4. The cascade uses all
six. `Resolution` carries the layer it stopped at and the evidence it saw, so
the review drawer and the Intelligence page can explain every answer the same way.

## 3. Data

### 3.1 New tables

```sql
CREATE TABLE merchant_rules (
    id TEXT PRIMARY KEY,
    merchant_key TEXT NOT NULL,
    account_id TEXT NOT NULL DEFAULT '',        -- '' means any account
    match TEXT NOT NULL DEFAULT 'exact',        -- exact | family
    category_id TEXT NOT NULL,
    category_name TEXT NOT NULL DEFAULT '',
    merchant_label TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,                       -- user | proposal | imported
    status TEXT NOT NULL DEFAULT 'active',      -- active | paused | retired
    actual_rule_id TEXT NOT NULL DEFAULT '',    -- when imported: the rule it came from
    actual_rule_json TEXT NOT NULL DEFAULT '',  -- verbatim, so it can be restored
    actual_status TEXT NOT NULL DEFAULT '',     -- '' | present | retired | restored
    applied_count INTEGER NOT NULL DEFAULT 0,
    disputed_count INTEGER NOT NULL DEFAULT 0,
    last_applied_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX uq_merchant_rules_active
    ON merchant_rules(merchant_key, account_id) WHERE status = 'active';

CREATE TABLE proposals (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,           -- rule | rule_change | rule_retire | alias | repair
    merchant_key TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL,   -- the change, in the shape the accept handler applies
    evidence_json TEXT NOT NULL,  -- why: sightings, decisions, disputes, model reason
    status TEXT NOT NULL DEFAULT 'open',   -- open | accepted | declined
    created_at REAL NOT NULL,
    resolved_at REAL
);
CREATE UNIQUE INDEX uq_proposal_open
    ON proposals(kind, merchant_key) WHERE status = 'open';
```

`merchant_aliases` gains nothing but new `source` values and a general
purpose. `merchant_memory` is unchanged. `promoted_rules` is kept as the
record of rules Clerk once wrote into Actual; new proposals go to
`proposals`, and an accepted `rule` proposal writes `merchant_rules`, not
Actual.

`decisions.source` gains `rule`, with the rule id in `rationale_json`, so
Activity shows rule applications alongside memory and model ones, and so the
observation channel (§2.4) can attribute a correction to a rule.

### 3.2 The account-scoped income rule

The one multi-condition category rule in the budget says: deposits from any
of six payees, into any of three checking accounts, are income. With
`account_id` on the rule table it imports as eighteen scoped rules, or six
unscoped ones if those payees never appear with another category anywhere.
The import preview shows both readings and their replay results (§5.2); the
user picks. If neither reads cleanly, the rule stays in Actual and is listed
under "kept in Actual" so it is not forgotten.

## 4. The Intelligence page

A fifth view in the sidebar, between Review and Connections. Review keeps
transactions waiting for a decision; Intelligence holds everything Clerk
*knows* and everything it *wants to know*. The Review badge counts
transactions; an Intelligence badge counts open proposals.

**Proposals** (top, only when non-empty). Each is one row with its evidence
and two buttons. Kinds: *make a rule* (a merchant filed the same way
`memory_propose_after` times, replacing today's "rules worth promoting"),
*change a rule* (disputes), *retire a rule* (category gone, or never fires),
*same merchant?* (alias), *repair* (rules pointing at a category that no
longer exists, with a picker for the replacement).

**Rules.** Searchable table: merchant, category, scope, match mode, source,
applied count, last applied, disputes. Add by hand (merchant text is
normalized live so the user sees the key it becomes), edit category, pause,
retire. A retired rule keeps its row for a while so a mistake can be undone.

**Merchants.** What Clerk has learned without being told: key, label,
aliases, evidence per category with share and sightings, current resolution
and which layer produced it. Actions: *make rule* (one click, pre-filled),
*posts as…* (alias), *forget*. This is where the user sees why a merchant is
still going to the model.

**From Actual.** The import panel (§5): what is in Actual now, what would
move, what stays, replay results, and the three buttons *import*, *retire in
Actual*, *restore to Actual*, each with a dry run.

**Create rule** buttons appear on Review rows (next to Apply, "Apply and
always"), in the review drawer, and on anticipated charges ("Teach" becomes
"Always file as…"). All of them write `merchant_rules`. The old flow that
wrote an Actual rule is removed from the UI; the worker method survives only
for restore.

## 5. Taking the rules over from Actual

### 5.1 Classifying a rule

A rule is **simple** when every condition is on `payee` or `imported_payee`
with op `is`, `oneOf`, or `contains` (any `conditionsOp`), and its actions
are exactly one `set category`. Everything else is **kept**: any condition on
account, amount, date, notes; any action that sets a payee, deletes, splits,
links a schedule, or sets anything other than a category. A rule in a stage
other than `default` is kept too, since stage order is an Actual concept.

A simple rule becomes one Clerk rule per payee it names:

- `payee is <id>` → the payee's current name (through `payee_mapping`, so a
  merged payee resolves to its survivor), normalized to a key, `exact`.
- `imported_payee contains "text"` → `normalize_merchant(text)`, `exact`.
  The `contains` semantics are not carried; the replay shows whether the key
  covers the same rows.
- The optional account condition of §3.2 → `account_id` scope.

### 5.2 Replay before trusting the translation

For each proposed Clerk rule, the preview replays it over the retained
history: how many transactions its key would claim, how many of those Actual
filed in the same category, and how many in another. A rule that replays
with disagreement is shown in amber and is not imported until the user
confirms the category. Two Actual rules that normalize to the same key with
different categories are a collision and are shown together. A payee whose
name normalizes to nothing (a bare number) cannot be a rule and is listed as
kept.

### 5.3 Three explicit steps, each reversible

1. **Import.** Copies simple rules into `merchant_rules` with
   `source = imported`, `actual_status = present`, and the original rule
   JSON. Actual is untouched. From here both systems answer; Actual answers
   first at import, so Clerk sees categorized rows and applies nothing.
   Nothing is lost by staying in this state.
2. **Retire in Actual.** Deletes each imported rule from Actual by id and
   marks `actual_status = retired`. Dry run first, listing exactly which
   rule ids go. From here Actual imports arrive uncategorized and the
   categorize job that follows every sync files them by rule. The lab
   (`docs/plaid-migration/lab.md`) is the place to run this against a copy
   of the real budget first.
3. **Restore to Actual.** For any imported rule, or all of them, recreates
   the original rule from the stored JSON through `createRule` and marks
   `actual_status = restored`. This is the road back, and it is also what
   makes step 2 safe to try.

Rules created in Actual *after* the import (a rename rule Actual writes when
the user renames a payee, a new transfer rule) are never touched; the import
panel lists them as "in Actual, not managed by Clerk" so the user notices a
category rule that slipped back in.

### 5.4 What changes when Actual no longer categorizes

- Between an import and the categorize job that follows it, rows sit
  uncategorized in Actual for as long as the job queue takes, normally
  seconds. The sync job already queues `categorize` on completion.
- The rule layer never waits on the model, so a model outage delays only
  first-time merchants, as now.
- A category deleted in Actual used to rewrite Actual's rules to the
  replacement. Clerk cannot see that choice, so a rule whose category id is
  missing from the snapshot becomes a `repair` proposal and stops applying
  until repaired. The Intelligence page shows it; the digest counts it under
  "waiting for you".
- A payee renamed in Actual changes the key Clerk sees. The `actual_payee`
  alias source (§2.1) is what keeps the rule firing: the imported name still
  normalizes to the old key, and the alias maps it to the new one.

## 6. What the model is asked

The model stays a consultant with bounded questions and never writes
anything:

1. **Category for an unknown merchant** (exists). Now also shown the rules
   for related keys, since a rule for `amazon` is a strong hint for
   `amazon fresh`.
2. **Same merchant?** Given a notification merchant or an unfamiliar key and
   a short list of known keys that share tokens, is any of them the same
   shop? A yes becomes an `alias` proposal, never an alias.
3. **Rules from history** (first run, on request). For merchants the user
   has filed consistently for a long time but that have no rule, Clerk can
   propose rules in bulk from evidence alone; the model is not needed for
   this, and it is listed here only to say so.

Each answer is a proposal on the Intelligence page with the model's one-sentence
reason attached.

## 7. Settings

| Setting | Change |
| --- | --- |
| `rule_promotion_enabled` | Now means "propose Clerk rules"; the Actual write path is gone |
| `rule_promote_after` | Kept under its name (stored settings and environment variables refer to it); relabelled in the UI as the count before *proposing* |
| `apply_mode` | Unchanged, and documented as governing learned evidence only; rules always apply |
| `memory_dispute_threshold` (new, default 2) | Disputes before a rule-change proposal |
| `memory_learn_from_actual` (new, default on) | The observation channel of §2.4 |

## 8. Stages

Each stage is a checkpoint: tests green, the doc updated, and a note in this
folder recording what was verified live.

**Stage 1: rules live in Clerk.** `merchant_rules`, `proposals`, the
resolver with the rule layer, `source = rule` decisions, "Create rule"
everywhere writing Clerk rules, promotion producing proposals instead of
Actual rules, the Intelligence page with Proposals and Rules. Anticipated charges
classify through the resolver. No Actual rule is read or removed yet.
Done: see [stage-1.md](stage-1.md).

**Stage 2: take over from Actual.** Worker methods `listRules`, `listPayees`,
`deleteRule` with contract tests; the rule classifier and replay; the From
Actual panel with import, retire, restore and dry runs. Verified in the lab
against a copy of the real budget, then run for real.

**Stage 3: one identity.** Alias sources generalized, the payee catalogue
read as alias evidence, canonicalization in the resolver for both the
cascade and anticipated charges, the Merchants section of the Intelligence page,
"teach" on a charge unified with "create rule".

**Stage 4: learning from Actual.** Applied-decision observation, corrections
and disputes, rule-change and retire proposals, category repair, the digest
line.

**Stage 5: the model as consultant.** Same-merchant questions, rule hints in
the category prompt, bulk rule proposals from history for a first run.

## 9. Decisions taken in this plan

Written down so they can be overturned before Stage 1 starts:

- Rules match on Clerk's **normalized key**, not on Actual's payee id. This
  is broader than Actual's `payee is` (one rule covers every store number)
  and is why the replay step exists.
- A rule **always applies**, even in review mode. Pausing a rule is the way
  to stop it.
- Clerk **never creates a rule by itself**. High-confidence evidence still
  auto-applies under the existing thresholds; what changes is that "rule"
  now means "the user said so".
- The Actual write path for rules is removed from the UI and kept only for
  restore. The transfer and delete rules are never read for any purpose
  other than listing them as kept.
- The page is called **Intelligence**, because rules are the smallest part
  of it, and because it is Actual Intelligence.
