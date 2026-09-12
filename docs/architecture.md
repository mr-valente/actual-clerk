# Actual Clerk architecture

Actual Clerk is a sidecar. Actual Budget remains the system of record: Clerk reads the budget, writes categories and tags back through Actual's own sync protocol, and keeps only the state it needs to be durable, reviewable, and safe to retry. What a merchant *means* is Clerk's to know: the simple payee-to-category rules live in Clerk (see [Intelligence](intelligence/plan.md)), and Actual keeps only the rules that do something other than pick a category.

## Reference review

The reference applications establish useful patterns and also show what to avoid. `actual-ai` demonstrates the Actual API surface, rule-aware processing, and a notes marker for AI-touched transactions; it also carries a broad hosted provider matrix, editable prompt templates, per-transaction model calls, and no persistence between runs. `paperless-clerk` contributes the shape Clerk takes: one local OpenAI-compatible endpoint, SQLite-backed durable jobs with leases, a conservative apply policy, an explicit review surface, and a build-free web client.

Clerk keeps the useful parts of both and inherits neither data model.

## Talking to Actual

Actual has no REST API. Its supported [`@actual-app/api`](https://actualbudget.org/docs/api/) client downloads the budget file, works against a local SQLite copy, and syncs CRDT messages back to the server. Clerk uses that official package directly in one long-lived Node worker. The Python application sends only high-level requests over a private, line-delimited JSON protocol; the worker's stdout is framed for replies and all library logging goes to stderr.

That local file is not safe for concurrent writers, so **every read and every write goes through one gateway and one worker, in order**. A process-lifetime file lock also prevents two Clerk replicas sharing `/app/data` from opening the API cache:

```text
Browser UI
    |
FastAPI JSON API ---- SQLite (jobs, decisions, rules, proposals, memory, health, digests, settings)
    |
Single durable job worker
    +---- ActualGateway  (async serializer and worker supervisor)
    |         +---- @actual-app/api worker (one open budget session)
    |                   +---- Actual server -> private budget cache
    +---- SimpleFIN client   (direct, read-only, balances and errors)
    +---- Model client       (OpenAI-compatible chat completions)
    +---- ntfy client        (morning digest and connection alerts)
    |
Phone companion app --- POST /api/anticipated/device/* (card notifications in)
```

The optional `model_reasoning` setting asks a hybrid local model to spend less
effort on Clerk's bounded categorization questions. It is sent as
`reasoning_effort`, or as the `enable_thinking` chat-template argument for
`off`; if the server rejects the hint, Clerk withdraws it for that client and
continues with the existing request contract.

There is no Redis and no external task service. SQLite runs in WAL mode with short transactions, a partial unique index for the active job of each kind, and leases so a crashed worker's job is reclaimed on restart.

The API package and server version are exposed in gateway status and diagnostics. Connection-setting changes restart the worker into a cache keyed by server URL and sync ID. `ACTUAL_VERIFY_SSL=false` is scoped to the child process rather than weakening TLS for Clerk's other clients.

Besides the documented methods, the object `init()` returns carries `send`,
which reaches Actual's internal server handlers. Clerk uses a small, tested set
of them to manage bank links itself: `account-unlink`,
`simplefin-accounts-link`, `simplefin-status`, `simplefin-accounts`,
`accounts-get`, and `secret-set` (for the SimpleFIN token only). The exact API
pin is what makes this safe; the worker contract tests name every handler and
argument shape. See [the Plaid migration notes](plaid-migration/README.md).

Everything the gateway returns is a plain dictionary. Nothing outside
`clients/actual.py` knows about Node or Actual's query objects, which is what
lets the budget report, health checks, and categorizer be tested without a
server. Transaction writes are prevalidated, grouped with
`batchBudgetUpdates`, and explicitly synced before the worker reports success.

## Job kinds

| Kind | Trigger | What it does |
| --- | --- | --- |
| `sync` | schedule, manual | Delivers Plaid connections (refresh, cursor stream, adoption, import), runs Actual's bank sync, re-reads the budget, rebuilds the overview, queues `categorize` and `health` |
| `categorize` | after `sync`, manual | Runs the filing cascade and writes results back |
| `health` | schedule, manual | Reads SimpleFIN directly, scores each account, alerts on transitions |
| `digest` | daily at the configured local time | Builds and sends the morning report |

A failed bank sync does not abort a `sync` run: Clerk still has a budget to report on, and the health check is what explains the failure.

Actual's server runs no scheduler of its own, so a bank sync only happens when a client asks for one. Clerk's `sync` job is that client, which is what keeps a headless Actual current without a browser open. A `categorize` job carrying `{"full": true}` reaches back over the whole retained history instead of the recent window, for the first run against an existing budget. A job carrying `{"reviews": true}` targets only the open review queue, including items older than the recent window; scheduled categorization excludes those stable exceptions. Every sync does reconcile those exceptions against their exact current transaction IDs: a review already categorized, converted to a transfer, deleted, or otherwise made ineligible in Actual closes as `resolved_external` without teaching Clerk merchant memory from a decision it did not make.

## The filing cascade

Clerk answers the cheapest reliable question first. Rules and evidence are
read through one resolver (`domain/intelligence.py`) that the filing cascade
and anticipated charges share; the model is only ever consulted by the
cascade, after the resolver has nothing.

1. **Rules.** A rule is `merchant key -> category`, declared by a person: by
   hand on the Intelligence page, by *Always* on a review, by *Make it a
   rule* on a decision, or by accepting a proposal. Its confidence is 1.0 by
   definition. It applies without thresholds and regardless of `apply_mode`,
   which governs what Clerk may do with *learned* evidence. A rule may be
   scoped to one account, and may match its merchant's store variants
   (`family`) rather than the exact key. A rule whose category no longer
   exists in Actual is skipped rather than repaired by guesswork.
2. **Memory.** Merchant descriptors are normalized to a stable key (`SQ *BLUE BOTTLE 4471`, `TST* Blue Bottle Coffee`, and `BLUE BOTTLE COFFEE #4471 OAKLAND CA` all become `blue bottle coffee`). Evidence is built from the user's own categorized history, recency-weighted with a nine-month half-life, plus Clerk's own applied decisions and any explicit corrections, which count triple. Confidence is the weighted share of the dominant category multiplied by a saturation curve over the raw sighting count — the two are tracked separately so old evidence loses influence without ceasing to be evidence. A single consistent exact prior filing is proposed without a model call, but stays review-only until it satisfies the configured observation and confidence thresholds; related-key matching never uses this provisional path.
3. **Model.** A merchant with no usable history goes to the local model **once per merchant, not once per transaction**. The model receives the budget's existing categories as a numbered list and answers with a number, which removes every failure mode that comes from asking a small local model to reproduce a UUID. It also receives the user's own comparable filings, because a category tree is a personal document: `Costco` belongs under Groceries in one budget and Household in another. Even a high-confidence answer remains a proposal: model confidence never authorizes a first-time merchant write.
4. **Review.** Every model proposal and anything else not settled from a rule or reliable merchant history is queued for a human rather than guessed at. Approval records memory, allowing later transactions from that merchant to use the automatic memory path when its evidence meets the configured thresholds.

Alias resolution runs before all four steps: a key the alias table maps to
another (a phone notification's "Valve" for the bank's "Steam") is looked up
under the canonical key first, then under its own. Aliases come from three
places: taught by hand, learned when an anticipated charge settled against a
row with a different name, and read from the payee catalogue in Actual on
every filing run, where a bank descriptor that the user settled on a
differently named payee is an alias the user curated without calling it
one. Only unambiguous pairs are read, and the table never chains: an alias
points at a merchant, never at another alias.

### Proposals

The cascade runs in reverse too. When Clerk has filed the same merchant the
same way `rule_promote_after` times by memory or approval, it records a
*proposal* to make that a rule, shown on the Intelligence page with its
evidence. Accepting it declares the rule; declining it closes the question
for that merchant. Clerk never declares a rule on its own, and it no longer
writes rules into Actual: the merchant key is what a rule matches, so a key
that only exists after normalization is as good as any other.

Transactions a rule filed are counted against the rule rather than learned
from: the rule is already the user's word, and feeding it back into memory
would only make the evidence agree with itself.

### Learning from Actual

Every filing run compares each decision Clerk applied with the transaction
as Actual holds it now. The same category means the decision stands. A
different one, set by hand, is a *correction*: recorded in memory with
correction weight under the new category, so the evidence follows what the
user actually did without the user ever opening Clerk. A correction to a
decision a rule made is also a *dispute* against that rule; after
`memory_dispute_threshold` of them Clerk records a proposal to change the
rule (or to retire it, when the category was cleared rather than moved). A
transaction that is gone or became a transfer teaches nothing. Each
decision is looked at until a correction settles it, so a category changed
weeks later is still noticed.

A rule whose category has left the budget files nothing and becomes a
*repair* proposal, which asks for the replacement. Actual used to rewrite
its own rules when a category was deleted; that choice is now the user's to
make on the Intelligence page.

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

The provider is a property of the account, not of Clerk. An account Actual links itself carries Actual's sync source (`simpleFin`); an account whose feed Clerk delivers (Plaid) is overlaid from Clerk's own `bank_links` table onto every snapshot, and health only compares an account with readings and errors from its own provider.

Monitoring is per account and opt-out, stored in Clerk rather than written into the budget. An unmonitored account is still measured and still displayed — its real reading is kept as `underlying_status` — but reports as `muted`, which is outside the alerting statuses, excluded from the degraded count and the digest, and exempt from the staleness check. A legacy account that sees one transaction a year is not a broken connection.

Whether an account raises the alarm is decided once, on the server: every account carries an `alerting` flag derived from the same `ALERTING_STATUSES` the summary and the digest use. The dashboard banner, the sidebar badge, and the morning message therefore cannot disagree about what counts as a problem. `no_transactions` is deliberately outside that set — an account that has simply gone quiet gets the softer freshness nudge, which names the account and its silence in days rather than colouring the dashboard red.

Each account gets the most serious of its signals as a status, and keeps all of them for the detail view. Alerts fire on **transitions**, not on state: one message when a connection breaks, one when it recovers, and nothing in between. Accounts that were never linked to bank sync are never treated as broken.

## The budget report

```
free money = expected income - committed spending
available  = free money + refunds reversing a month already reported
remaining  = available - discretionary spending - committed overspend
```

Committed spending is, by default, every non-income category carrying a budget this month — the shape [the setup guides](budget-setup.md) describe. Naming category groups explicitly overrides that, which is what a budget that assigns an amount to every category needs.

Actual has two budgeting methods, storing budgeted amounts in `zero_budgets` (envelope) or `reflect_budgets` (tracking) according to its own `budgetType` preference. Clerk reads whichever is active, so nothing breaks either way — but the tracking budget is the better fit and [the setup guides](budget-setup.md) recommend it, because its Projected Savings is the same arithmetic Clerk performs and its per-category *Copy until year end* removes the monthly re-budgeting chore. Because the two types occupy separate tables, switching between them is a preference flip rather than a migration.

Expected income prefers, in order: income budgeted in Actual for the month, a configured monthly figure, income actually received this month, and a trailing average over whole prior months. Budgeted income leads because Actual's tracking budget asks for it explicitly and builds its own Projected Savings from it — reading the same figure makes Clerk's free money equal the number Actual already shows, with nothing to configure twice. Envelope budgets carry no such row and fall through to the later options. The current month is excluded from the average on purpose, because half a month of pay would drag it down. When no income can be found at all, the report says so rather than dividing by a number that does not exist.

Transfers, off-budget accounts, and starting balances never count. Uncategorized spending counts in full and is reported separately so its provisional share is visible.

A refund belongs to the month that was charged for the purchase, so it is netted against its own category's spending for the month before anything else happens to it. What it cannot cancel there reverses a charge an earlier report already counted. That report is a record and is not restated; the money is instead reported as returned and added to what this month has to spend. Free money stays expected income minus commitments, so it remains exactly the figure Actual shows as Projected Savings, and the returned money sits beside it as its own line. Netting per category rather than across the month is what stops a refund in one category from being read as underspending in another; keeping the overflow out of spending is what stops a negative spend from extrapolating into a projected month-end richer than the month began, and keeps the remaining share at or under 100%. A refund on a committed category keeps the simpler rule: it draws on that category's own budget rather than on free money.

Each delivered digest stores the budget-state fields that actually move free money (`free_cents`, `returned_cents`, `spent_cents`, and `remaining_cents`) plus the current per-account SimpleFIN `balance-date`. Calendar-derived pace and daily-safe figures are intentionally excluded from change detection. If the budget-state tuple matches the prior delivered report in the same month, the financial blocks collapse to a quiet message while connection and review warnings remain. An advancing `balance-date` proves that newer bank data arrived; a non-advancing value proves only that SimpleFIN exposed no newer balance timestamp, because the protocol describes when the balance became its current value rather than a universal last-polled timestamp.

Overspending is measured against a committed category's accrued balance, not against one month's budget in isolation. A bill budgeted a twelfth at a time takes money out of free money every month, so charging the full invoice against the single month it lands in would bill the same money twice. Unspent budget therefore carries forward to its own category over a rolling twelve months -- one annual cycle. Overspending does not carry the other way: a month that ran over was already charged to free money then, and never becomes a debt the following month has to clear as well. The same mechanism absorbs the swings in a variable bill, which is why the setup guides recommend budgeting an average rather than a worst case.

## Anticipated charges

A card app's notification announces a purchase before the bank feed carries
it, and some issuers never expose it as pending. The Android companion app
(`android/`, built in a container by `scripts/build-android.sh`) forwards
those notifications; Clerk reads the amount, direction, and merchant out of
the text (`domain/anticipated.py`, so the reading can improve without a new
app build) and stores one *anticipated charge* per notification in its own
SQLite. Each open charge is given a provisional category through the same
memory the filing cascade uses (history plus applied decisions, same
thresholds, no model), looked up through a learned merchant alias first; the
user can set a category or an alias by hand, and a category set by hand is a
rule. A
categorized charge is charged to its category as a posted row would be, so a
budgeted bill draws on its budget; an uncategorized one is discretionary.
Free money is untouched. Every path that rebuilds
the overview first reconciles open charges against the snapshot: same account,
same amount, dated within the match window, preferring the row whose merchant
key relates to the notification's, then the nearest date, one transaction per
charge. Matched charges leave the report, and one nothing settles within the
expiry period is retired as never posted. Nothing is written into Actual;
the bank feed remains the record. See
[anticipated charges](anticipated-charges.md).

## Failure boundaries

- HTTP and model retries are bounded and classify retryable status codes; whole jobs have persisted attempt counts and exponential backoff.
- Stale worker leases are reclaimed on startup and on every claim.
- One active job per kind: a second sync request while a sync is running is a duplicate, not a queue.
- A category the user set by hand between proposal and write always wins. Clerk's write re-reads the live transaction and skips it rather than overwriting; only an explicit human instruction from the review screen overwrites.
- Resolving a review claims the row before writing, so a double click cannot produce a double write. A failed write hands the claim back.
- Accepting a proposal claims it the same way; a proposal that cannot be acted on is handed back open.
- The digest is claimed once per local date *and scheduled time*, and the claim is released if delivery fails, so neither a retry nor the next one is blocked. Keying it to the date alone made the scheduled path untestable — a digest that failed to arrive could only be tried again the following day — while giving no extra protection, because the delivery time is what a person means by "once a day". Moving the time asks for a delivery at the new time; leaving it alone still yields exactly one. A row claimed before scheduled times were recorded still blocks the whole date, so an upgrade cannot re-send a digest that already went out.
- The morning report's header is a fixed, user-named string, carries ntfy's newspaper emoji tag, and keeps every figure in the body. A header that changes with the numbers cannot be recognised at a glance, and a body that reports everything is one nobody reads — so each block is individually switchable, including one opt-in list containing every monitored bank-linked account balance, while the alert priority is derived from the state itself and is not affected by what is displayed. The body uses bold, Markdown-compatible section labels and lists that render richly in ntfy's web app and remain understandable as plain text in phone clients.
- Every delivered digest stores ntfy's own acknowledgement — server, topic, and message id. `delivered` alone cannot distinguish a message that reached the topic someone is watching from one accepted onto a topic nobody is subscribed to, which is precisely the question asked when a digest is recorded as sent but never seen.
- The time zone is a real dependency, not an assumption about the base image: `tzdata` is a declared package requirement, because a slim image resolves no IANA name at all and would silently keep every schedule on UTC. Which zone is in force, and where it came from, is reported rather than inferred — a stored zone always exists, so it can never be read as evidence that someone chose it.
- A failed bank sync, a SimpleFIN outage, and a model outage each degrade one capability rather than failing the run. Model failures queue for review.
- Only decisions Actual actually accepted are learned from.
- The dashboard reads a persisted overview snapshot, so it keeps working while Actual is briefly unreachable and reports the snapshot's age.
- Secrets are never returned by the settings API; SimpleFIN credentials are lifted out of the access URL before any request so they cannot reach a log line or a redirect target.

## UI information architecture

Five focused views: overview, review, intelligence, connections, and activity, plus settings. The overview leads with the budget hero card and surfaces anything degraded above it. Review groups transactions awaiting a decision by merchant, so one choice settles every transaction from that merchant; *Always* settles it and declares a rule. Intelligence holds everything Clerk knows and everything it wants to know: the rules, the proposals waiting for an answer, and how much it has learned. Connections shows per-account health and the full transition history. Activity separates filing decisions from the longer run history with explicit tabs and opens on decisions by default.

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
