# Actual Clerk architecture

Actual Clerk is a sidecar. Actual Budget remains the system of record: Clerk reads the budget, writes categories, tags, and rules back through Actual's own sync protocol, and keeps only the state it needs to be durable, reviewable, and safe to retry.

## Reference review

The reference applications establish useful patterns and also show what to avoid. `actual-ai` demonstrates the Actual API surface, rule-aware processing, and a notes marker for AI-touched transactions; it also carries a broad hosted provider matrix, editable prompt templates, per-transaction model calls, and no persistence between runs. `paperless-clerk` contributes the shape Clerk takes: one local OpenAI-compatible endpoint, SQLite-backed durable jobs with leases, a conservative apply policy, an explicit review surface, and a build-free web client.

Clerk keeps the useful parts of both and inherits neither data model.

## Talking to Actual

Actual has no REST API. Its supported [`@actual-app/api`](https://actualbudget.org/docs/api/) client downloads the budget file, works against a local SQLite copy, and syncs CRDT messages back to the server. Clerk uses that official package directly in one long-lived Node worker. The Python application sends only high-level requests over a private, line-delimited JSON protocol; the worker's stdout is framed for replies and all library logging goes to stderr.

That local file is not safe for concurrent writers, so **every read and every write goes through one gateway and one worker, in order**. A process-lifetime file lock also prevents two Clerk replicas sharing `/app/data` from opening the API cache:

```text
Browser UI
    |
FastAPI JSON API ---- SQLite (jobs, decisions, memory, health, digests, settings)
    |
Single durable job worker
    +---- ActualGateway  (async serializer and worker supervisor)
    |         +---- @actual-app/api worker (one open budget session)
    |                   +---- Actual server -> private budget cache
    +---- SimpleFIN client   (direct, read-only, balances and errors)
    +---- Model client       (OpenAI-compatible chat completions)
    +---- ntfy client        (morning digest and connection alerts)
```

The optional `model_reasoning` setting asks a hybrid local model to spend less
effort on Clerk's bounded categorization questions. It is sent as
`reasoning_effort`, or as the `enable_thinking` chat-template argument for
`off`; if the server rejects the hint, Clerk withdraws it for that client and
continues with the existing request contract.

There is no Redis and no external task service. SQLite runs in WAL mode with short transactions, a partial unique index for the active job of each kind, and leases so a crashed worker's job is reclaimed on restart.

The API package and server version are exposed in gateway status and diagnostics. Connection-setting changes restart the worker into a cache keyed by server URL and sync ID. `ACTUAL_VERIFY_SSL=false` is scoped to the child process rather than weakening TLS for Clerk's other clients.

Everything the gateway returns is a plain dictionary. Nothing outside
`clients/actual.py` knows about Node or Actual's query objects, which is what
lets the budget report, health checks, and categorizer be tested without a
server. Transaction writes are prevalidated, grouped with
`batchBudgetUpdates`, and explicitly synced before the worker reports success.

## Job kinds

| Kind | Trigger | What it does |
| --- | --- | --- |
| `sync` | schedule, manual | Runs Actual's bank sync, re-reads the budget, rebuilds the overview, queues `categorize` and `health` |
| `categorize` | after `sync`, manual | Runs the filing cascade and writes results back |
| `health` | schedule, manual | Reads SimpleFIN directly, scores each account, alerts on transitions |
| `digest` | daily at the configured local time | Builds and sends the morning report |

A failed bank sync does not abort a `sync` run: Clerk still has a budget to report on, and the health check is what explains the failure.

Actual's server runs no scheduler of its own, so a bank sync only happens when a client asks for one. Clerk's `sync` job is that client, which is what keeps a headless Actual current without a browser open. A `categorize` job carrying `{"full": true}` reaches back over the whole retained history instead of the recent window, for the first run against an existing budget. A job carrying `{"reviews": true}` targets only the open review queue, including items older than the recent window; scheduled categorization excludes those stable exceptions. Every sync does reconcile those exceptions against their exact current transaction IDs: a review already categorized, converted to a transfer, deleted, or otherwise made ineligible in Actual closes as `resolved_external` without teaching Clerk merchant memory from a decision it did not make.

## The filing cascade

Clerk answers the cheapest reliable question first. A single consistent exact
prior filing is useful enough to propose without a model call, but it remains
review-only until it satisfies the configured observation and confidence
thresholds; related-key matching never uses this provisional path.

1. **Memory.** Merchant descriptors are normalized to a stable key (`SQ *BLUE BOTTLE 4471`, `TST* Blue Bottle Coffee`, and `BLUE BOTTLE COFFEE #4471 OAKLAND CA` all become `blue bottle coffee`). Evidence is built from the user's own categorized history, recency-weighted with a nine-month half-life, plus Clerk's own applied decisions and any explicit corrections, which count triple. Confidence is the weighted share of the dominant category multiplied by a saturation curve over the raw sighting count — the two are tracked separately so old evidence loses influence without ceasing to be evidence.
2. **Model.** A merchant with no usable history goes to the local model **once per merchant, not once per transaction**. The model receives the budget's existing categories as a numbered list and answers with a number, which removes every failure mode that comes from asking a small local model to reproduce a UUID. It also receives the user's own comparable filings, because a category tree is a personal document: `Costco` belongs under Groceries in one budget and Household in another. Even a high-confidence answer remains a proposal: model confidence never authorizes a first-time merchant write.
3. **Review.** Every model proposal and anything else not settled from reliable merchant history is queued for a human rather than guessed at. Approval records memory, allowing later transactions from that merchant to use the automatic memory path when its evidence meets the configured thresholds.

Actual's rules are not re-implemented. Actual applies them during import, so a transaction that reaches Clerk uncategorized is one no rule claimed.

### Promotion back into Actual

The cascade runs in reverse too. When Clerk has filed the same merchant the same way `rule_promote_after` times, it offers to write a native Actual rule. Once that rule exists, Actual applies it on import and the answer costs nothing — no memory lookup, no model call, no Clerk involvement at all.

Promotion requires a match value that appears **verbatim** in the payee name. A key that only exists after normalization — an alias, a joined hyphen — is never promoted, because a rule that can never fire is worse than no rule. Very short matches are refused as well: a rule containing `UBER` would swallow rides and meal delivery alike.

## Tagging

Categories answer *which budget line*; tags answer *what kind of spending this was*. Actual stores tags inline in a transaction's notes as `#tag`, with colour and description in its own tag table.

Almost every tag Clerk writes is derived from history rather than from a model:

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

An imported transfer rule creates a special provenance case. Actual immediately generates the linked row on the other account, and that counterpart can inherit the source row's cleared flag despite having no `financial_id` from its own bank. Clerk finds only this asymmetric shape — generated half without an import id, linked half with one — and looks for a small subset of recent candidates that exactly explains the gap. It holds that amount out only if the adjusted cleared balance agrees with SimpleFIN within tolerance; otherwise the ordinary cleared balance remains authoritative and the mismatch is still tested. Old unmatched transfer rows are ineligible, and when the destination import reconciles a generated row it gains a `financial_id` and falls out automatically.

A raw `drifted` result is not immediately public. SQLite retains it as a candidate while the account continues to expose its non-drift status; only a mismatch present in three successively newer SimpleFIN `balance-date` values is promoted into `account_health`, the transition ledger, dashboard alarm, digest, and ntfy alert. Re-reading or receiving an older timestamp leaves the count unchanged, and any intervening matching check deletes the candidate. This is deliberately specific to drift: an authentication error, missing account, or stale bank timestamp is actionable on its first observation.

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

Each delivered digest stores the budget-state fields that actually move free money (`free_cents`, `spent_cents`, and `remaining_cents`) plus the current per-account SimpleFIN `balance-date`. Calendar-derived pace and daily-safe figures are intentionally excluded from change detection. If the budget-state tuple matches the prior delivered report in the same month, the financial blocks collapse to a quiet message while connection and review warnings remain. An advancing `balance-date` proves that newer bank data arrived; a non-advancing value proves only that SimpleFIN exposed no newer balance timestamp, because the protocol describes when the balance became its current value rather than a universal last-polled timestamp.

Overspending is measured against a committed category's accrued balance, not against one month's budget in isolation. A bill budgeted a twelfth at a time takes money out of free money every month, so charging the full invoice against the single month it lands in would bill the same money twice. Unspent budget therefore carries forward to its own category over a rolling twelve months -- one annual cycle. Overspending does not carry the other way: a month that ran over was already charged to free money then, and never becomes a debt the following month has to clear as well. The same mechanism absorbs the swings in a variable bill, which is why the setup guides recommend budgeting an average rather than a worst case.

## Failure boundaries

- HTTP and model retries are bounded and classify retryable status codes; whole jobs have persisted attempt counts and exponential backoff.
- Stale worker leases are reclaimed on startup and on every claim.
- One active job per kind: a second sync request while a sync is running is a duplicate, not a queue.
- A category the user set by hand between proposal and write always wins. Clerk's write re-reads the live transaction and skips it rather than overwriting; only an explicit human instruction from the review screen overwrites.
- Resolving a review claims the row before writing, so a double click cannot produce a double write. A failed write hands the claim back.
- Rule creation claims its suggestion the same way; a failure leaves the suggestion open.
- The digest is claimed once per local date *and scheduled time*, and the claim is released if delivery fails, so neither a retry nor the next one is blocked. Keying it to the date alone made the scheduled path untestable — a digest that failed to arrive could only be tried again the following day — while giving no extra protection, because the delivery time is what a person means by "once a day". Moving the time asks for a delivery at the new time; leaving it alone still yields exactly one. A row claimed before scheduled times were recorded still blocks the whole date, so an upgrade cannot re-send a digest that already went out.
- The morning report's header is a fixed, user-named string and every figure lives in the body. A header that changes with the numbers cannot be recognised at a glance, and a body that reports everything is one nobody reads — so each block is individually switchable, while the alert priority is derived from the state itself and is not affected by what is displayed.
- Every delivered digest stores ntfy's own acknowledgement — server, topic, and message id. `delivered` alone cannot distinguish a message that reached the topic someone is watching from one accepted onto a topic nobody is subscribed to, which is precisely the question asked when a digest is recorded as sent but never seen.
- The time zone is a real dependency, not an assumption about the base image: `tzdata` is a declared package requirement, because a slim image resolves no IANA name at all and would silently keep every schedule on UTC. Which zone is in force, and where it came from, is reported rather than inferred — a stored zone always exists, so it can never be read as evidence that someone chose it.
- A failed bank sync, a SimpleFIN outage, and a model outage each degrade one capability rather than failing the run. Model failures queue for review.
- Only decisions Actual actually accepted are learned from.
- The dashboard reads a persisted overview snapshot, so it keeps working while Actual is briefly unreachable and reports the snapshot's age.
- Secrets are never returned by the settings API; SimpleFIN credentials are lifted out of the access URL before any request so they cannot reach a log line or a redirect target.

## UI information architecture

Four focused views: overview, review, connections, and activity, plus settings. The overview leads with the budget hero card and surfaces anything degraded above it. Review groups transactions awaiting a decision by merchant, so one choice settles every transaction from that merchant, and lists rules worth promoting. Connections shows per-account health and the full transition history. Activity separates filing decisions from the longer run history with explicit tabs and opens on decisions by default.

The build-free web client serves its HTML with revalidation and references its
JavaScript, CSS, and favicon with one SHA-256 fingerprint derived from every
asset's relative name and contents. A changed asset therefore gives the whole
bundle new URLs; assets carrying the current fingerprint are immutable, while
unversioned or incorrectly fingerprinted requests must revalidate. Private API
responses retain their stricter `no-store` policy.

## Reading Actual through its own semantics

Actual does not rewrite transactions when a category is deleted into a
replacement, or when two payees are merged. It records a redirect in
`category_mapping` / `payee_mapping`. Clerk queries transactions through
Actual's public AQL view, which resolves those mappings, filters tombstones, and
handles split transactions using Actual's own executor. Budget history comes
from `getBudgetMonth`, the same spreadsheet-backed calculation exposed by the
official API, rather than from a second reconstruction of raw budget rows.

The former Python integration had to reproduce these rules and still diverged
in edge cases. Its [category bug report](bug-renamed-categories.md) is retained
as the historical reason this boundary now belongs to Actual itself.
