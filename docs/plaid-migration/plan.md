# Plaid in Actual Clerk: findings and staged plan

Working document for moving Clerk, and the Actual budget it looks after, from
SimpleFIN to Plaid, while keeping the road back to SimpleFIN open. Stages are
implemented one at a time; each ends with a checkpoint.

## 1. What the Actual API allows

Everything below was read from the pinned `@actual-app/api` 26.9.0 bundle, not
from documentation.

### Unlinking an account keeps the account

Actual's `account-unlink` handler sets `account_id`, `bank`, `balance_current`,
`balance_available`, `balance_limit`, `account_sync_source`, and
`bank_sync_status` to null on the account row. Nothing else changes: the
account, every transaction (including each one's SimpleFIN `imported_id`),
payees, rules, schedules, and budget history all stay. From Actual's point of
view the account simply becomes a manual account. It can be relinked later.

### The documented method list is not the whole API

`api.init()` returns a `send(handlerName, args)` function (also exposed as the
deprecated `api.internal.send`) that reaches every server handler, not just the
`api/*` wrappers. Clerk already pins the exact package version, so using a small
set of these handlers is safe as long as the worker contract tests cover them.
The relevant ones:

| Handler | Effect |
| --- | --- |
| `account-unlink` `{id}` | Detach bank sync from an account (above) |
| `simplefin-accounts-link` `{externalAccount, upgradingId, offBudget, startingDate, startingBalance}` | Attach a SimpleFIN account to an existing (`upgradingId`) or new Actual account and run a first sync from `startingDate` |
| `simplefin-status`, `simplefin-accounts` | Ask the Actual *server* whether a SimpleFIN token is set and list its accounts |
| `secret-set` `{name: "simplefin_token", value}` (`value: null` deletes) | Store or remove the SimpleFIN token on the Actual server |
| `accounts-bank-sync` `{ids}` / `api/bank-sync` | What Clerk calls today |
| `api/transactions-import` | `importTransactions`: dedup by `imported_id`, then fuzzy match, then Actual's own rules |
| `api/transaction-update`, `api/transaction-delete`, `api/account-create` | Public methods Clerk will use for Plaid imports |

So **both migration directions can be driven entirely from Clerk**: SimpleFIN
to Plaid (unlink in Actual, Clerk imports) and Plaid back to SimpleFIN (Clerk
stops, relinks the account in Actual with a starting date, optionally sets the
server token).

### How Actual deduplicates an import, and why the cutover needs care

`importTransactions` matches an incoming row by `imported_id` first. If that
fails it looks for a fuzzy match: same account, same amount, date within seven
days. On the public API path the fuzzy query has a strict clause: an existing
row that already carries an `imported_id` is **excluded** when the incoming row
also carries one. SimpleFIN rows have SimpleFIN ids and Plaid rows have Plaid
ids, so **any overlap window would be imported twice**. Actual's own bank sync
path relaxes this rule for linked accounts, which is why the reverse migration
(relinking SimpleFIN) is safe: Actual will fuzzy-match rows Clerk imported from
Plaid and adopt them.

Consequences for the forward migration:

- Clerk imports only Plaid transactions dated on or after a per-account
  **cutover date**.
- For the boundary window (pending rows at cutover, posting-date drift), Clerk
  does its own adoption: match by amount and date, then rewrite the existing
  row's `imported_id` to the Plaid `transaction_id` so later `modified` and
  `removed` events find it. Unmatched rows are imported normally.
- The same adoption logic handles Plaid's pending-to-posted swap, which arrives
  as `removed` (pending id) plus `added` (posted id, carrying
  `pending_transaction_id`). Actual's strict rule would otherwise duplicate
  every card purchase.

`importTransactions` runs Actual's rules, so promoted Clerk rules and the
user's own rules keep working on Plaid imports.

## 2. What Plaid allows

- **Trial plan**: ten production Items for the lifetime of the team, removals
  do not free a slot, unlimited calls, and Transactions Refresh is included.
  Sandbox is unlimited. OAuth institutions need an HTTPS redirect URI registered
  in the dashboard.
- **`/transactions/sync`** is a per-Item cursor stream of `added`, `modified`,
  `removed`, with `has_more` paging, `transactions_update_status`, and a
  `TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION` error that means "restart from
  the original cursor". Each response also returns the Item's accounts with
  cached balances. Limit 50 per minute per Item.
- **`/transactions/refresh`** asks Plaid to extract now instead of on its one
  to four times a day schedule. It is asynchronous and signals completion by
  webhook. Clerk is not reachable from the internet, so it will instead poll
  `/item/get` for `status.transactions.last_successful_update` to advance (or a
  new `last_failed_update`), bounded by a timeout, then call `/transactions/sync`.
  Limit 2 per minute and 120 per hour per Item, so hourly use is fine.
- **`/accounts/get`** returns cached balances for free (15 per minute per
  Item). `/accounts/balance/get` is real-time but paid and rate-limited, so
  Clerk will not use it by default.
- **`/item/get`** carries `item.error` (for example `ITEM_LOGIN_REQUIRED`),
  `consent_expiration_time`, and the update timestamps above. These replace
  SimpleFIN's `errlist` and `balance-date` as health signals.
- **Link**: `link-initialize.js` must load from `cdn.plaid.com`; a link token
  is minted server-side and lasts four hours (thirty minutes in update mode).
  Update mode reuses the Item's access token and does not burn a slot.
- **Sandbox**: `/sandbox/public_token/create` creates an Item without the Link
  UI (institution `ins_109508`, username `user_transactions_dynamic` for
  realistic data), `/sandbox/item/reset_login` breaks a login to test repair,
  `/sandbox/transactions/create` seeds custom transactions. All usable from
  tests and from a sandbox-only button in the UI.

Amount signs: Plaid outflows are positive; Actual outflows are negative. Credit
and loan balances are positive when owed in Plaid, negative in Actual.

## 3. Design

Clerk becomes the bank-sync engine for Plaid accounts while remaining an
observer for SimpleFIN accounts that Actual still syncs natively.

```text
sync job
  +-- Plaid phase (per Item)
  |     refresh (policy) -> wait for item status -> /transactions/sync (cursor)
  |     route by account_id -> adopt / import / remove -> persist cursor
  +-- Actual native bank sync (remaining SimpleFIN-linked accounts)
  +-- snapshot, overview, categorize, health (unchanged)

health job
  +-- SimpleFIN client  (accounts Actual links)          -> RemoteAccountInfo
  +-- Plaid client      (accounts Clerk links)           -> RemoteAccountInfo
  +-- evaluate_accounts (provider-neutral)
```

Data Clerk keeps, all in its own SQLite:

- `plaid_items`: item id, institution, encrypted-at-rest access token (same
  secret handling as today's settings secrets), sync cursor, last refresh and
  sync times, last error, needs-repair flag.
- `bank_links`: one row per Actual account Clerk syncs: provider, Plaid account
  id, item id, Actual account id, enabled, cutover date, adoption log.
- Per-transaction provenance is not duplicated; Actual's `imported_id` is the
  record.

Linkage in health checks becomes an overlay: an account is "linked" if Actual
links it (SimpleFIN) or Clerk links it (Plaid). Statuses, transitions, muting,
the digest, and the alert set stay as they are; only the signal sources change.

## 4. Stages

Each stage is a reviewable set of commits with tests. Nothing in a later stage
is started before the earlier checkpoint is approved.

### Stage 1: provider-neutral foundations (no Plaid yet)

- Worker: add `listAccountsDetailed`, `unlinkAccount`, `linkSimpleFinAccount`,
  `simpleFinServerStatus`, `simpleFinServerAccounts`, `setServerSecret`,
  `importTransactions`, `deleteTransactions`, `adoptImportedIds`,
  `createAccount`; worker contract tests for each, using the same fake-API
  style as the existing suite.
- Gateway: Python wrappers for the above.
- Health domain: rename `SimpleFinAccountInfo` to a provider-neutral
  `RemoteAccountInfo` (keep the old name as an alias), carry `provider` and a
  "linked by Clerk" overlay into `evaluate_accounts`, generalise the wording of
  statuses that currently say "SimpleFIN".
- Database: `plaid_items` and `bank_links` tables and accessors, migration.
- Dev tooling: the host has no Node, so document and script running the worker
  tests through the `node:22` container.
- Checkpoint: all existing behaviour unchanged, suites green.

### Stage 2: Plaid client, sandbox linking, and account mapping

- `clients/plaid.py`: raw `httpx` client with pinned `Plaid-Version`, typed
  errors (repairable codes flagged), link token create (new and update mode),
  public token exchange, `/item/get`, `/accounts/get`, `/item/remove`, and the
  sandbox helpers.
- Settings: client id, secret, environment, `days_requested`, redirect URI,
  refresh policy, delete-removed-pending policy, plus env-var equivalents.
- UI: "Connect a bank" on the Connections page opens Plaid Link (script from
  `cdn.plaid.com`, OAuth re-init from a stored link token), a sandbox-only
  "Create sandbox connection" button, an account-mapping dialog per Item
  (existing Actual account or create one, cutover date, off-budget), Items list
  with slot usage, repair via update mode, remove.
- Health: Plaid balances and item status feed `RemoteAccountInfo`; the
  Connections page reads correctly for a mixed SimpleFIN and Plaid budget.
- Checkpoint: a sandbox Item linked and mapped against a throwaway Actual
  server, health showing Plaid balances, no transactions imported yet.

### Stage 3: Plaid transaction sync

- Sync engine: refresh according to policy, wait for item status, cursor sync
  with pagination and mutation retry, routing, cutover filtering, adoption of
  pending-to-posted and boundary rows, import through `importTransactions`,
  removal policy (uncleared and untouched rows only; cleared rows are flagged,
  never deleted), cursor persisted only after a successful import.
- Job integration: the sync job runs the Plaid phase before the native bank
  sync, records per-Item events, degrades one Item at a time, marks
  `ITEM_LOGIN_REQUIRED` and friends as "needs repair" for health.
- A `compose.sandbox.yml` with a throwaway Actual server for end-to-end runs.
- Checkpoint: sandbox transactions flow into Actual, categorisation and tagging
  run on them, pending-to-posted swaps do not duplicate, repair flow works.

### Stage 4: migration tooling, both directions

- Per-account migration in the UI: "Move to Plaid" (choose Plaid account, set
  cutover, unlink SimpleFIN in Actual, preview what adoption would touch, then
  apply) and "Move back to SimpleFIN" (disable the Plaid link, relink in Actual
  with the cutover as starting date, verify the server token).
- Actual server SimpleFIN token management (status, set, clear) from Clerk.
- Dry-run previews everywhere a write to Actual happens.
- Digest, README, architecture doc, and `.env.example` updated; the SimpleFIN
  wording made provider-neutral.
- Checkpoint: a sandbox account migrated forward and back without duplicates.

### Stage 5: production cutover

Runbook, executed once the Trial account exists: switch the environment to
production, link real banks (slot budget is ten, forever), migrate one account
at a time with SimpleFIN left running for a settling period, compare balances
via the health page, then unlink the rest and clear the server token. Reverse
path documented alongside.

## 5. Open questions to settle as we go

- Where the sandbox Actual server should run for testing (a local throwaway
  container is assumed).
- Whether Plaid `personal_finance_category` should be offered to the
  categorizer as a hint. It is cheap evidence, but Clerk's rule is that only
  the user's own history authorises an automatic filing.
- Refresh policy default: once per sync run per Item, or only for the morning
  report run.
