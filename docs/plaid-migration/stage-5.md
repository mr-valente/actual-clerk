# Stage 5: production cutover runbook

Written 2026-09-23 against the live stack, once the Plaid Free Trial
credentials existed. Nothing here needs a code change: live Clerk 1.0.2
carries every Plaid piece from stages 1 to 4, and the mechanics below were
each exercised in the lab (`stage-2.md` to `stage-4.md`).

## Where things stand

| | |
| --- | --- |
| Clerk | 1.0.2 at `http://shiro:30031` (Tailscale), settings held in its own database, no Plaid keys yet, environment `sandbox` |
| Actual | 26.9.0 at `http://shiro:30030`, six open accounts, all linked to SimpleFIN by Actual itself |
| Clerk's SimpleFIN | access URL stored; hourly sync and health, balance tolerance $1.00 |
| Plaid | Free Trial: production keys, ten Items for the life of the team, removing one gives nothing back, Transactions and Transactions Refresh included |

The six accounts and the Plaid connection each one needs:

| Actual account | Institution (SimpleFIN name) | Item | Notes |
| --- | --- | --- | --- |
| FFFCU Checking | First Financial FCU of Maryland | 1 | Active |
| FFFCU Savings | First Financial FCU of Maryland | 1 | Same login as checking; monitoring off, last row April |
| Capital One Venture Card | Capital One | 2 | OAuth bank; the phone app anticipates its charges |
| Wells Fargo Reflect Card | Wells Fargo | 3 | OAuth bank |
| Amazon Store Card (3336) | Amazon Store Card (Synchrony) | 4 | Search "Amazon Store Card" or "Synchrony" in Link |
| Valley Checking | Valley National Bank - Personal | 5 | Dormant since June, monitoring off. Optional |

One Item per bank login, so four or five of the ten slots. Everything a bank
shows in Link should be selected when that bank is connected: Clerk's UI
does not add accounts to an existing connection later (the API can, see the
end of this note), and reconnecting the bank would spend a second slot.

## Part A: the Plaid dashboard

1. **Keys.** Dashboard → Developers → Keys. Copy the `client_id` and the
   **Production** secret. The sandbox secret does not work against
   production and vice versa.
2. **Compliance center** (Dashboard → Settings → Compliance, or the
   Launch Center prompts). Complete *Application display information* (app
   name, website, a logo if asked; this is what the bank shows the user
   when they authorise), *Company information*, the *Master Services
   Agreement*, and the *Security questionnaire*. Capital One and Wells Fargo
   are OAuth institutions and will refuse an app with an empty profile.
   Submit the questionnaire early: most institutions unlock within hours,
   some take up to five business days.
3. **OAuth institutions page** (Dashboard → Settings → Compliance → OAuth
   institutions, or search "OAuth" in the dashboard). Wait until Capital One
   and Wells Fargo read as enabled before trying to link them. The credit
   union, Synchrony, and Valley are not gated this way.
4. **Redirect URI: leave it alone.** On a desktop browser Plaid opens the
   bank's OAuth page in a pop-up and returns on its own, no redirect URI
   needed. Only if a bank insists on a redirect (Link says so) register an
   https URL under Developers → API → *Allowed redirect URIs* and set the
   same value in Clerk. The cleanest way to get one is `tailscale serve` on
   shiro pointing `https://shiro.chaco-bushi.ts.net` at port 30031, then
   `https://shiro.chaco-bushi.ts.net/plaid-oauth`; Clerk serves its page on
   any path, and its Link code resumes from that address.
5. **Do not put the production keys in the lab.** Every Item created with
   them, from anywhere, counts against the ten. The lab stays on sandbox
   keys or stays down.

## Part B: Clerk

### 1. Back up first

- Actual: Settings → Export budget, keep the zip. The migration never
  deletes an account or a transaction, but this is the one-click undo.
- Clerk: nothing to do. Its database on the share gets the access tokens as
  banks are linked; the daily state of Clerk is rebuilt from Actual anyway.

### 2. Enter the keys

Settings → Plaid:

| Field | Value |
| --- | --- |
| Client ID | from the dashboard |
| Secret | the Production secret |
| Environment | Production |
| History to request (days) | 90 (the default). This is fixed per connection at link time, so decide before connecting. 90 covers every adoption window |
| OAuth redirect URI | blank |
| Name shown in Link | Actual Clerk, or whatever the compliance profile says |

Leave the engine toggles at their defaults (deliver on, refresh on, refresh
at most every 55 minutes, wait 45 seconds, delete withdrawn pendings on,
opening balance on, adoption window 14 days). Save, then **Test
credentials**: it mints a real link token, which proves the keys and the
environment together. A failure naming the redirect URI means the dashboard
allowlist and Clerk disagree; a failure naming the secret means the sandbox
secret was pasted.

The Connections page now shows a *Plaid connections* panel reading "0 of 10
lifetime production connections used" and the sandbox-only button is gone.

### 3. Connect the banks

Do this from a desktop browser, at the Clerk address, with `cdn.plaid.com`
reachable. For each bank:

1. Connections → **Connect a bank**. Link opens; search the institution.
2. Log in. Capital One and Wells Fargo hand off to the bank's own page in a
   pop-up; allow pop-ups for the Clerk address if the browser blocks it.
   Approve every account you want Clerk to see, then the bank returns you to
   Link.
3. Link finishes, Clerk exchanges the token, and the connection's drawer
   opens listing its accounts with balances. **Close the drawer without
   mapping** for accounts that already exist in Actual; mapping from here
   leaves Actual's SimpleFIN link in place and the account fed twice. The
   *Move to Plaid* action in the next step does the whole hand-over.
   The drawer's mapping form is for a Plaid account with no Actual
   counterpart (it can create one).
4. The panel shows the new row with "N accounts · 0 mapped" and the slot
   count goes up by one. Run **Check now** once; the connection should read
   as healthy. Plaid now starts pulling the requested history in the
   background; that can take from seconds to a few minutes.

Connect all four or five banks in one sitting if the OAuth page says they
are enabled, or as each one unlocks. Nothing changes in Actual until an
account is moved.

### 4. Move one account, then wait

Start with **Valley Checking** if it is being moved (dormant, muted, and
the same account the lab round trip used), otherwise **FFFCU Savings**. Then
FFFCU Checking, Amazon, Wells Fargo, and Capital One last, since it is the
busiest.

1. Connections → click the account's row → the drawer shows *Bank feed*
   with "Actual links it to SimpleFIN" → **Move to Plaid…**
2. Pick the Plaid account (institution · name · mask · balance). Leave
   *Import from Plaid starting* at today and *Unlink SimpleFIN in Actual*
   ticked.
3. **Preview.** Read every line:
   - "Plaid is still preparing this connection's history": wait a few
     minutes and preview again. Applying is safe too (the engine waits for
     the history), but the preview is worth having.
   - "N transactions would be imported (date to date)": today's rows and
     later. Usually zero to a handful; pending card charges count.
   - "N existing rows would be adopted rather than duplicated": rows
     SimpleFIN already delivered for the same days, listed with date,
     amount, and payee. Expected for cards with pending charges; each is
     matched by amount within the 14-day window and keeps its category.
   - "The account is empty, so an opening balance …": should **not**
     appear for any of these six accounts. If it does, the wrong Actual
     account was chosen.
   - "Actual's own link is removed": expected.
4. **Move to Plaid.** Clerk stores the mapping, unlinks SimpleFIN in Actual,
   and queues a sync. Activity shows `plaid_sync` with a refresh, the read,
   and the counts. Then **Check now** (or the drawer's *Sync bank and
   recheck*).
5. The account's drawer should now say "Clerk delivers this account from
   Plaid", the row should read *OK* with *Cleared vs bank* at $0.00 (the
   tolerance is $1.00; drift 0 was the lab result on both a checking and a
   credit account), and *Bank data age* should be recent.

Then leave it for two or three days. What to look for:

- **Balances agree** every hour: the account stays green on Connections and
  in the morning report's connections section.
- **Activity** shows `plaid_imported` counts that match what the bank's app
  shows posting, and no `plaid_error` lines.
- **Review** and the category filing behave as before; imports go through
  Actual's rules and then Clerk's own, whichever feed delivered them.
- **Pending charges** arrive uncleared and settle in place when they post
  (`plaid_settled` / adoption "posted"). A withdrawn pending charge is
  deleted while uncleared and reported if it had been cleared.
- **Payee names** may differ from SimpleFIN's: Plaid supplies a cleaned
  merchant name as the payee and keeps the bank's raw text as the imported
  payee, which is what merchant memory keys on. Expect a few Review items
  the first week while memory catches up on the new spellings.
- **Capital One** specifically: the phone app's anticipated charges settle
  against whichever feed posts the row, so nothing changes there.

If it goes wrong: the account drawer → **Move to SimpleFIN…** → the
remembered SimpleFIN account is preselected → Preview → apply. Clerk pauses
the Plaid mapping and Actual relinks the account from the chosen date and
adopts the rows Clerk delivered. This needs the Actual server's SimpleFIN
token, which is why step 6 comes last.

### 5. Move the rest

One per day, or as fast as confidence allows, exactly as in step 4. Between
moves the Connections page is honest about mixed state: SimpleFIN accounts
say "Actual links it to SimpleFIN", moved ones say "Clerk delivers this
account from Plaid", and anything fed by both is chipped **Fed twice**
(which should never appear if *Unlink SimpleFIN* stayed ticked).

Every sync during the mixed period runs the Plaid engine first and Actual's
own SimpleFIN sync second, so the snapshot after each run sees both.

### 6. Retire SimpleFIN

When every account you intend to move says "Clerk delivers this account
from Plaid" and has been stable for a week:

1. Settings → SimpleFIN → *Actual server token: stored* → **Remove**. This
   clears the token and the access key Actual derived from it. From here
   Actual cannot link or sync SimpleFIN; *Move to SimpleFIN* would first
   need a new token stored here.
2. Settings → SimpleFIN → Access URL → tick *Clear saved secret* → Save.
   Clerk stops asking SimpleFIN about anything; the health check reads
   Plaid alone.
3. Let the SimpleFIN subscription lapse.

Accounts left on SimpleFIN on purpose (Valley, say) mean skipping this step
and keeping both providers, which Clerk supports indefinitely.

## Afterwards

- **Repairs.** A bank that wants a fresh login shows the connection as
  *Needs repair* on Connections, with a **Repair** button that opens Link in
  update mode. That reuses the connection and spends no slot. Its accounts
  read as errors, not missing, until it is fixed.
- **Slots.** The panel's "N of 10" count is the truth; a removed connection
  keeps its slot spent. Prefer Repair over Remove and reconnect.
- **Refresh budget.** Hourly syncs ask each connection for one refresh an
  hour, far under Plaid's limits of 2 a minute and 120 an hour.
- **Adding an account to a connected bank** (a new card at the same login)
  is not in the UI. Mint an update-mode token with account selection and
  run Link with it from the browser console, or ask for the button:

  ```bash
  curl -s -X POST http://shiro:30031/api/plaid/link-token \
    -H 'content-type: application/json' \
    -d '{"item_id": "<item id from GET /api/plaid/items>", "account_selection": true}'
  ```

- **Not exercised outside the sandbox** before this runbook: OAuth pop-ups
  with real banks, and Plaid's real posting behaviour for pending swaps. The
  planner and engine tests cover both; the first week's Activity log is the
  check.
