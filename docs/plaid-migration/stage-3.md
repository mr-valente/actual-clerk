# Stage 3: the Plaid transaction sync engine

Clerk now delivers Plaid's change stream into Actual on every sync run,
before Actual's own bank sync, so one snapshot afterwards sees both feeds.

## How a sync works

For every Item with at least one enabled mapping:

1. **Refresh.** If refresh is on and the Item was last refreshed more than
   `plaid_refresh_min_interval_minutes` ago, Clerk calls
   `/transactions/refresh`, then polls `/item/get` every five seconds for up
   to `plaid_refresh_wait_seconds`, stopping as soon as the Item reports a
   newer successful (or failed) transactions update. Clerk cannot receive
   Plaid's webhook, so the poll stands in for it. A refresh Plaid declines is
   reported and the stream is read anyway; a login error stops the Item.
2. **Read.** `/transactions/sync` from the stored cursor to the end,
   restarting from the original cursor on
   `TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION` (three times, then a
   retryable failure). An Item still preparing history (`NOT_READY`, nothing
   returned, no cursor yet) is left for the next run.
3. **Plan** each mapped account with `domain.plaid_import.plan_account`, a
   pure function over the change set and the account's current Actual rows:
   - transactions dated before the account's **cutover date** are skipped;
   - a transaction whose id already exists is **settled** in place if its
     amount or cleared state changed (Actual's import never rewrites those);
   - a posted transaction naming a `pending_transaction_id` Actual holds
     **adopts** that row: new id, cleared, settled amount;
   - a transaction within `plaid_adopt_window_days` of the cutover with the
     same amount as a row carrying a foreign (non-Plaid) id **adopts** that
     row rather than duplicating it, closest date first, each row once;
   - everything else is **imported** through `importTransactions`, which
     deduplicates by id, fuzzy-matches manual rows, and runs the user's rules;
   - a `removed` id whose row is still uncleared and unreconciled is
     **deleted** (switchable); a cleared row is **kept** and reported.
   Split children and starting-balance rows are never touched.
4. **Write**, in order: adoptions, deletions, then the import. An opening
   balance row (dated the day before the cutover, flagged as Actual's own
   starting balances are, filed under "Starting Balances") is prepended on
   the first import into an account that holds no transactions at all, sized
   so posted history adds up to Plaid's current balance.
5. **Persist the cursor** only after every mapped account on the Item was
   written. A failed run replays the same window; dedup by id makes that
   idempotent. One Item failing never stops the others.

Adoptions are logged in `import_adoptions`; every step is a job event on the
Activity page.

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `plaid_sync_enabled` | on | Run the engine at all |
| `plaid_refresh_enabled` | on | Ask for an on-demand refresh first |
| `plaid_refresh_min_interval_minutes` | 55 | Per-Item gap between refreshes (Plaid allows 2 per minute, 120 per hour) |
| `plaid_refresh_wait_seconds` | 45 | Bounded wait for the refresh to land |
| `plaid_delete_removed_pending` | on | Delete withdrawn pending charges while uncleared |
| `plaid_starting_balance` | on | Opening balance for an empty account |
| `plaid_adopt_window_days` | 14 | Cutover adoption window |

## Verified against the lab

With the sandbox Item mapped onto two empty Actual accounts (cutover
2026-09-01):

- First sync: 65 transactions imported, 66 older ones skipped, one refresh
  sent, opening balances added to both accounts (checking `-137.67`, credit
  card `+1,277.41`), each filed under "Starting Balances" and flagged as a
  starting balance.
- Health check afterwards: **cleared balance equals Plaid's balance to the
  cent on both accounts** (`drift 0`), with pending rows counted as
  uncleared. This confirms Plaid's `current` balance is the posted balance
  for both depository and credit accounts, and that the opening-balance
  arithmetic is right.
- Second sync minutes later: nothing imported, no refresh (inside the
  interval), and every row still carries a unique import id.

Not exercised live: pending-to-posted swaps and withdrawn pendings, which the
sandbox produces only as time passes. Both are covered by planner and engine
tests; the lab will show them on later syncs.

## Notes

- Plaid's sandbox pairs enriched merchant names with unrelated bank
  descriptions ("Dunkin'" over "ACH PAYMENT PAYPAL"). Clerk keeps both: the
  merchant name becomes the payee, the bank text becomes `imported_payee`,
  which is what merchant memory keys on. Real institutions are consistent.
- `modified` amounts win over what Actual holds. A tip added at settlement
  is the truth of the charge; the row keeps its date, category, notes, and
  tags.
