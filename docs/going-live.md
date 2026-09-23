# Going live with `feature/plaid`

The branch carries three pieces of work: the Plaid engine and migration
tooling (`docs/plaid-migration/`), anticipated charges from the phone
(`docs/anticipated-charges.md`), and Clerk-owned rules under Intelligence
(`docs/intelligence/`). All three have been reviewed and patched. This is the
order to roll them onto the live stack while SimpleFIN keeps feeding the
banks, and what to do the day the Plaid Trial account arrives.

The live stack is `$MNT_HOME/docker/stacks/shiro/actual-budget/compose.yaml`
on shiro: `actual-budget` on :30030 and `actual-clerk` on :30031, data under
`$MNT_HOME/.local/share/actual-budget`. Nothing in this release needs a
change to that compose file beyond the image tag.

## Part 1: now, still on SimpleFIN

### 0. What is already done on this machine

- The release image was built and pushed with the shared builder
  (`build actual-clerk --major`) as `valentemath/actual-clerk:v1.0.0`, also
  tagged `latest`, from this checkout.
- `scripts/transfer-lab-intelligence.py` is written and was verified against a
  copy of the live Clerk database: the phone source, the three charges (two
  open), the taught alias, one hand-made rule, and the phone-app settings
  (including the device token) all come across, and a second run copies
  nothing. The new tables and columns are created on the way.
- The phone app on the Pixel needs no rebuild. It only needs its server URL
  changed from the lab to the live Clerk.

### 1. Publish the image (from this machine)

```fish
build actual-clerk --major     # → valentemath/actual-clerk:v1.0.0 and latest
```

The live compose file follows `latest`. The version on the Clerk footer says
which build is running.

### 2. Stop live Clerk (on shiro)

Only the Clerk container. Actual keeps running; SimpleFIN keeps running
inside Actual.

```bash
docker compose -f "$MNT_HOME/docker/stacks/shiro/actual-budget/compose.yaml" stop actual-clerk
```

Clerk's database lives on the TrueNAS share, and SQLite cannot be shared
across hosts, which is why the transfer in the next step refuses to run
unless you say Clerk is stopped.

### 3. Carry the lab's phone intelligence over (from this machine)

Done on 2026-09-12 at 17:21. What was learned doing it: SQLite's own
locking and backup calls hang against the TrueNAS SMB share, so the script
must not be pointed at the share directly. Copy the database to local disk,
run the transfer there, and copy the result back. Clerk's write-ahead log is
empty once the container is stopped, so a plain file copy is consistent.

```bash
cd ~/forge/actual-clerk
LIVE="$MNT_HOME/.local/share/actual-budget/clerk/data"
WORK=$(mktemp -d)
cp "$LIVE/clerk.db" "$WORK/clerk.db"
uv run python scripts/transfer-lab-intelligence.py \
  --source lab/data/clerk/clerk.db --target "$WORK/clerk.db" --dry-run
uv run python scripts/transfer-lab-intelligence.py \
  --source lab/data/clerk/clerk.db --target "$WORK/clerk.db" --i-stopped-clerk
cp "$WORK/../backups/"clerk-before-lab-transfer-*.db "$MNT_HOME/.local/share/actual-budget/clerk/backups/"
cp "$WORK/clerk.db" "$LIVE/clerk.db"
rm -f "$LIVE/clerk.db-wal" "$LIVE/clerk.db-shm"
```

Result: 1 source, 3 charges, 1 alias, 1 rule, 4 settings; integrity ok; the
pre-transfer copy is `clerk/backups/clerk-before-lab-transfer-2026-09-12_17-20-57.db`.
The two open charges arrive with their lab categories; live Clerk re-derives
those on its first refresh from its own rules and history, so they may read
as "Uncategorized" until step 6.

What deliberately does not move: the 71 rules the lab imported from Actual
(the live budget still holds those rules in Actual, so they are imported from
there in step 6), the lab's 13 open proposals (live proposes its own), and
the lab's Plaid sandbox Items and mappings.

### 4. Start the new Clerk (on shiro)

```bash
docker compose -f "$MNT_HOME/docker/stacks/shiro/actual-budget/compose.yaml" pull actual-clerk
docker compose -f "$MNT_HOME/docker/stacks/shiro/actual-budget/compose.yaml" up -d actual-clerk
docker logs -f actual-clerk
```

Then check, in Clerk at :30031:

- The footer shows the new version (1.0.0).
- **Connections** shows a *Phone notifications* panel with the Capital One
  source on the Pixel, pointed at the Venture card, and two charges *Waiting
  for the bank* (Valve $3.19, Geico $152.40).
- **Overview** counts those two under anticipated charges.
- **Intelligence** exists between Review and Connections, with one rule
  (Chipotle) and one alias (valve → steam).
- **Settings → Phone app** shows the device token as set.

The first filing run after the upgrade also reads every decision Clerk ever
applied and compares it with what Actual holds now
(`docs/intelligence/stage-4.md`). Expect an Activity line such as "Read N
correction(s) made in Actual" once; that is the observation channel catching
up, not a fault. Proposals may appear under Intelligence as a result.

### 5. Re-point the phone

On the Pixel: open Actual Clerk → **Server settings** → replace the lab URL
with the live one (the address you open Clerk at from the phone, port 30031)
→ **Connect**. The token is unchanged. The home screen should show the live
budget line and the Capital One source still registered (the registration
travelled with the database, keyed by the phone's device id). **Recent
charges** should list the two charges from the live ledger.

Make a small purchase on the Venture card to prove the path end to end. The
next SimpleFIN sync that carries the Geico row will settle the Geico charge
and teach the alias between "Geico" and whatever the bank posts it as.

Then take the lab down, so its hourly SimpleFIN sync stops and nothing else
points at it:

```bash
docker compose -f lab/compose.yml down
```

`lab/data` is kept; refresh it from live before the next lab session
(`docs/plaid-migration/lab.md`).

### 6. Let Clerk take the simple rules over from Actual

The runbook is in `docs/intelligence/stage-2.md`; the short form:

1. **Intelligence → From Actual → Read Actual's rules.** Expect around 60
   rules: about 52 movable, 8 kept (4 set a transfer payee, 4 delete a
   transaction). Anything marked *partly* has a problem that says what to
   fix (usually a collision or a Clerk rule that disagrees).
2. **Preview import**, then **Import**. Both systems now answer; Actual
   still files at import, so nothing changes in the budget yet. The two open
   phone charges should now show their category *by a rule you set*.
3. Let a sync or two run. Then **Preview retire**, then **Retire in Actual**.
   From here Actual's imports arrive uncategorized and the categorize job
   that follows each sync files them by rule; check Activity after the next
   sync shows `source = rule` applications and nothing sitting in Review
   that a rule should have caught.
4. If anything looks wrong, **Restore to Actual** puts the rules back from
   the copies Clerk kept.

Optional: **Propose rules from history** asks for a rule wherever your
history is unanimous; **Decline all** clears the rest.

### 7. Settings worth a look after the upgrade

| Setting | Default | Note |
| --- | --- | --- |
| Intelligence → learn from Actual | on | The observation channel; corrections in Actual become evidence and disputes |
| Intelligence → dispute threshold | 2 | Corrections against one rule before Clerk proposes changing it |
| Intelligence → ask the model about same-merchant names | off | One extra model call per unfamiliar merchant; leave off unless you want alias proposals |
| Phone app → match window / expiry | 10 / 14 days | Capital One posts within a few days; the defaults fit |

Nothing else changed defaults. SimpleFIN, the model, the digest, and
notifications carry the settings the live database already had.

## Part 2: when the Plaid Trial account arrives

The full runbook, written against the live stack when the Trial
credentials arrived, is `docs/plaid-migration/stage-5.md`. The mechanics
were all verified in the lab (`docs/plaid-migration/stage-2.md` to
`stage-4.md`); this is the short order.

1. **Credentials.** Settings → Plaid: client id, the *production* secret,
   environment `production`. *Test connection* mints a link token and proves
   the keys. If any of your banks use OAuth (most large US banks do), set a
   redirect URI: it must be https and registered in the Plaid dashboard,
   which means Clerk needs to be reachable at an https address for the link
   flow (a Tailscale or reverse-proxy address is fine).
2. **Link banks one at a time.** Connections → *Connect a bank*. The Trial
   gives ten Items for life and removals do not give a slot back, so link
   each institution once and map every account you want from it. Capital One
   can be linked too; it just stays slow, which is what the phone app is for.
3. **Migrate one account first**, a quiet one. Connections → the account →
   *Move to Plaid*: choose the Plaid account, cutover date today, leave
   *unlink Actual* on. **Dry run** shows rows to import, rows Actual would
   match itself, rows adopted, and whether an opening balance is added.
   Apply. Watch the queued sync in Activity and the connection's health
   reading (Plaid's balance versus Actual's cleared balance should be within
   the tolerance).
4. **Settle for a few days.** Compare the account against the bank's own
   statement. If it goes wrong, *Move to SimpleFIN* is the road back: it
   pauses the Plaid mapping and relinks the account in Actual with the
   remembered SimpleFIN account, and Actual adopts the rows Clerk delivered
   rather than duplicating them.
5. **Migrate the rest**, one per day or so, the same way. An account left in
   the *Fed twice* state past its cutover window will duplicate rows; the
   Connections page flags it.
6. **Retire SimpleFIN.** Once no account is SimpleFIN-linked: Settings →
   SimpleFIN → *Remove* the Actual server's token, and clear
   `simplefin_access_url` in Clerk's settings. SimpleFIN's own subscription
   can lapse after that.
7. **Phone charges keep working throughout.** Anticipations settle against
   whichever feed delivers the row; the provider does not matter to them.

## If something goes wrong

- **Roll the image back**: set the compose tag back to the previous image and
  `up -d`. The new tables are ignored by the old build; the backup from step
  3 restores the database exactly as it was if you would rather have that.
- **Rules**: *Restore to Actual* on the Intelligence page recreates every
  retired rule from the stored copy.
- **Phone**: pausing a source on the Connections page stops forwarding
  without unregistering; removing it drops its charges.
