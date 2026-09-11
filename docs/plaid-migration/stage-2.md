# Stage 2: Plaid client, linking, and account mapping

Clerk can now hold Plaid connections (Items), map their accounts onto Actual
accounts, and score those accounts in the health check against Plaid's own
readings. Nothing is imported yet; that is Stage 3.

## What changed

### `clients/plaid.py`

A dependency-free client: one POST per call with the client id and secret in
headers, `Plaid-Version` pinned to `2020-09-14`, every answer reduced to
Clerk's shapes.

| Call | Plaid endpoint | Notes |
| --- | --- | --- |
| `create_link_token` | `/link/token/create` | New connection (products, `days_requested`, optional redirect URI) or update mode (`access_token`, no products) |
| `exchange_public_token` | `/item/public_token/exchange` | |
| `get_item` | `/item/get` | Institution name, `item.error`, consent expiry, transactions update timestamps |
| `get_accounts` | `/accounts/get` | Cached balances, free. Credit and loan balances are negated into Actual's sign |
| `remove_item` | `/item/remove` | Frees no Trial slot |
| `transactions_sync_page`, `transactions_refresh` | `/transactions/sync`, `/transactions/refresh` | One page at a time; the engine that pages and imports is Stage 3 |
| `sandbox_create_item`, `sandbox_reset_login` | `/sandbox/...` | Refused outside the sandbox |
| `test_connection` | `/link/token/create` | Minting a token is free and proves the keys and environment |

`PlaidError` carries `error_type`, `error_code`, `display_message`,
`request_id`, `retryable` (API and institution errors, 5xx, 429), and
`needs_repair` (the codes Link's update mode fixes: `ITEM_LOGIN_REQUIRED`,
`PENDING_EXPIRATION`, and friends). Amounts go through `Decimal`.

### Settings

`plaid_client_id`, `plaid_secret`, `plaid_env` (`sandbox` or `production`),
`plaid_days_requested`, `plaid_redirect_uri` (https only), `plaid_client_name`;
environment names `CLERK_PLAID_*`. `Settings.plaid_configured` gates every
Plaid path.

### `plaid_links.py`

`read_items` asks every live Item for its status and accounts, records the
outcome on the Item (`ok`, `needs_repair`, `error`, with the message and the
institution name Plaid reports), and never lets one Item's failure hide
another. `readings` folds that into provider-tagged health readings and
errors; `describe` folds it into what the Connections page shows, with each
Plaid account carrying its mapping if it has one.

### API

| Endpoint | Purpose |
| --- | --- |
| `GET /api/plaid/items` | Items, accounts, balances, mappings, slot usage, and the Actual accounts available to map onto |
| `POST /api/plaid/link-token` | New connection, or update mode when `item_id` is given |
| `POST /api/plaid/exchange` | Exchange Link's public token and keep the Item |
| `POST /api/plaid/sandbox/items` | Create a sandbox Item without the Link UI |
| `POST /api/plaid/items/{id}/repaired` | After update mode: re-read the Item, clear the flag if healthy |
| `POST /api/plaid/items/{id}/remove` | Disconnect at Plaid, pause its mappings |
| `POST /api/plaid/sandbox/items/{id}/reset-login` | Break a sandbox login to rehearse repair |
| `POST /api/plaid/links` | Map a Plaid account onto an existing Actual account or a new one Clerk creates, with a cutover date |
| `PATCH` / `DELETE /api/plaid/links/{actual_account_id}` | Pause, resume, move the cutover date, or forget a mapping |
| `POST /api/settings/test/plaid` | Credential test |

Access tokens are stored in Clerk's database and never returned. A mapping
refuses a Plaid account already mapped elsewhere (409) and an Actual account
already mapped to a different Plaid account (409).

### Health

The health job reads every Item after SimpleFIN. Readings arrive as
`RemoteAccountInfo(provider="plaid")`, Item errors as provider-tagged errors
keyed by Item id, and `providers={"plaid": ...}` says whether Plaid was asked.
Clerk's link carries the Item id onto the account (`connection_id`), which is
what lets a broken Item's error reach its accounts even though the Item
returns no readings at all. Plaid rarely gives a balance timestamp, so the
Item's `last_successful_update` stands in as the balance date for staleness.

### UI

- Connections page: a **Plaid connections** panel with environment and slot
  usage, one row per Item (status, accounts, mapped count, bank data age),
  and per-row Map accounts, Repair, Break login (sandbox), Remove.
- A drawer per Item lists its accounts with balances and a mapping form each:
  choose an Actual account (existing ones show what Actual links them to, so
  a mid-migration state is visible) or create one, set the import date, then
  pause, resume, move the date, or unmap.
- **Connect a bank** opens Plaid Link (`link-initialize.js` from
  `cdn.plaid.com`); on success the public token goes to `/api/plaid/exchange`
  and the drawer opens on the new Item. Repair opens update mode and calls
  `/repaired` afterwards. OAuth banks return through the configured redirect
  URI; the link token is kept in `sessionStorage` and Link resumes with
  `receivedRedirectUri`.
- Settings: a Plaid section with a credential test.
- Remedies on the Connections banner are per provider.

## Verified

- Against the real Plaid sandbox, from a scratch script: link token, sandbox
  Item creation and exchange, `/item/get`, `/accounts/get` (credit balance
  negated), one `/transactions/sync` page (`NOT_READY` right after creation),
  `/transactions/refresh`, `/sandbox/item/reset_login`, the resulting
  `ITEM_LOGIN_REQUIRED` from `/accounts/get` while `/item/get` still answers,
  an update-mode link token, and `/item/remove`.
- Through the lab Clerk's API: a sandbox Item created, both its accounts
  mapped onto **new** Actual accounts in the lab budget (created through the
  worker), a health check scoring them under Plaid with Plaid's balances
  (checking `+500.00`, credit `-500.00`), then the login broken and the next
  health check flagging the Item as needing repair.
- Not verified here: the Link UI itself, which needs a browser. Sandbox Link
  works over plain HTTP for non-OAuth institutions; open the lab at port
  30131, Connections, **Connect a bank**, and use `user_good` / `pass_good`.

## Notes

- A health run that starts before a mapping is created can overwrite the
  overview with its older snapshot; the next run corrects it. Cosmetic, but
  worth knowing when reading the page right after mapping.
- Plaid's `current` balance is the posted balance for depository accounts,
  which is what Clerk compares against Actual's cleared balance. Stage 3 will
  confirm this against sandbox transactions.
