# Bug report: renaming or reorganizing a category detaches its data from Clerk

**Status:** resolved by architecture replacement. As of the official API
migration, Clerk no longer reads Actual's raw ORM relationships through
`actualpy`. Transactions use Actual's AQL views, and budget months use Actual's
official spreadsheet-backed API. Both paths therefore inherit Actual's own
redirect and split semantics. The remainder of this document is preserved as a
historical incident report for the integration that was removed.

---

## Historical report (obsolete implementation)

**How to use this file.** It is written to be pasted to an LLM (or read by a
person) as a self-contained brief. It assumes no knowledge of the conversation
that produced it. Everything asserted here was verified against Actual
Budget's source or against a real SQLite database at the time of writing —
where something is a guess, it says so.

---

### The prompt

> Actual Clerk reads an Actual Budget file through `actualpy`. When a user
> renames, merges, or otherwise reorganizes a category in Actual, spending and
> budget amounts that Actual still displays correctly can stop being visible to
> Clerk. One cause of this is fixed; at least one more remains. Find and fix the
> remainder, and add regression tests that would have caught it.
>
> Read the sections below before touching code. The first describes what is
> already handled — do not re-fix it. The second is the open problem.

---

## Symptom, as the user experiences it

Everything looks right in Actual Budget. In Clerk, some subset of the same data
is missing:

- Spending appears as **uncategorized** in Clerk while Actual shows a category.
- A **budget amount** is absent from Clerk's committed total while Actual's
  budget screen counts it.
- Clerk's free money is **overstated**, because a budget it cannot see is not
  subtracted from expected income.

The workaround the user found, three separate times: create a brand new
category, reassign the affected transactions to it, and re-enter the budget
amount. That always works, which is itself a clue — it proves the data Actual
displays is reachable, and that Clerk's read path is what loses it.

## Mechanism 1 — Actual's redirect tables (FIXED, do not re-fix)

Actual does not rewrite transactions when a category is deleted into a
replacement or when two payees are merged. It records a redirect and resolves
it on every read. From Actual's own source:

```sql
-- packages/loot-core/migrations/1608652596044_trans_views.sql  (v_transactions)
FROM transactions t
LEFT JOIN category_mapping cm ON cm.id = t.category
LEFT JOIN payee_mapping    pm ON pm.id = t.description
```

```js
// packages/loot-core/src/server/db/index.ts — createCategory
// Create an entry in the mapping table that points it to itself
await insert('category_mapping', { id, transferId: id });
```

```js
// packages/loot-core/src/server/db/index.ts — deleteCategory
await update('category_mapping', { id: category.id, transferId });
return delete_('categories', category.id);   // budget rows are NOT touched
```

Every category is mapped to itself on creation; a merge repoints the mapping.
`actualpy` joins `Transactions.category_id == Categories.id` **directly**, with
no mapping hop, so it reads pre-merge ids that resolve to nothing.

**What was done about it** (all in `src/actual_clerk/clients/actual.py`):

- `Redirects` and `read_redirects()` load `category_mapping` and
  `payee_mapping`, ignoring self-maps, with a cycle guard.
- `transaction_dict()` resolves both category and payee through them.
- `collect_snapshot()` resolves budget rows too — `deleteCategory` leaves budget
  rows on the old id. A redirected budget row is only inherited when the
  surviving category has no row of its own that month (`inherited` /
  `setdefault`), so a merge can never invent money by summing two rows.
- `write_updates()` refuses to write a `category_id` that is not in
  `categories`. Actual does not enforce that column as a foreign key.
- `build_budget_report()` in `src/actual_clerk/domain/budget.py` treats a
  `category_id` it cannot resolve as uncategorized rather than as anonymous
  discretionary spending.

Regression tests live in `tests/test_redirects.py` and run against a real
SQLite database built from Actual's own schema.

## Mechanism 2 — THE OPEN PROBLEM

After all of the above shipped, the user still saw four transactions that Actual
showed as categorized and Clerk counted as uncategorized. Re-applying the
category in Actual fixed them.

**This is not mechanism 1, and the diagnostic proves it.** Clerk's report
distinguishes the two cases:

- `orphaned` — the transaction carries a `category_id` that resolves to nothing.
- `uncategorized` — the transaction carries **no `category_id` at all**.

The report showed `orphaned: 0.00` and `uncategorized: 29.34`. So Clerk read
those rows with an empty category column, while Actual displayed a category for
them. A redirect cannot explain an empty column.

### Hypotheses, none yet tested

1. **Split transactions.** `actualpy`'s `get_transactions()` filters
   `is_parent == int(is_parent)` and defaults to `False`, so it returns children
   and plain rows but never parents. Check what Actual displays for a split
   whose parent is categorized, and whether any row involved can reach Clerk
   with a null category.
2. **Sync lag on specific rows.** Clerk keeps a local copy of the budget file
   and applies CRDT messages. Confirm whether a category set in Actual can be
   missing from Clerk's local copy while other changes to the same transaction
   arrive. Compare `transactions.category` in Clerk's `data/budget/*.sqlite`
   against the server's for the same transaction id.
3. **A category set by an Actual rule at import**, where message ordering leaves
   the local copy without it.
4. **A tombstoned category with no mapping row.** `get_categories()` defaults to
   `include_deleted=False`; a row could be hidden from Clerk while Actual still
   resolves it some other way. Note this would normally surface as `orphaned`,
   not `uncategorized`, so it is the weakest hypothesis.

### How to get the evidence

The diagnostic already prints what is needed. Run it (Settings → Limits &
reliability → **Run diagnostics**, or `GET /api/diagnostics`) and read:

- **Section 6** — every uncategorized transaction listed individually with date,
  amount, account, payee, and **transaction id**.
- **Section 6b** — the `orphaned` versus `uncategorized` split described above.
- **Section 4** — every budget row exactly as stored, with raw category ids.

Then, for one affected transaction id, compare directly:

```python
# against Clerk's local copy
session.get(Transactions, "<id>").category_id
session.get(CategoryMapping, "<that category id>")
# and what Actual shows for the same row in its UI
```

Reproducing from scratch is the harder half. `tests/test_redirects.py` shows the
pattern: build a real SQLite file from `SQLModel.metadata.create_all`, perform
the operation the way Actual performs it (**raw SQL, not ORM deletes — an ORM
cascade will null the foreign key and give you a false positive**), then read it
back through `transaction_dict()`.

## Related divergences found but deliberately not fixed

These are real and could bite during a rename or reorganization. They were left
alone because neither could produce the symptom above, and fixing something that
cannot cause the bug is how two rounds of this investigation were wasted.

**Income and hidden classification.** Actual's tracking budget computes:

```js
// packages/loot-core/src/server/budget/tracking.ts
const expenseGroups = groups.filter(g => !g.is_income && !g.hidden);
// per group:
dependencies: group.categories.filter(cat => !cat.hidden).map(c => `budget-${c.id}`)
```

Actual excludes income **groups** and hidden **groups**, and excludes hidden
**categories** from each group's sum. Clerk instead excludes a category when
*either* its own `is_income` flag or its group's is set
(`clients/actual.py`, `collect_snapshot`), and does not consider hidden at all in
`is_committed()`. A category flagged `is_income` inside an ordinary expense group
therefore counts for Actual and not for Clerk.

**Actual's budget screen reads cached spreadsheet cells**, not `reflect_budgets`.
`total-budgeted` sums `group-budget-<id>` cells persisted in `kvcache`. Actual
ships a **Settings → Reset budget cache** action for when those go stale.
`cloud-storage.ts` deletes `kvcache` before upload, so Clerk's downloaded copy
never contains it and cannot check it. **Do not advise a user to reset that
cache while a value's absence from the database is still unexplained** — the
reset recomputes from the database and would destroy a value that lived only in
the cache.

## Acceptance criteria

1. A transaction categorized in Actual is never reported uncategorized by Clerk,
   for every way Actual can attach a category (direct, rule at import, split,
   after a merge or rename).
2. A budget amount visible on Actual's budget screen always appears in Clerk's
   `committed_cents`, or the diagnostic names the specific row and why not.
3. Tests run against a real database using Actual's schema and reproduce the
   original failure before the fix.
4. `GET /api/diagnostics` still reconciles: section 6b's four buckets sum to the
   month's total spending, and section 4's rows sum to section 3's table total.

## Traps, from the investigation that produced this file

- **Do not trust "Clerk and Actual agree" as validation** when both numbers come
  from the same snapshot. A "difference: 0.00" line compared Clerk's figure with
  Clerk's *reconstruction* of Actual's figure and could not have caught this.
- **Do not assert a mechanism you have not verified.** An earlier version of the
  diagnostic claimed "Actual still counts it" about an orphaned budget row.
  Actual's `handleBudgetChange` is gated on `if (budget.category)`, so it does
  not.
- **Fix causes, not coincidences.** If a proposed fix cannot produce the
  symptom, it is not the fix, even when it is a genuine improvement.
- **Instrument before theorising.** Every real advance here came from printing
  raw rows; every wrong turn came from reasoning about derived totals.
