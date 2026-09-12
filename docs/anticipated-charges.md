# Anticipated charges from phone notifications

Some card feeds are slow by design. Capital One, for one, ignores Plaid's
on-demand refresh and never exposes pending transactions, so a purchase made
this morning reaches Actual a day or three later. The card's own phone app,
though, announces the charge the moment it is authorised.

Clerk closes that gap with a small Android companion app. It listens for the
card app's notifications and forwards them to Clerk, which turns each into an
**anticipated charge**: money the budget treats as spent right now, held in
Clerk's own ledger, and **never written into Actual Budget**. When the bank's
transaction finally arrives through the ordinary feed (Plaid or SimpleFIN),
Clerk settles the anticipation against it and the real row takes over.

```text
card charged ──▶ card app notification ──▶ companion app ──▶ POST /api/anticipated/device/notifications
                                                                     │
                       Clerk reads amount, direction, merchant ◀─────┘
                       and stores an anticipated charge (open)
                                   │
   overview / digest count it as spent ──▶ bank feed imports the row ──▶ settled (matched)
                                                    or nothing arrives ──▶ expired after N days
```

## What Clerk does with a notification

1. **Reads it.** The amount is the first money figure in the text. The
   direction comes from the wording: *declined* is recorded but never counted;
   *credit*, *refund*, *returned*, *payment received* and the like are credits,
   recorded and shown but not counted as spending; anything else with an amount
   is a charge. The merchant is the phrase after "at" (or "from"), trimmed of
   the card, the time, and the outcome. The wording is the issuer's, so the
   parser is generous about form and strict about substance: no amount, nothing
   anticipated.
2. **Stores it once.** The phone sends a stable key per notification, so a
   retry, a reboot, or an app re-posting the same notification never produces
   a second charge.
3. **Counts it.** An open charge on an on-budget account is added to the
   month's spending as if it were discretionary, because nothing yet says which
   category it will land in and the conservative reading of a charge is that it
   competes for free money. Free money itself is untouched, so it still equals
   Actual's Projected Savings. The Overview, the morning report, and the phone
   all show the figure.
4. **Settles it.** Every time the overview is rebuilt (after a sync, a filing
   run, a connection check, the morning report, or a manual refresh), each open
   charge is compared with the transactions Actual holds: same account, exactly
   the same amount, dated from one day before the notification up to the match
   window after it. Among candidates, one whose merchant normalises to the same
   key as the notification's wins, then the nearest date, then a bank-imported
   row over a hand-entered one. Each transaction settles at most one charge,
   and earlier notifications choose first, so two identical coffees on one
   morning settle one each.
5. **Lets go.** A charge nothing has settled after the expiry period stops
   counting: the bank is evidently never going to post it (a hold that was
   released, an authorisation that was reversed). It stays in the ledger as
   *never posted*.

Every step is visible. The Connections page lists the phone sources, what is
waiting for the bank, and what recently settled; a charge can be dismissed by
hand, and a wrong match reopened. Nothing in Actual ever changes because of
this feature.

## Setting it up

### 1. Clerk

**Settings → Phone app.** Turn the feature on (it is on by default), and set a
**device token**: any string you like, entered again in the phone app. Clerk
has no login of its own, and these are the only endpoints an outside device
writes to, so the token is what stops a stray device on the network from
feeding the budget. The match window and expiry are there too.

| Variable | Default | Meaning |
| --- | --- | --- |
| `CLERK_ANTICIPATED_ENABLED` | `true` | Count charges the phone has seen |
| `CLERK_ANTICIPATED_DEVICE_TOKEN` | — | Token the phone must present |
| `CLERK_ANTICIPATED_MATCH_WINDOW_DAYS` | `10` | Days after the notification the bank's row may still be dated |
| `CLERK_ANTICIPATED_EXPIRE_DAYS` | `14` | Days before an unposted charge stops counting; at least the window |

### 2. Build the phone app

The host needs Docker and nothing else; the whole Android toolchain runs in a
container and the APK is exported as a build artifact:

```bash
scripts/build-android.sh
# → dist/android/actual-clerk-companion.apk
```

The first run creates a signing key in `android/keystore/` (ignored by git)
and a random password beside it. Keep them: Android only installs an update
over an app signed with the same key, so a lost key means uninstalling the app
before the next build. The version comes from `pyproject.toml` and the version
code from the commit count; both can be overridden with `APP_VERSION` and
`APP_VERSION_CODE`.

Plain `docker build` works as well:

```bash
docker build -f android/Dockerfile --target apk --output type=local,dest=dist/android android
```

Without a key in `android/keystore/` this generates a throwaway one so the
build still yields an installable APK.

### 3. Install and pair

Copy the APK to the phone and open it (Android asks to allow installs from
that source). Then:

1. **Connect.** Enter the URL you open Clerk at from the phone and the device
   token. The app checks the connection and lists the Actual accounts Clerk
   knows about.
2. **Grant notification access.** The home screen links to the system page.
   On Android 13 and later an app installed outside the Play Store first needs
   *Allow restricted settings* from its App info menu (the three dots, top
   right), after which the toggle becomes available.
3. **Register a source.** The registration screen lists every notification
   currently on the shade, with its app. Tap the card app's notification (make
   a small purchase first if nothing is showing), then pick the Actual account
   its charges belong to. From then on every notification from that app is
   forwarded.

The phone shows the budget's remaining free money after each charge, and a
log of what each notification became. Sources can also be paused, moved to a
different account, or removed from Clerk's Connections page.

Delivery is durable: a notification seen while the phone is away from home
is queued and sent when it is back, with retries. Consider excluding the app
from battery optimisation so the listener is not stopped; the system binds it
itself once access is granted.

## What it is not

- **Not a bank feed.** Anticipated charges never create, modify, or delete a
  transaction in Actual. They exist so the budget is honest between the card
  and the bank.
- **Not a categoriser.** A charge is counted as discretionary until the real
  row is imported and filed; the notification's merchant is only used to pick
  the right row when several share an amount.
- **Not a balance.** Connection health compares bank balances with Actual's
  cleared balance exactly as before; anticipated charges do not enter that
  comparison.

## Data

Two tables in Clerk's SQLite: `notification_sources` (one app on one phone,
pointed at an Actual account) and `anticipated_charges` (one per notification,
with the parsed amount, merchant, the raw title and text, and how it was
settled: `open`, `matched`, `expired`, `dismissed`, or `ignored` for a declined
or unreadable notification). Deleting a source deletes its charges. Moving a
source to another account moves only its open charges; settled history stays
where it settled.

## Endpoints

Phone (gated by the device token when one is set):

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/anticipated/device/hello?device_id=` | Proves the token; lists accounts, this device's sources, the budget summary |
| `POST` | `/api/anticipated/device/sources` | Register an app on this phone against an Actual account |
| `DELETE` | `/api/anticipated/device/sources/{id}?device_id=` | Unregister it |
| `POST` | `/api/anticipated/device/notifications` | Forward one notification; replies with the charge and the budget as it now stands |
| `GET` | `/api/anticipated/device/charges?device_id=` | This device's recent charges |

Web UI:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/anticipated` | Sources, open charges, recently settled, settings |
| `PATCH` | `/api/anticipated/sources/{id}` | Move to another account, pause, resume |
| `DELETE` | `/api/anticipated/sources/{id}` | Remove a source and its charges |
| `POST` | `/api/anticipated/charges/{id}/dismiss` | Stop counting a charge now |
| `POST` | `/api/anticipated/charges/{id}/reopen` | Count it again after a wrong match |
