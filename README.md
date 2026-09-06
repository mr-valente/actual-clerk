# Actual Clerk

Actual Clerk is a budgeting assistant that runs alongside [Actual Budget](https://actualbudget.org/). It files your transactions, watches your bank connections, and answers one question every morning:

> **How much money do I have left to spend this month?**

Actual stays the system of record. Clerk reads your budget, writes categories and tags back through Actual's own sync protocol, and keeps only the state it needs to be durable and reviewable.

It supports OpenAI-compatible **local** endpoints only. There are no hosted AI providers, and the model is the last resort rather than the first — most transactions never reach it.

---

## What Clerk does

### Files your transactions, and gets cheaper as it learns

Clerk answers the cheapest reliable question first:

1. **Memory.** Noisy descriptors are normalized to a stable merchant key — `SQ *BLUE BOTTLE 4471`, `TST* Blue Bottle Coffee`, and `BLUE BOTTLE COFFEE #4471 OAKLAND CA` are one merchant. If your own history has filed that merchant consistently, Clerk files it the same way. No model call.
2. **The local model**, once per *merchant* rather than once per transaction, shown your existing categories as a numbered list and your own comparable filings as examples. Its answer is always a proposal for review, never permission to alter a first-time merchant quietly.
3. **You**, for every first-time merchant and anything else Clerk cannot settle from reliable history. Clerk queues its best guess rather than guessing on your behalf.

Once Clerk has filed the same merchant the same way a few times, it offers to write a **native Actual rule**. From then on Actual applies it during import and the answer costs nothing at all.

### Tags what a category cannot express

A category says which budget line. A tag says what kind of spending it was. Clerk writes `#subscription`, `#recurring`, `#annual`, `#unusual`, and `#refund` — almost all derived from your transaction history rather than from a model — plus a `#clerk` marker so everything it touched is findable and reversible from inside Actual.

### Tells you when a bank connection breaks

This is the failure that costs the most and announces itself the least. A SimpleFIN link stops delivering, Actual keeps showing the last balance it saw, and you find out weeks later.

Clerk asks SimpleFIN directly and compares three independent signals per account: what SimpleFIN reports now, how old that answer is, and what Actual holds. A stale balance, a balance that disagrees with the bank's, an account that stopped being returned, or a reported error each become a visible status — and one notification when it changes, not one every hour.

Balances are compared cleared-against-posted, so money still waiting to clear the bank is never reported as a mismatch. It is shown separately instead, and it still counts as spent in your budget, because a committed charge is committed whether or not the bank has caught up. Transfer rules need one more distinction: Actual can infer the second half from a transaction imported on another account and mark both halves cleared before the second bank feed arrives. Clerk recognizes that provenance and, only when the inferred amount fully explains the gap, holds it out of the bank comparison until its own account import confirms it.

Actual's bank import is additive: it adds and updates what the bank sends, but it never withdraws a transaction the bank has stopped reporting. A charge that is dropped after Actual imported it therefore stays in the ledger forever as money that never clears — counting as spent, and invisible to the balance comparison, which deliberately looks only at cleared money. Clerk names any imported transaction still uncleared two weeks on, on the account's own row. It stays a remark rather than a status: the connection is working, and nothing is deleted on a bank's silence.

A balance mismatch must appear in three successively newer SimpleFIN balance snapshots before Clerk declares it or sends an alert. Polling the same `balance-date` three times is one observation, not three. SimpleFIN can publish a new balance one poll before the matching posted transaction set settles; keeping that observation pending prevents the familiar false “mismatch” followed by “restored” an hour later, while a disagreement that survives genuinely newer bank data still becomes visible.

Every account has a **monitoring switch** on the Connections page. Turn it off for a dormant or legacy account and Clerk keeps reading it and showing its numbers, but stops scoring it, stops alerting on it, and stops calling it stale. Nothing changes in Actual — the account stays linked and keeps importing.

An account that has merely gone quiet is never treated as broken. It gets a nudge naming it and how long it has been silent, not a red banner, so the dashboard, the sidebar badge, and the morning digest always agree on what actually needs attention.

### Reports what is actually left to spend

```
free money = expected income - what you have already committed
remaining  = free money - what you have spent since the 1st
```

Set your income and recurring bills up in Actual once — [the guides](docs/budget-setup.md) walk through it click by click — and the dashboard shows what is left in dollars and as a percentage, what is safe to spend today, whether you are ahead of or behind the month's pace, and where you land if the rest of the month looks like the start of it.

On Actual's Tracking Budget this needs no configuration in Clerk at all: budget your expected income and your bills, and Clerk's free money is exactly Actual's own **Projected Savings**, tracked against your spending as the month goes on.

Unspent budget carries forward to its own category for up to a year, so a variable bill absorbs its own swings and an annual charge budgeted a twelfth at a time is covered when it finally lands — instead of blowing a hole in that month.

### Finds the commitments you forgot about

Clerk detects anything billing on a schedule three or more times, tells you what it costs per month, flags subscription price increases, flags an expected charge that never arrived, and points out which ones have no budget set. It is the working list for the budget setup above.

### Sends one message every morning

An ntfy notification with your free money, the pace, what is safe to spend today, any bank connection that needs attention, and anything waiting for your review. A single optional block can also include the current balances of every monitored bank-linked account. When the spendable budget is unchanged from the previous delivered report, those repeated figures collapse to “Nothing to report” instead. If SimpleFIN's per-account balance timestamp moved, Clerk can say newer bank data arrived with no new discretionary spending; if it did not, Clerk says SimpleFIN exposed no newer balance timestamp. One a day, short enough to read on a lock screen.

---

## Getting started

### 1. Run it

```bash
cp .env.example .env
$EDITOR .env                 # Actual URL, password, and budget sync ID
docker compose -f compose.example.yml up -d
```

Open <http://localhost:8080>.

Your budget **Sync ID** is in Actual under *Settings*, after clicking **Show advanced settings**. Actual shows two identifiers there — Clerk requires the **Sync ID**, the one used to reach the budget on the server.

Clerk reads whichever budgeting method you use. The setup works best on Actual's **Tracking Budget**, whose "Projected Savings" is the same arithmetic as Clerk's free money — see [Budget setup](docs/budget-setup.md).

### 2. Connect SimpleFIN

Open **Connections → Connect SimpleFIN** and paste a setup token from your SimpleFIN Bridge. Clerk claims it once and stores the resulting access URL.

Generate a **new** token for Clerk rather than reusing the one Actual holds: a SimpleFIN setup token can only be claimed once, and Clerk's access is read-only either way. This step is optional — without it Clerk still checks freshness from Actual's own data, it just cannot see the bank's side.

### 3. Point it at a local model

In **Settings → Local model**, set the base URL of an OpenAI-compatible server and a model name. Ollama, llama.cpp, vLLM, and LM Studio all work.

```
CLERK_OPENAI_BASE_URL=http://host.docker.internal:11434/v1
CLERK_MODEL=qwen2.5:14b
# CLERK_MODEL_REASONING=medium
```

A mid-sized instruct model is plenty: Clerk asks it to pick a number from a list, not to reason about your finances. Clerk works without a model too — it files what it recognizes and queues the rest.

### 4. Set up the budget

Budget your recurring bills in Actual once, and leave everyday categories unbudgeted. That one-time setup is what makes the free-money number meaningful.

- **[Setting up a Tracking Budget](docs/tracking-budget.md)** — the closer fit. Clerk's free money equals Actual's own Projected Savings, and a year of bills can be budgeted in one action.
- **[Setting up an Envelope Budget](docs/envelope-budget.md)** — Actual's default method, with a little more monthly upkeep.

[Budget setup](docs/budget-setup.md) compares the two if you are unsure.

Clerk reads categories, payees, splits, and budget months through Actual's
official query and budget APIs. Merges and reorganizations therefore use the
same redirect resolution as Actual itself; the former integration-specific
[category issue](docs/bug-renamed-categories.md) is retained as historical
context rather than an active limitation.

### 5. Catch up on your existing transactions

A new install only files the last 45 days. To work through everything already in your budget, open **Review → Catch up on all history**. Clerk goes back over your whole retained history (`CLERK_HISTORY_LOOKBACK_DAYS`, two years by default) and asks the local model once per unfamiliar merchant — not once per transaction — so even a long history is a bounded number of calls. Watch it on the Activity page; the run is labelled *full history*.

Anything Clerk is not confident about lands in the review queue rather than in your budget.

The two manual actions have separate scopes. **Retry review queue** asks Clerk to
reconsider only the transactions already waiting, which is useful after changing
the model, settings, or classification code. **Catch up older history** searches
for other uncategorized transactions beyond the normal 45-day filing window.
Scheduled filing deliberately leaves open reviews alone so an unresolved
exception does not churn after every sync. A sync still removes a waiting item
when you have already resolved it in Actual by categorizing or deleting the
transaction, converting it to a transfer, or otherwise making it ineligible
for filing. Clerk retains that outcome in Activity without learning merchant
memory from a choice made outside Clerk.

### 6. Turn on the morning report

In **Settings → Notifications**, enable ntfy and pick a hard-to-guess topic on [ntfy.sh](https://ntfy.sh) (or point at your own server). Subscribe to the same topic on your phone. Then, under **Settings → Morning report**, set the delivery time and time zone.

The notification header is yours to name — *The Morning Report* by default — and stays the same every morning, so it is recognisable on a lock screen before a word is read. Everything the report actually says goes in the body, as a headline figure and a few short blocks, each of which can be switched off:

| Block | Shows |
| --- | --- |
| Free money left | The headline figure and how much of the month remains |
| Spent so far | What has gone out since the 1st, against what was free |
| Safe to spend a day | What you can spend daily and still finish level |
| Pace for the month | Whether you are ahead of or behind an even spend |
| Projected month end | Where the month lands at the current pace (off by default) |
| Committed overspend | Named when a bill has gone past its budget |
| Account balances | Current Actual balance for every monitored bank-linked account (off by default) |
| Bank connections | Connections needing attention |
| Waiting for you | Transactions to review, and anything uncategorized |

A broken bank connection still raises the notification's priority whether or not that block is shown: which parts you want to read is a preference, a dead connection is not. Report bodies use short bold section labels and Markdown-compatible lists; ntfy renders those in its web app, while the same labels and hyphen bullets remain understandable as plain text in its phone apps.

On an unchanged morning, connection problems and work waiting for you are still shown. SimpleFIN defines `balance-date` as the timestamp attached to the balance value, so an advancing timestamp proves newer bank data arrived; an unchanged timestamp cannot prove whether the bank was polled and found the same value. Clerk therefore says only that SimpleFIN exposed no newer balance timestamp rather than claiming the connection is stale.

---

## How Clerk decides what to touch

Clerk is deliberately conservative:

- **It never overwrites a category you set.** If you categorize something between Clerk proposing and Clerk writing, your choice wins and Clerk records that it stood down.
- **It never creates a category on its own.** When a merchant fits nowhere, it says so and offers a suggestion you can accept in one click.
- **It never writes a rule without asking.**
- **It never auto-files a first-time merchant.** Model confidence is useful for ranking a proposal, not for granting permission to write it.
- **It never touches a transaction it is unsure about.** Below the confidence threshold, the proposal goes to the review queue instead of into your budget.
- **Everything it does is reversible from inside Actual**, because everything it touches carries the `#clerk` tag.

Set **Settings → Filing → For merchants Clerk already knows** to *Propose every category for review* if you would rather approve established merchants too. First-time merchants always require approval.

---

## Configuration

Everything is configurable in the UI. Any value set as an environment variable becomes authoritative and shows as read-only in Settings, so a container-managed deployment stays declarative.

### Actual Budget

| Variable | Default | Meaning |
| --- | --- | --- |
| `ACTUAL_URL` | `http://actual_server:5006` | Actual server URL |
| `ACTUAL_PASSWORD` | — | Server password (required) |
| `ACTUAL_BUDGET_ID` | — | Budget sync ID (required) |
| `ACTUAL_ENCRYPTION_PASSWORD` | — | Only for end-to-end encrypted budgets |
| `ACTUAL_VERIFY_SSL` | `true` | Disable only for a trusted self-signed server |

### SimpleFIN and the model

| Variable | Default | Meaning |
| --- | --- | --- |
| `CLERK_SIMPLEFIN_ACCESS_URL` | — | Access URL; prefer claiming a token in the UI |
| `CLERK_OPENAI_BASE_URL` | `http://host.docker.internal:11434/v1` | OpenAI-compatible endpoint |
| `CLERK_OPENAI_API_KEY` | — | Only if your server requires one |
| `CLERK_MODEL` | `qwen2.5:14b` | Model name |
| `CLERK_MODEL_REASONING` | *(server default)* | Reasoning effort: `off`, `low`, `medium`, or `high`; unsupported hints are withdrawn automatically |
| `CLERK_MODEL_CONTEXT_TOKENS` | `16384` | Context limit |
| `CLERK_MODEL_MAX_OUTPUT_TOKENS` | `2048` | Output limit |

### Filing

| Variable | Default | Meaning |
| --- | --- | --- |
| `CLERK_CATEGORIZATION_ENABLED` | `true` | File uncategorized transactions |
| `CLERK_AI_ENABLED` | `true` | Ask the model about unfamiliar merchants |
| `CLERK_APPLY_MODE` | `automatic` | `automatic` or `review` |
| `CLERK_MEMORY_MIN_CONFIDENCE` | `0.75` | Confidence needed from your own history |
| `CLERK_MEMORY_MIN_OBSERVATIONS` | `2` | Sightings needed from your own history |
| `CLERK_CATEGORIZE_LOOKBACK_DAYS` | `45` | How far back to file |
| `CLERK_HISTORY_LOOKBACK_DAYS` | `730` | History read for memory and recurring detection |
| `CLERK_AI_EXAMPLE_COUNT` | `8` | Your own transactions shown to the model |
| `CLERK_CATEGORY_CANDIDATE_LIMIT` | `90` | Categories offered to the model |
| `CLERK_ALLOW_NEW_CATEGORIES` | `false` | Surface suggestions for missing categories |
| `CLERK_RULE_PROMOTION_ENABLED` | `true` | Offer to write native Actual rules |
| `CLERK_RULE_PROMOTE_AFTER` | `3` | Consistent decisions before offering |

### Tags

| Variable | Default | Meaning |
| --- | --- | --- |
| `CLERK_TAGGING_ENABLED` | `true` | Master switch for all tag writing |
| `CLERK_TAG` | `clerk` | Provenance tag, written without the `#` |
| `CLERK_TAG_PROVENANCE` | `true` | Mark Clerk's own work |
| `CLERK_TAG_CADENCE` | `true` | `#subscription`, `#recurring`, `#annual` |
| `CLERK_TAG_ANOMALIES` | `true` | `#unusual`, `#refund` |

### Budget report

| Variable | Default | Meaning |
| --- | --- | --- |
| `CLERK_COMMITTED_GROUPS` | — | Comma-separated groups holding your bills; empty means "anything budgeted" |
| `CLERK_MONTHLY_INCOME` | `0` | Manual override; `0` uses income budgeted in Actual, then received, then a trailing average |
| `CLERK_INCOME_LOOKBACK_MONTHS` | `3` | Months in the income average |
| `CLERK_CURRENCY` | `USD` | Display currency |

### Schedule, health, and notifications

| Variable | Default | Meaning |
| --- | --- | --- |
| `CLERK_SYNC_ENABLED` | `true` | Sync on a schedule |
| `CLERK_SYNC_INTERVAL_MINUTES` | `60` | How often |
| `CLERK_BANK_SYNC_ENABLED` | `true` | Ask Actual to run bank sync (see below) |
| `CLERK_HEALTH_INTERVAL_MINUTES` | `60` | How often connections are checked |
| `CLERK_BALANCE_STALE_HOURS` | `36` | When a bank balance counts as stale |
| `CLERK_BALANCE_TOLERANCE` | `1.0` | Balance difference to ignore |
| `CLERK_TRANSACTION_STALE_DAYS` | `4` | Silence before an account looks stalled |
| `CLERK_HEALTH_ALERTS_ENABLED` | `true` | Notify on connection changes |
| `CLERK_DIGEST_ENABLED` | `true` | Send the morning digest |
| `CLERK_DIGEST_TIME` | `07:30` | Local delivery time |
| `CLERK_DIGEST_TITLE` | `The Morning Report` | The notification header |
| `TZ` / `CLERK_TIMEZONE` | `UTC` | Time zone for the digest and the month boundary |
| `CLERK_NOTIFICATIONS_ENABLED` | `false` | Enable ntfy delivery |
| `CLERK_NTFY_URL` | `https://ntfy.sh` | ntfy server |
| `CLERK_NTFY_TOPIC` | — | Topic (required when notifications are on) |
| `CLERK_NTFY_TOKEN` | — | For a protected topic |

`TZ` seeds the time zone until you pick one in **Settings → Sync & digest**, after which your choice stands and `TZ` stops reclaiming it. `CLERK_TIMEZONE` overrides both and locks the field.

If the digest arrives at the wrong hour, or not at all, **Settings → Limits & reliability → Run diagnostics** prints the whole schedule: both clocks, the four conditions the scheduler checks, where the time zone came from, and every digest Clerk has claimed — with the ntfy topic and message id it was accepted onto, which is what separates "never sent" from "sent somewhere you are not listening".

To watch the scheduled path fire on demand, set the digest time a few minutes ahead and save. One delivery is reserved per date *and* time, so moving the time asks for a fresh one rather than waiting for tomorrow; leaving it alone still gives exactly one a day. **Send report now** is a rehearsal that skips the clock entirely and never spends a scheduled delivery.

### Reliability

| Variable | Default | Meaning |
| --- | --- | --- |
| `CLERK_REQUEST_TIMEOUT_SECONDS` | `180` | Outbound request timeout |
| `CLERK_MODEL_MAX_RETRIES` | `3` | Retries per model request |
| `CLERK_JOB_MAX_ATTEMPTS` | `3` | Attempts per run |
| `CLERK_LOG_LEVEL` | `INFO` | Container log detail |
| `CLERK_DATA_DIR` | `/app/data` | Clerk's database and budget cache |
| `CLERK_HOST` / `CLERK_PORT` | `0.0.0.0` / `8080` | Bind address |

---

## Clerk is what keeps Actual synced

Actual does not sync with your bank on its own. Its server has no scheduler — a bank sync only happens when a client asks for one, which is why your balances appear to refresh only when you open Actual in a browser. There is no setting in Actual to change that.

Clerk is that client. Every sync run calls Actual's own bank sync — the same operation the browser triggers — then re-reads the budget and files whatever arrived. With `CLERK_SYNC_ENABLED` on (the default) that happens every `CLERK_SYNC_INTERVAL_MINUTES`, hourly out of the box, whether or not any browser is open.

The connection detail's **Sync bank and recheck** action runs that native bank
sync first and then scores the refreshed balances. A plain connection check is
still available on the Connections and Activity pages when no bank import is
needed.

Hourly is already generous: SimpleFIN itself refreshes each linked account roughly once a day, and the time of day varies per bank. Polling more often does not produce fresher data, it just asks the same question more times. If you want Actual left alone — because something else already drives bank sync — set `CLERK_BANK_SYNC_ENABLED=false` and Clerk will only read.

## Deployment notes

- **Persistent storage:** `/app/data` holds Clerk's SQLite database and the official API's private cached copy of the budget. The cache makes restarts fast; the database holds jobs, decisions, learned merchants, health history, and settings. Clerk takes an exclusive lock on that API cache so two replicas cannot write it concurrently.
- **Health check:** the image reports healthy once `/api/health` answers.
- **Network:** Clerk needs to reach your Actual server, your model server, and (optionally) SimpleFIN and ntfy. `host.docker.internal` is mapped for a model running on the host.
- **Security:** Clerk has no authentication of its own. Put it behind whatever already protects your Actual instance, and do not expose it to the internet.

## Development

```bash
uv sync --extra dev
npm ci
uv run --extra dev pytest
uv run --extra dev ruff check src tests
npm test
uv run actual-clerk          # http://localhost:8080
```

Node 20 or newer is required for a source checkout; the container includes
Node 22. `@actual-app/api` is pinned exactly and its npm lockfile is committed.
The Python suite and worker contract suite run without an Actual server, a
SimpleFIN account, or a model. A container build additionally verifies that the
official API and its native SQLite dependency load in the shipped runtime.

## Licence

MIT. See [LICENSE](LICENSE).
