# Stage 4: migration tooling, both directions

An account's feed can now be moved between SimpleFIN and Plaid from Clerk,
with a dry run before every write, and the road back is a first-class
action rather than a manual repair.

## The state of an account

Nothing is stored; the state is derived on every read from two facts: what
Actual itself links the account to (`account_sync_source`) and whether Clerk
holds an enabled mapping.

| State | Meaning |
| --- | --- |
| `simplefin` | Actual links and imports it; Clerk verifies |
| `plaid` | Clerk delivers it; Actual sees a manual account |
| `both` | Actual still links it *and* Clerk delivers it. A migration passes through this; staying there duplicates rows once the cutover window passes. The Connections page marks the account **Fed twice** |
| `plaid_paused` | A mapping exists but is off (moved back, or its Item was removed) |
| `manual` | Nothing feeds it |

`GET /api/migration` returns every open account with its state, Actual's own
link, Clerk's mapping, health status, and the SimpleFIN account it follows
or used to follow, plus the Plaid Items with their unmapped accounts and the
Actual server's SimpleFIN status.

## Move to Plaid

`POST /api/migration/to-plaid` with the Actual account, the Plaid account,
a cutover date, and `unlink_actual` (default on).

- **Dry run** reads the Item's whole stream from the beginning without
  touching the stored cursor, plans the account exactly as the engine would,
  and asks Actual for a dry-run import of the rows that would be new. The
  answer says how many rows import, how many Actual would match itself, how
  many older rows stay out, which existing rows are adopted (with their old
  provider ids), whether an opening balance would be added and its size, and
  whether Actual's own link is removed.
- **Apply** unlinks the account in Actual (if asked and linked), stores the
  mapping with `previous_provider` and `previous_external_id` remembered, and
  queues a sync. The mapping's first delivery is served from the start of
  the Item's stream (see backfill), so an account added to a connection that
  has been syncing for weeks still gets everything from its cutover.

## Move to SimpleFIN

`POST /api/migration/to-simplefin` with the Actual account, optionally the
SimpleFIN account (the remembered one by default), and a starting date.

- **Dry run** reports the SimpleFIN account Actual would follow, the date,
  whether Clerk's Plaid mapping would be paused, and warnings: no server
  token, SimpleFIN not returning that account, or Actual already linking it.
- **Apply** pauses the mapping, then links the account in Actual through
  `simplefin-accounts-link` with `upgradingId` and `startingDate`. Actual
  runs its first sync immediately, and because its bank-sync path relaxes
  the id check, rows Clerk delivered from Plaid in the overlap are adopted
  by Actual rather than duplicated. A sync is queued afterwards.

## Backfill

An Item's cursor is shared by all its accounts. A mapping created after the
Item has been read would otherwise never see the history the cursor moved
past. The engine now serves any never-delivered mapping from the start of
the stream first (a separate read with an empty cursor, filtered by the
mapping's cutover as always), then runs the normal incremental read for
every mapping and persists the cursor. Reported as `plaid_backfilled`.

## The Actual server's SimpleFIN token

`GET /api/simplefin/server` (optionally with accounts), `POST` and `DELETE
/api/simplefin/server-token`. Storing takes the same setup token Actual's
own Settings would; the server claims it itself. Removing clears both the
token and the access key the server derived from it. Both need an admin
login on the Actual server, which is what Clerk's password provides on a
single-user install. Settings → SimpleFIN shows the state with Store,
Replace, and Remove.

## Wording

The digest, the Connections page, the Settings intro, and the health
statuses no longer name SimpleFIN where the provider may be Plaid. Remedies
on the Connections banner are per provider.

## Verified in the lab

On the dormant "Valley Checking" account (SimpleFIN-linked, ten rows):

1. `GET /api/migration` listed all nine accounts with the expected states,
   the server token present, and six SimpleFIN accounts visible.
2. Dry run to a fresh sandbox Item's checking account with today's cutover:
   three rows would import, none adopted, no opening balance (the account
   is not empty), and Actual's link would be removed. State unchanged
   afterwards.
3. Apply: SimpleFIN unlinked, mapping stored remembering `simpleFin` and the
   SimpleFIN account id, the queued sync delivered six rows (the sandbox
   generated more after its refresh), every import id unique.
4. Dry run back: the remembered SimpleFIN account preselected, mapping to be
   paused, no warnings.
5. Apply: Actual relinked the account (`bank_sync_status: ok`, bank name
   restored), the mapping paused, state `simplefin`, rows unchanged, no
   duplicates, and the next sync delivered nothing from Plaid for it.

The migration Item was removed afterwards; the original sandbox Item and its
two mapped accounts remain for ongoing lab syncs.

## Notes

- After moving back to SimpleFIN, rows Clerk delivered from Plaid keep their
  Plaid ids until Actual's own sync matches them by amount and date, which
  happens on the first SimpleFIN sync covering that window.
- Moving to Plaid with `unlink_actual` off is allowed for a deliberate
  overlap test and is flagged everywhere as **Fed twice**.
