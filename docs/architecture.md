# Actual Clerk architecture

Actual Clerk is a sidecar. Actual Budget remains the system of record: Clerk reads the budget, writes categories, tags, and rules back through Actual's own sync protocol, and keeps only the state it needs to be durable, reviewable, and safe to retry.

## Reference review

The reference applications establish useful patterns and also show what to avoid. `actual-ai` demonstrates the Actual API surface, rule-aware processing, and a notes marker for AI-touched transactions; it also carries a broad hosted provider matrix, editable prompt templates, per-transaction model calls, and no persistence between runs. `paperless-clerk` contributes the shape Clerk takes: one local OpenAI-compatible endpoint, SQLite-backed durable jobs with leases, a conservative apply policy, an explicit review surface, and a build-free web client.

Clerk keeps the useful parts of both and inherits neither data model.

## Talking to Actual

Actual has no REST API. Its official client downloads the budget file, works against a local SQLite copy, and syncs CRDT messages back to the server. Clerk uses [`actualpy`](https://github.com/bvanelli/actualpy), a Python implementation of that protocol, which keeps the whole application in one language and one process.

That local file is not safe for concurrent writers, so **every read and every write goes through one gateway, on one thread, in order**:

```text
Browser UI
    |
FastAPI JSON API ---- SQLite (jobs, decisions, memory, health, digests, settings)
    |
Single durable worker
    +---- ActualGateway  (one thread, one open budget session)
    |         +---- actualpy -> Actual server -> budget file
    +---- SimpleFIN client   (direct, read-only, balances and errors)
    +---- Model client       (OpenAI-compatible chat completions)
    +---- ntfy client        (morning digest and connection alerts)
```

There is no Redis and no external task service. SQLite runs in WAL mode with short transactions, a partial unique index for the active job of each kind, and leases so a crashed worker's job is reclaimed on restart.

Everything the gateway returns is a plain dictionary. Nothing outside `clients/actual.py` touches a SQLAlchemy object, which is what lets the budget report, the health checks, and the categorizer be tested without a server.

## Job kinds

| Kind | Trigger | What it does |
| --- | --- | --- |
| `sync` | schedule, manual | Runs Actual's bank sync, re-reads the budget, rebuilds the overview, queues `categorize` |
| `categorize` | after `sync`, manual | Runs the filing cascade and writes results back |
| `health` | schedule, manual | Reads SimpleFIN directly, scores each account, alerts on transitions |
| `digest` | daily at the configured local time | Builds and sends the morning report |

A failed bank sync does not abort a `sync` run: Clerk still has a budget to report on, and the health check is what explains the failure.

Actual's server runs no scheduler of its own, so a bank sync only happens when a client asks for one. Clerk's `sync` job is that client, which is what keeps a headless Actual current without a browser open. A `categorize` job carrying `{"full": true}` reaches back over the whole retained history instead of the recent window, for the first run against an existing budget.

## The filing cascade

Clerk answers the cheapest reliable question first.

1. **Memory.** Merchant descriptors are normalized to a stable key (`SQ *BLUE BOTTLE 4471`, `TST* Blue Bottle Coffee`, and `BLUE BOTTLE COFFEE #4471 OAKLAND CA` all become `blue bottle coffee`). Evidence is built from the user's own categorized history, recency-weighted with a nine-month half-life, plus Clerk's own applied decisions and any explicit corrections, which count triple. Confidence is the weighted share of the dominant category multiplied by a saturation curve over the raw sighting count — the two are tracked separately so old evidence loses influence without ceasing to be evidence.
2. **Model.** A merchant with no usable history goes to the local model **once per merchant, not once per transaction**. The model receives the budget's existing categories as a numbered list and answers with a number, which removes every failure mode that comes from asking a small local model to reproduce a UUID. It also receives the user's own comparable filings, because a category tree is a personal document: `Costco` belongs under Groceries in one budget and Household in another.
3. **Review.** Anything neither step settles above its confidence threshold is queued for a human rather than guessed at. The proposal is kept so the review screen can offer it in one click.

Actual's rules are not re-implemented. Actual applies them during import, so a transaction that reaches Clerk uncategorized is one no rule claimed.

### Promotion back into Actual

The cascade runs in reverse too. When Clerk has filed the same merchant the same way `rule_promote_after` times, it offers to write a native Actual rule. Once that rule exists, Actual applies it on import and the answer costs nothing — no memory lookup, no model call, no Clerk involvement at all.

Promotion requires a match value that appears **verbatim** on the statement. A key that only exists after normalization — an alias, a joined hyphen — is never promoted, because a rule that can never fire is worse than no rule. Very short matches are refused as well: a rule containing `UBER` would swallow rides and meal delivery alike.

## Tagging

Categories answer *which budget line*; tags answer *what kind of spending this was*. Actual stores tags inline in a transaction's notes as `#tag`, with colour and description in its own tag table.

Almost every tag Clerk writes is derived from history rather than from a model:

- `#subscription` — a merchant billing on a schedule for a steady amount. A subscription that raised its price is still a subscription, so the judgement is made on the current price level rather than on the whole history.
- `#recurring` — on a schedule, varying amount (utilities).
- `#annual` — renews roughly yearly.
- `#unusual` — far above this merchant's typical charge, with enough history to say so.
- `#refund` — money coming back rather than going out.
- `#clerk` — provenance, so everything Clerk touched is findable and reversible from inside Actual.

Tag writing is idempotent: notes are re-derived on the budget thread from whatever the transaction holds at write time, so a note the user edited between the proposal and the write is extended rather than replaced.

## Connection health

A SimpleFIN connection does not announce that it has broken. It stops returning fresh data, and Actual keeps showing the last balance it saw as if nothing were wrong. Clerk reads SimpleFIN directly and compares three independent signals per account:

- **What SimpleFIN says now** — `errlist` (v2) or `errors` (v1), matched to the account or to its whole connection.
- **How old that answer is** — a `balance-date` older than the threshold means the bank stopped refreshing, even though nothing reported an error.
- **What Actual holds** — a balance that disagrees with the bank's means transactions are missing on one side; a long silence means nothing has arrived.

Balances are compared like with like. Actual's headline balance counts every transaction, cleared or not; a bank reports only what it has posted. Comparing those two directly reports a mismatch every time something is waiting to clear, so Clerk sums the cleared side separately in the same query and compares that against the bank, reporting the uncleared remainder as context rather than as a fault. The budget deliberately keeps using the full balance: money committed is committed whether or not the bank has caught up.

Monitoring is per account and opt-out, stored in Clerk rather than written into the budget. An unmonitored account is still measured and still displayed — its real reading is kept as `underlying_status` — but reports as `muted`, which is outside the alerting statuses, excluded from the degraded count and the digest, and exempt from the staleness check. A legacy account that sees one transaction a year is not a broken connection.

Whether an account raises the alarm is decided once, on the server: every account carries an `alerting` flag derived from the same `ALERTING_STATUSES` the summary and the digest use. The dashboard banner, the sidebar badge, and the morning message therefore cannot disagree about what counts as a problem. `no_transactions` is deliberately outside that set — an account that has simply gone quiet gets the softer freshness nudge, which names the account and its silence in days rather than colouring the dashboard red.

Each account gets the most serious of its signals as a status, and keeps all of them for the detail view. Alerts fire on **transitions**, not on state: one message when a connection breaks, one when it recovers, and nothing in between. Accounts that were never linked to bank sync are never treated as broken.

## The budget report

```
free money = expected income - committed spending
remaining  = free money - discretionary spending - committed overspend
```

Committed spending is, by default, every non-income category carrying a budget this month — the shape [the setup guides](budget-setup.md) describe. Naming category groups explicitly overrides that, which is what a budget that assigns an amount to every category needs.

Actual has two budgeting methods, storing budgeted amounts in `zero_budgets` (envelope) or `reflect_budgets` (tracking) according to its own `budgetType` preference. Clerk reads whichever is active, so nothing breaks either way — but the tracking budget is the better fit and [the setup guides](budget-setup.md) recommend it, because its Projected Savings is the same arithmetic Clerk performs and its per-category *Copy until year end* removes the monthly re-budgeting chore. Because the two types occupy separate tables, switching between them is a preference flip rather than a migration.

Expected income prefers, in order: income budgeted in Actual for the month, a configured monthly figure, income actually received this month, and a trailing average over whole prior months. Budgeted income leads because Actual's tracking budget asks for it explicitly and builds its own Projected Savings from it — reading the same figure makes Clerk's free money equal the number Actual already shows, with nothing to configure twice. Envelope budgets carry no such row and fall through to the later options. The current month is excluded from the average on purpose, because half a month of pay would drag it down. When no income can be found at all, the report says so rather than dividing by a number that does not exist.

Transfers, off-budget accounts, and starting balances never count. Refunds are subtracted. Uncategorized spending counts in full and is reported separately so its provisional share is visible.

Overspending is measured against a committed category's accrued balance, not against one month's budget in isolation. A bill budgeted a twelfth at a time takes money out of free money every month, so charging the full invoice against the single month it lands in would bill the same money twice. Unspent budget therefore carries forward to its own category over a rolling twelve months -- one annual cycle. Overspending does not carry the other way: a month that ran over was already charged to free money then, and never becomes a debt the following month has to clear as well. The same mechanism absorbs the swings in a variable bill, which is why the setup guides recommend budgeting an average rather than a worst case.

## Failure boundaries

- HTTP and model retries are bounded and classify retryable status codes; whole jobs have persisted attempt counts and exponential backoff.
- Stale worker leases are reclaimed on startup and on every claim.
- One active job per kind: a second sync request while a sync is running is a duplicate, not a queue.
- A category the user set by hand between proposal and write always wins. Clerk's write re-reads the live transaction and skips it rather than overwriting; only an explicit human instruction from the review screen overwrites.
- Resolving a review claims the row before writing, so a double click cannot produce a double write. A failed write hands the claim back.
- Rule creation claims its suggestion the same way; a failure leaves the suggestion open.
- The daily digest is claimed once per local date, and the claim is released if delivery fails, so neither a retry nor tomorrow is blocked.
- A failed bank sync, a SimpleFIN outage, and a model outage each degrade one capability rather than failing the run. Model failures queue for review.
- Only decisions Actual actually accepted are learned from.
- The dashboard reads a persisted overview snapshot, so it keeps working while Actual is briefly unreachable and reports the snapshot's age.
- Secrets are never returned by the settings API; SimpleFIN credentials are lifted out of the access URL before any request so they cannot reach a log line or a redirect target.

## UI information architecture

Five focused views: overview, review, connections, recurring, and activity, plus settings. The overview leads with the budget hero card and surfaces anything degraded above it. Review combines transactions awaiting a decision with rules worth promoting. Connections shows per-account health and the full transition history. Recurring is the working list for the budget setup. Activity holds every run and every filing decision, including the ones withheld and why.
