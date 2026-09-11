# Stage 1: provider-neutral foundations

No Plaid code yet. This stage gives Clerk the primitives to manage bank links
itself and stops the health model assuming SimpleFIN. Existing behaviour is
unchanged: with no Clerk-managed links, every account is scored exactly as
before.

## What changed

### Worker primitives (`actual_worker.mjs`)

The object `api.init()` returns carries `send()`, which reaches every Actual
server handler. The worker now keeps it (falling back to the deprecated
`api.internal`) and exposes:

| Method | Actual handler or API | Purpose |
| --- | --- | --- |
| `listAccountsDetailed` | `accounts-get` | Every account with its link columns and bank name. The AQL `accounts` table hides `bank` and the balance columns, which is why the handler is used |
| `accountTransactions` | AQL | One account's rows with `imported_id`, cleared, reconciled, transfer flags |
| `unlinkAccount` | `account-unlink` | Detach bank sync; account and rows stay |
| `simpleFinServerStatus` / `simpleFinServerAccounts` | `simplefin-status` / `simplefin-accounts` | Ask the Actual *server* about its SimpleFIN token and accounts |
| `linkSimpleFinAccount` | `simplefin-accounts-link` | Attach a SimpleFIN account to an existing (`upgradingId`) or new account, with `startingDate` |
| `setServerSecret` | `secret-set` | Store or clear `simplefin_token` on the server; any other name is refused |
| `createAccount` | `createAccount` | For mapping a Plaid account onto a new Actual account |
| `importTransactions` | `importTransactions` | Actual's own reconciliation and rules; supports dry run |
| `deleteTransactions` | `deleteTransaction` | Batched, reports ids already gone |
| `adoptImportedIds` | `updateTransaction` | Point an existing row at a new provider id, optionally settling cleared and date |

Each has a Python wrapper on `ActualGateway` and a worker contract test.

### Provider-neutral health (`domain/health.py`)

- `RemoteAccountInfo` replaces `SimpleFinAccountInfo` (kept as an alias) and
  carries `provider`. Errors carry `provider` too (default `simpleFin`).
- An account is only compared with readings and errors from its own provider,
  keyed by `(provider, external id)`. A SimpleFIN payload that omits a
  Plaid-fed account no longer reads as "missing".
- `providers={provider: configured}` joins `simplefin_configured`; a provider
  that answered at all counts as configured.
- Wording names the provider ("Plaid did not return this account"), and the
  `missing` label is now "Not returned by the bank".
- `ActualAccountInfo.managed_by_clerk` and the health row's `provider_label`
  are new fields for the UI.

### Link overlay (`reporting.apply_bank_links`)

Actual only knows the links it holds. `JobManager.snapshot()` now overlays
Clerk's `bank_links` onto every budget snapshot: a linked account's
`sync_source`, `external_id`, and `bank_name` come from Clerk's table, with
what Actual said preserved under `actual_sync_source`. Health, freshness, the
digest, and diagnostics all read the overlaid snapshot.

### Tables (`db.py`)

`plaid_items` (access token, cursor, status), `bank_links` (one per Actual
account Clerk feeds, with `cutover_date`), and `import_adoptions` (every id
rewrite). Upserts use `ON CONFLICT ... DO UPDATE`: an `INSERT OR REPLACE`
would have silently deleted another account's link on a `(provider, external
id)` clash instead of refusing it.

## Verified live against the lab budget

Run through the worker RPC inside `clerk-lab` (see [lab.md](lab.md)):

1. `listAccountsDetailed` returned all six accounts with SimpleFIN ids, bank
   names, `bank_sync_status`, and cached balances.
2. `simpleFinServerStatus` reported the server token configured;
   `simpleFinServerAccounts` returned the six SimpleFIN accounts with org
   name, domain, and id in the shape `simplefin-accounts-link` expects.
3. **Round trip on the dormant "Valley Checking" account**: `unlinkAccount`
   cleared the sync source, external id, bank, and cached balance; all ten
   transactions and their `imported_id`s survived. `linkSimpleFinAccount`
   with `upgradingId` and a starting date thirty days back restored the link
   (`bank_sync_status: ok`, balance re-cached) and imported nothing new: no
   duplicates, no synthetic starting balance, ten rows before and after.

That is the reverse migration path (Plaid back to SimpleFIN) proven at the
Actual level.

## Notes for later stages

- Server secrets need an admin session on the Actual server; `setServerSecret`
  surfaces Actual's `unauthorized` answer as an error rather than pretending.
- `importTransactions` on the public API always runs strict id checking, so
  the cutover adoption logic in Stage 3 is required, not optional.
- The AQL `accounts` table cannot see link columns; anything needing them goes
  through `accounts-get`.
