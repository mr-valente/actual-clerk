const content = document.querySelector("#content");
const drawer = document.querySelector("#drawer");
const drawerContent = document.querySelector("#drawer-content");
const scrim = document.querySelector("#scrim");
const claimDialog = document.querySelector("#claim-dialog");
const claimForm = document.querySelector("#claim-form");
const categoryDialog = document.querySelector("#category-dialog");
const categoryForm = document.querySelector("#category-form");

const state = {
  route: "overview",
  data: null,
  reviews: [],
  // A category picked in Review but not yet applied, by merchant group. The
  // page is redrawn every poll, and a choice that lived only in the DOM was
  // quietly replaced by the original suggestion a few seconds after it was
  // made -- and then applied.
  reviewChoices: new Map(),
  reviewFingerprint: "",
  intelligence: null,
  intelligenceFingerprint: "",
  intelligenceTab: "",
  proposalFilter: "all",
  ruleFilter: "active",
  merchantFilter: "all",
  ruleFormOpen: false,
  aliasFormOpen: false,
  actualRules: null,
  actualRulesError: "",
  ruleSearch: "",
  merchantSearch: "",
  aliasSearch: "",
  accounts: null,
  decisions: [],
  jobs: [],
  settings: null,
  health: null,
  plaid: null,
  activityView: "all",
  activityFingerprint: "",
  decisionSearch: "",
  jobFilter: "all",
  openJobId: null,
  poll: null,
};

const pageMeta = {
  overview: ["Budget", "Overview"],
  review: ["Needs a decision", "Review"],
  intelligence: ["What Clerk knows", "Intelligence"],
  accounts: ["Bank sync", "Connections"],
  activity: ["History", "Activity"],
  settings: ["Configuration", "Settings"],
};

const appearanceStorageKey = "actual-clerk-appearance";
const darkModeQuery = window.matchMedia("(prefers-color-scheme: dark)");
const reducedMotionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");

function applyAppearance(settings = {}, persist = true) {
  const appearance = {
    theme: settings.appearance_theme || settings.theme || "system",
    density: settings.appearance_density || settings.density || "comfortable",
    motion: settings.appearance_motion || settings.motion || "system",
  };
  document.documentElement.dataset.theme = appearance.theme;
  document.documentElement.dataset.density = appearance.density;
  document.documentElement.dataset.motion = appearance.motion;
  const dark = appearance.theme === "dark" || (appearance.theme === "system" && darkModeQuery.matches);
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", dark ? "#080811" : "#102a43");
  if (persist) {
    try { localStorage.setItem(appearanceStorageKey, JSON.stringify(appearance)); }
    catch { /* appearance still applies for this page */ }
  }
}

try { applyAppearance(JSON.parse(localStorage.getItem(appearanceStorageKey) || "null") || {}, false); }
catch { applyAppearance({}, false); }

darkModeQuery.addEventListener("change", () => {
  if (document.documentElement.dataset.theme === "system") {
    applyAppearance({ theme: "system", density: document.documentElement.dataset.density, motion: document.documentElement.dataset.motion }, false);
  }
});

// ------------------------------------------------------------------ helpers

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

function titleCase(value = "") {
  return String(value).replaceAll("_", " ").replace(/\b\w/g, (char) => char.toUpperCase());
}

function currency() { return state.data?.currency || "USD"; }

function money(cents, { sign = false, compact = false } = {}) {
  const value = Number(cents || 0) / 100;
  const options = { style: "currency", currency: currency(), maximumFractionDigits: compact && Math.abs(value) >= 1000 ? 0 : 2, minimumFractionDigits: compact && Math.abs(value) >= 1000 ? 0 : 2 };
  let text;
  try { text = new Intl.NumberFormat(undefined, options).format(value); }
  catch { text = `${value.toFixed(2)} ${currency()}`; }
  return sign && value > 0 ? `+${text}` : text;
}

function percent(ratio) {
  const value = Number.isFinite(ratio) ? ratio : 0;
  return `${Math.round(value * 100)}%`;
}

function relativeTime(value) {
  if (!value) return "—";
  const seconds = Math.round((new Date(value).getTime() - Date.now()) / 1000);
  if (!Number.isFinite(seconds)) return "—";
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  const units = [["year", 31536000], ["month", 2592000], ["day", 86400], ["hour", 3600], ["minute", 60]];
  for (const [unit, size] of units) if (Math.abs(seconds) >= size) return formatter.format(Math.round(seconds / size), unit);
  return formatter.format(seconds, "second");
}

function fullTime(value) {
  return value ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value)) : "—";
}

function shortDate(value) {
  if (!value) return "—";
  const date = new Date(`${value}T00:00:00`);
  if (Number.isNaN(date.getTime())) return escapeHtml(value);
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(date);
}

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try { const body = await response.json(); detail = body.detail || body.error || detail; } catch { /* not JSON */ }
    throw new Error(detail);
  }
  if (response.status === 204) return null;
  return response.json();
}

function toast(title, message = "", type = "success") {
  const item = document.createElement("div");
  item.className = `toast ${type}`;
  item.innerHTML = `<strong>${escapeHtml(title)}</strong>${message ? `<p>${escapeHtml(message)}</p>` : ""}`;
  document.querySelector("#toasts").append(item);
  setTimeout(() => item.remove(), 5200);
}

function statusChip(status, label) {
  return `<span class="status-chip ${escapeHtml(status)}">${escapeHtml(label || titleCase(status))}</span>`;
}

function emptyState(icon, title, text) {
  return `<div class="empty-state"><div><span class="empty-icon">${icon}</span><h3>${escapeHtml(title)}</h3><p>${escapeHtml(text)}</p></div></div>`;
}

function overview() { return state.data?.overview || {}; }
function budget() { return overview().budget || {}; }

// Whether a connection is a problem is decided on the server, so the banner,
// the sidebar badge, and the digest can never disagree. The literal list is
// only a fallback for a snapshot written before that field existed.
const ALERTING_STATUSES = ["error", "missing", "stale", "drifted"];
function isDegraded(item) {
  return item.alerting === undefined ? ALERTING_STATUSES.includes(item.status) : Boolean(item.alerting);
}
// Each alerting status fails for its own reason and is fixed in its own place.
// Reauthorizing is the answer only when SimpleFIN has actually lost the bank;
// telling someone to reauthorize a link that is working sends them to check a
// connection that was never the problem.
const STATUS_REMEDIES = {
  simpleFin: {
    error: "SimpleFIN reported an error for this bank. Reauthorize it in your SimpleFIN Bridge, then run a check here.",
    missing: "SimpleFIN is no longer returning this account. Relink it in your SimpleFIN Bridge, then run a check here.",
    stale: "The bank has stopped sending SimpleFIN fresh data for this account. Nothing needs fixing in Actual, and it usually clears on its own once the bank refreshes.",
  },
  plaid: {
    error: "Plaid reports a problem with this bank connection. If the bank wants a fresh login, use Repair on the Plaid connection below; it keeps the same connection.",
    missing: "Plaid is no longer returning this account on its connection. Open the connection below to remap or remove the mapping.",
    stale: "Plaid has not received fresh data from the bank for this account. Nothing needs fixing in Actual; if it persists, check the connection below.",
  },
  drifted: "Actual and the bank disagree about posted money, so a transaction is missing on one side. Compare the account with the bank and reconcile it in Actual.",
};
function connectionRemedies(degraded) {
  const byKey = new Map();
  for (const item of degraded) {
    const key = `${item.sync_source || "simpleFin"}:${item.status}`;
    if (!byKey.has(key)) byKey.set(key, { status: item.status, provider: item.sync_source || "simpleFin", names: [] });
    byKey.get(key).names.push(item.account_name);
  }
  return [...byKey.values()].map(({ status, provider, names }) => {
    const remedy = STATUS_REMEDIES[status] || STATUS_REMEDIES[provider]?.[status] || STATUS_REMEDIES.simpleFin[status] || "Run a check here for the current detail.";
    return `<p><strong>${escapeHtml(names.join(", "))}</strong> — ${escapeHtml(remedy)}</p>`;
  }).join("");
}
// A manual account has nothing to watch and a muted one was opted out, so
// neither belongs in the "connected" ratio.
function isWatched(item) { return item.status !== "not_linked" && item.status !== "muted"; }
// How old the bank's own balance is. This is the single piece of evidence that
// says whether a feed is still alive -- a link can answer every request and
// still be handing back a balance from last week -- so it belongs on the row
// rather than only inside the detail view. Hours while that still reads
// naturally, days once it does not.
function balanceAge(item) {
  const hours = item.balance_age_hours;
  if (hours === null || hours === undefined) return "";
  if (hours < 1) return "under an hour old";
  // Switch on the rounded figure, or 47.6h prints "48h old" one refresh before
  // the same age starts printing "2.0 days old".
  const whole = Math.round(hours);
  if (whole < 48) return `${whole}h old`;
  return `${(hours / 24).toFixed(1)} days old`;
}
function isQuiet(item) { return item.status === "no_transactions"; }

// -------------------------------------------------------------- components

function budgetHero() {
  const report = budget();
  if (!report || !Object.keys(report).length) {
    return `<section class="hero"><div class="hero-top"><div class="hero-headline"><p class="eyebrow">Free money</p><div class="hero-amount"><strong>—</strong></div><p class="hero-sub">Clerk has not read the budget yet. Run a sync, or check the Actual connection in Settings.</p></div></div></section>`;
  }
  if (!report.configured) {
    return `<section class="hero"><div class="hero-top"><div class="hero-headline"><p class="eyebrow">Free money</p><div class="hero-amount"><strong>Set your income</strong></div><p class="hero-sub">Clerk works out free money as expected income minus everything you have budgeted. It cannot find any income yet. Budget your expected income against an income category in Actual -- the tracking budget asks for exactly this.</p></div><div class="hero-side"><a class="button primary" href="#settings-actual">Open settings</a></div></div></section>`;
  }

  const free = report.free_cents || 0;
  const returned = report.returned_cents || 0;
  // Free money plus what an earlier month handed back: the sum this month is
  // measured against, and the denominator of every share shown here.
  const available = report.available_cents ?? free;
  const spent = report.spent_cents || 0;
  const remaining = report.remaining_cents || 0;
  const spentRatio = available > 0 ? Math.min(1.4, spent / available) : 1;
  const paceRatio = report.days_in_month ? report.day_of_month / report.days_in_month : 0;
  const overspent = remaining < 0;
  const paceClass = report.on_track ? "pace-ahead" : "pace-behind";
  // "-49% left" reads as a quantity of something you still have. Past zero the
  // honest phrasing is how far past it you are.
  const share = Number(report.remaining_percent) || 0;
  const shareLabel = available <= 0 ? "" : share < 0
    ? `${percent(Math.abs(share))} over budget`
    : `${percent(share)} left`;
  const paceDelta = Math.abs(report.pace_delta_cents || 0);
  const paceTitle = `An even month would have spent ${money(report.pace_expected_cents)} by day `
    + `${report.day_of_month} of ${report.days_in_month}. You have spent ${money(spent)}.`;
  const incomeNote = {
    override: "from the monthly income you set in Clerk",
    budgeted: "from the income you budgeted in Actual",
    received: "from income received this month",
    average: "averaged over recent months",
    unknown: "no income found",
  }[report.income_basis] || "";

  return `<section class="hero">
    <div class="hero-top">
      <div class="hero-headline">
        <p class="eyebrow">Free money · ${escapeHtml(report.month)} · day ${report.day_of_month} of ${report.days_in_month}</p>
        <div class="hero-amount ${overspent ? "spent" : ""}">
          <strong>${money(remaining)}</strong>
          ${shareLabel ? `<span class="pct">${shareLabel}</span>` : ""}
        </div>
        <p class="hero-sub">${overspent
          ? `You are ${money(Math.abs(remaining))} past the free money for this month.`
          : `${money(spent)} of ${money(available)} spent since the 1st.`} Free money is ${money(report.expected_income_cents)} expected income (${escapeHtml(incomeNote)}) minus ${money(report.committed_cents)} already budgeted for bills.${returned ? ` A further ${money(returned)} came back this month, refunding a purchase from an earlier one.` : ""}${report.anticipated_cents ? ` ${money(report.anticipated_cents)} from ${report.anticipated_count} charge${report.anticipated_count === 1 ? "" : "s"} your phone has seen is counted before the bank posts it${report.anticipated_committed_cents ? `, ${money(report.anticipated_committed_cents)} of it against bills already budgeted` : ""}.` : ""}</p>
      </div>
      <div class="hero-side">
        <div class="hero-chip"><span>Safe to spend daily</span><strong>${money(report.daily_safe_to_spend_cents)}</strong></div>
        <div class="hero-chip ${paceClass}" title="${escapeHtml(paceTitle)}"><span>${report.on_track ? "Under an even pace" : "Over an even pace"}</span><strong>${money(paceDelta)}</strong></div>
      </div>
    </div>
    <div class="gauge" role="img" aria-label="${percent(report.spent_percent)} of free money spent">
      <i class="${overspent ? "over" : ""}" style="width:${Math.max(1, Math.min(100, spentRatio * 100))}%"></i>
      <b style="left:${Math.max(0, Math.min(100, paceRatio * 100))}%"></b>
    </div>
    <div class="gauge-legend">
      <span><i></i> Spent ${money(spent)}</span>
      <span><i class="marker"></i> Where an even month would be today</span>
      <span>${report.days_remaining} day(s) left</span>
      <span>Projected month end: ${money(report.projected_remaining_cents)}</span>
    </div>
    <div class="hero-foot">
      <div><span>Expected income</span><strong class="money">${money(report.expected_income_cents)}</strong></div>
      <div><span>Received so far</span><strong class="money">${money(report.income_received_cents)}</strong></div>
      <div><span>Committed (budgeted)</span><strong class="money">${money(report.committed_cents)}</strong></div>
      <div><span>Discretionary spent</span><strong class="money">${money(report.discretionary_spent_cents)}</strong></div>
      <div><span>Committed overspend</span><strong class="money ${report.committed_overspend_cents ? "negative" : ""}">${money(report.committed_overspend_cents)}</strong></div>
      ${report.committed_carried_cents ? `<div><span>Set aside from earlier months</span><strong class="money positive">${money(report.committed_carried_cents)}</strong></div>` : ""}
      ${returned ? `<div><span>Refunded from earlier months</span><strong class="money positive">${money(returned)}</strong></div>` : ""}
      ${report.anticipated_cents ? `<div><span>Anticipated, not yet posted</span><strong class="money">${money(report.anticipated_cents)} · ${report.anticipated_count}</strong></div>` : ""}
      <div><span>Uncategorized</span><strong class="money">${money(report.uncategorized_cents)}${report.uncategorized_count ? ` · ${report.uncategorized_count}` : ""}</strong></div>
    </div>
  </section>`;
}

function healthRow(item, { actions = true, toggle = false } = {}) {
  const age = balanceAge(item);
  const drift = item.drift_cents;
  const balanceComparison = item.transfer_adjusted
    ? "matches after inferred transfer"
    : (drift ? `${money(drift, { sign: true })} vs cleared` : "matches cleared");
  const linked = item.status !== "not_linked";
  const monitorControl = toggle && linked
    ? `<label class="toggle-control compact" title="${item.monitored === false ? "Monitoring is off" : "Monitoring is on"}" data-stop>
        <input type="checkbox" data-action="toggle-monitoring" data-id="${escapeHtml(item.account_id)}" data-name="${escapeHtml(item.account_name || "")}" ${item.monitored === false ? "" : "checked"} />
        <i aria-hidden="true"></i>
       </label>`
    : "";
  return `<article class="data-row health-row ${actions ? "clickable" : ""}" ${actions ? `data-action="account-detail" data-id="${escapeHtml(item.account_id)}"` : ""}>
    <span class="dot ${escapeHtml(item.status)}" title="${escapeHtml(item.status_label || item.status)}"></span>
    <div class="row-title"><strong>${escapeHtml(item.account_name || "Account")}</strong><small>${escapeHtml([item.institution, item.provider_label ? `via ${item.provider_label}` : ""].filter(Boolean).join(" · ") || "Manual account")}</small></div>
    <div class="row-meta">${escapeHtml((item.detail || "").slice(0, 90))}${age ? `<br /><span${item.remote_balance_date ? ` title="Bank balance dated ${escapeHtml(fullTime(item.remote_balance_date))}"` : ""}>Bank data ${escapeHtml(age)}</span>` : ""}</div>
    <div class="row-meta">${item.remote_balance_cents === null || item.remote_balance_cents === undefined
      ? "—"
      : `<strong>${money(item.remote_balance_cents)}</strong><br /><span>${balanceComparison}</span>`}</div>
    <div class="health-state">${item.managed_by_clerk && item.actual_sync_source ? `<span class="status-chip warning" title="Actual still links this account to ${escapeHtml(item.actual_sync_source)} while Clerk delivers it from ${escapeHtml(item.provider_label || "another provider")}. Finish the move so it is fed once.">Fed twice</span>` : ""}${statusChip(item.status, item.status_label)}${monitorControl}</div>
  </article>`;
}

function jobRow(job, { actions = true } = {}) {
  const retryable = ["failed", "cancelled"].includes(job.status);
  return `<article class="data-row job-row clickable" data-action="job-detail" data-id="${escapeHtml(job.id)}">
    <span class="mark">${{ sync: "⇄", categorize: "◇", health: "♡", digest: "✉" }[job.kind] || "•"}</span>
    <div class="row-title"><strong>${escapeHtml(titleCase(job.kind))}</strong><small>${escapeHtml(titleCase(job.trigger))} · ${relativeTime(job.created_at)}</small>${job.status === "running" ? `<div class="progress-track indeterminate" role="progressbar" aria-label="Running"><i></i></div>` : ""}</div>
    <div class="row-meta">${escapeHtml(jobSummary(job))}</div>
    <div>${statusChip(job.status)}</div>
    ${actions ? `<div class="row-actions">${retryable ? `<button class="button ghost small" data-action="retry-job" data-id="${escapeHtml(job.id)}">Retry</button>` : ""}${["queued", "retry_wait"].includes(job.status) ? `<button class="button ghost small" data-action="cancel-job" data-id="${escapeHtml(job.id)}">Cancel</button>` : ""}</div>` : "<div></div>"}
  </article>`;
}

function jobSummary(job) {
  if (job.status === "failed" || job.status === "retry_wait") return job.error_message || "Failed";
  const result = job.result || {};
  if (job.kind === "sync") return `${result.transactions ?? 0} transaction(s) read${result.imported ? `, ${result.imported} imported` : ""}${result.reviews_resolved ? ` · ${result.reviews_resolved} review(s) resolved` : ""}`;
  if (job.kind === "categorize") {
    const scope = result.review_retry ? "review retry · " : result.full_history ? "older history · " : "";
    const base = `${scope}${result.applied ?? 0} applied · ${result.needs_review ?? 0} to review · ${result.model_calls ?? 0} model call(s)${result.reviews_resolved ? ` · ${result.reviews_resolved} resolved in Actual` : ""}`;
    return result.model_abandoned ? `${base} · model unreachable` : base;
  }
  if (job.kind === "health") return `${result.linked ?? 0} linked · ${result.degraded ?? 0} degraded`;
  if (job.kind === "digest") return result.title || result.reason || "—";
  return titleCase(job.phase || "");
}

function decisionRow(item) {
  return `<article class="data-row decision-row clickable" data-action="review-detail" data-id="${escapeHtml(item.id)}">
    <span class="mark ${escapeHtml(item.source)}">${{ rule: "⚡", memory: "↺", model: "✦", unresolved: "?" }[item.source] || "•"}</span>
    <div class="row-title"><strong>${escapeHtml(item.payee_name || item.merchant_key || "Transaction")}</strong><small>${shortDate(item.transaction_date)} · <span class="money">${money(item.amount_cents)}</span> · ${escapeHtml(item.account_name)}</small></div>
    <div class="row-meta"><strong>${escapeHtml(item.category_name || item.proposed_category || "—")}</strong><br /><span>${escapeHtml(titleCase(item.source))} · ${percent(item.confidence)}</span></div>
    <div>${item.observed === "corrected" || item.observed === "cleared" ? statusChip("warning", "Changed in Actual") : statusChip(item.status)}</div>
    <div class="row-meta">${relativeTime(item.created_at)}</div>
  </article>`;
}

// ------------------------------------------------------------------ routing

function updateChrome() {
  const [eyebrow, title] = pageMeta[state.route] || pageMeta.overview;
  document.querySelector("#page-eyebrow").textContent = eyebrow;
  document.querySelector("#page-title").textContent = title;
  document.querySelectorAll("[data-route]").forEach((item) => item.classList.toggle("active", item.dataset.route === state.route));
  if (!state.data) return;
  const counts = state.data.counts || {};
  setBadge("nav-review-count", counts.needs_review || 0);
  setBadge("nav-intelligence-count", counts.proposals || 0);
  setBadge("nav-health-count", counts.degraded_accounts || 0);
  const pill = document.querySelector("#automation-pill");
  pill.classList.toggle("off", !state.data.sync_enabled);
  pill.querySelector("span").textContent = state.data.sync_enabled
    ? (state.data.apply_mode === "automatic" ? "Auto-filing known merchants" : "Proposing everything for review")
    : "Automatic sync off";
  const badge = document.querySelector("#connection-badge");
  const connected = state.data.gateway?.connected;
  badge.classList.toggle("off", !connected);
  badge.querySelector("span").textContent = connected ? "Actual connected" : "Actual not connected";
}

function setBadge(id, count) {
  const item = document.querySelector(`#${id}`);
  if (!item) return;
  item.textContent = count || "";
  item.classList.toggle("visible", Boolean(count));
}

async function renderRoute({ quiet = false, poll = false } = {}) {
  if (!quiet) content.innerHTML = `<div class="initial-loader"><span class="loader-mark"></span><p>Loading…</p></div>`;
  try {
    state.data = await api("/api/overview");
    if (state.route === "overview") {
      try { state.health = await api("/api/health"); } catch { /* keep the last answer */ }
    }
    if (state.route === "overview") await renderOverview();
    if (state.route === "review") await renderReview({ poll });
    if (state.route === "intelligence") await renderIntelligence({ poll });
    if (state.route === "accounts") await renderAccounts();
    if (state.route === "activity") await renderActivity();
    if (state.route === "settings") await renderSettings();
  } catch (error) {
    if (!quiet) content.innerHTML = `<div class="panel">${emptyState("!", "Could not load this page", error.message)}</div>`;
  }
  updateChrome();
}

async function renderOverview() {
  const data = state.data;
  const counts = data.counts || {};
  const view = overview();
  // Everything read straight off the snapshot first, then anything derived
  // from it -- a derived value placed above its source throws.
  const health = view.health || [];
  const fresh = view.freshness || {};
  const report = budget();

  const degraded = health.filter(isDegraded);
  const staleAccounts = (fresh.accounts || []).filter((item) => item.stale);
  // Everything else on this page is only as true as the last read, so the one
  // thing worth reporting about Clerk itself is when that read happened.
  const staleSnapshot = Number(data.stale) > 6 * 3600;
  const lastRead = data.last_sync?.completed_at ? relativeTime(data.last_sync.completed_at) : "not yet";

  const configured = state.health?.configured || {};
  const setupBanner = !configured.actual
    ? `<div class="alert-banner"><span>1</span><div><strong>Connect Actual Budget to get started</strong><p>Clerk needs your Actual server URL, password, and budget sync ID before it can read anything. Nothing runs on a schedule until then.</p></div><a class="button primary" href="#settings-actual">Open settings</a></div>`
    : !configured.simplefin && !configured.plaid
      ? `<div class="alert-banner"><span>2</span><div><strong>Connect SimpleFIN or Plaid to verify your bank links</strong><p>Without a bank provider Clerk can only tell that transactions stopped arriving, not that a bank connection is the reason.</p></div><a class="button ghost" href="#accounts">Connect</a></div>`
      : "";

  content.innerHTML = `
    ${setupBanner}
    ${degraded.length ? `<div class="alert-banner critical"><span>!</span><div><strong>${degraded.length} bank connection${degraded.length === 1 ? "" : "s"} need attention</strong><p>${escapeHtml(degraded.map((item) => `${item.account_name}: ${item.status_label || item.status}`).join(" · ").slice(0, 200))}</p></div><a class="button ghost" href="#accounts">Open connections</a></div>` : ""}
    ${staleAccounts.length ? `<div class="alert-banner"><span>↻</span><div><strong>${staleAccounts.length === 1 ? "An account has gone quiet" : `${staleAccounts.length} accounts have gone quiet`}</strong><p>${escapeHtml(staleAccounts.map((item) => `${item.account_name} (${item.days_since_transaction === null || item.days_since_transaction === undefined ? "no transactions yet" : `${item.days_since_transaction} day${item.days_since_transaction === 1 ? "" : "s"}`})`).join(" · ").slice(0, 220))}. If an account is dormant by design, turn its monitoring off on the Connections page.</p></div><button class="button ghost" data-action="sync-now">Sync now</button></div>` : ""}

    ${budgetHero()}

    <section class="grid metrics">
      <article class="metric-card"><span class="metric-icon">▤</span><span class="metric-label">Committed (budgeted)</span><div class="metric-value">${money(report.committed_cents || 0, { compact: true })}</div><span class="metric-note">${money(report.committed_spent_cents || 0)} of it spent so far</span></article>
      <article class="metric-card"><span class="metric-icon">◇</span><span class="metric-label">Discretionary spent</span><div class="metric-value">${money(report.discretionary_spent_cents || 0, { compact: true })}</div><span class="metric-note">against ${money(report.available_cents ?? report.free_cents ?? 0)} available</span></article>
      <article class="metric-card ${report.uncategorized_count ? "warning" : "good"}"><span class="metric-icon">✎</span><span class="metric-label">Uncategorized this month</span><div class="metric-value">${report.uncategorized_count || 0}</div><span class="metric-note">${money(report.uncategorized_cents || 0)} unaccounted for</span></article>
      <article class="metric-card ${staleSnapshot ? "warning" : ""}"><span class="metric-icon">◈</span><span class="metric-label">Filed by Clerk today</span><div class="metric-value">${counts.applied_today || 0}</div><span class="metric-note">${counts.needs_review ? `${counts.needs_review} waiting · ` : ""}read from Actual ${escapeHtml(lastRead)}</span></article>
    </section>

    ${anticipatedOverviewPanel(view.anticipated || [])}

    <section class="grid overview-grid">
      <div>
        <article class="panel">
          <header class="panel-head"><div><h2>Where the free money went</h2><p>Discretionary spending this month, largest first</p></div></header>
          <div class="quick-list">${(report.top_categories || []).length
            ? report.top_categories.map((item) => `<div class="quick-item"><span>${escapeHtml((item.category_name || "?").slice(0, 1).toUpperCase())}</span><div><strong>${escapeHtml(item.category_name)}</strong><p>${escapeHtml(item.group_name || "")}</p></div><span class="row-amount money">${money(item.spent_cents)}</span></div>`).join("")
            : emptyState("✓", "Nothing discretionary yet", "Spending outside your committed categories shows up here as the month goes on.")}</div>
        </article>
      </div>
      <div>
        <article class="panel">
          <header class="panel-head"><div><h2>Bank connections</h2><p>Checked directly against each bank provider, not just Actual's last sync</p></div><a href="#accounts" class="panel-link">All accounts</a></header>
          <div class="status-list">${health.length ? health.slice(0, 6).map((item) => healthRow(item)).join("") : emptyState("⇄", "No connection data yet", "Clerk checks every linked account against its bank provider. Run a sync, or connect SimpleFIN or Plaid on the Connections page.")}</div>
        </article>
      </div>
    </section>`;
}

// A backlog is made of merchants, not transactions: the same coffee shop can
// account for forty rows and one decision. Grouping is what makes a long
// queue finishable, so the queue is grouped before it is drawn.
function groupReviews(reviews) {
  const groups = new Map();
  for (const item of reviews) {
    const key = item.merchant_key || item.payee_name || item.id;
    if (!groups.has(key)) {
      groups.set(key, {
        key,
        label: item.payee_name || item.merchant_key || "Transaction",
        items: [],
        total_cents: 0,
        suggestion_id: item.category_id || "",
        suggestion_name: item.category_name || "",
        proposed: item.proposed_category || "",
        confidence: item.confidence ?? 0,
        source: item.source,
      });
    }
    const group = groups.get(key);
    group.items.push(item);
    group.total_cents += Number(item.amount_cents) || 0;
    group.confidence = Math.min(group.confidence, item.confidence ?? 0);
    if (!group.suggestion_id && item.category_id) {
      group.suggestion_id = item.category_id;
      group.suggestion_name = item.category_name || "";
    }
  }
  return [...groups.values()].sort((a, b) => b.items.length - a.items.length
    || Math.abs(b.total_cents) - Math.abs(a.total_cents));
}

// What Clerk suggested and what the user has picked are two different things
// on the same control: the label and the highlighted option follow the pick,
// the "Suggested" tag stays on the suggestion.
function reviewSelection(group) {
  const categories = overview().categories || [];
  const suggested = categories.find((category) => category.id === group.suggestion_id) || null;
  const chosen = categories.find((category) => category.id === state.reviewChoices.get(group.key)) || null;
  return { suggested, selected: chosen || suggested };
}

function categoryPicker(group) {
  const categories = overview().categories || [];
  const { suggested, selected } = reviewSelection(group);
  const suggestedId = suggested?.id || "";
  const selectedId = selected?.id || "";
  const grouped = new Map();
  for (const category of categories) {
    const name = category.group_name || "Other";
    if (!grouped.has(name)) grouped.set(name, []);
    grouped.get(name).push(category);
  }
  const options = [...grouped.entries()].map(([groupName, items]) => `
    <section class="category-picker-group" data-category-group>
      <p>${escapeHtml(groupName)}</p>
      ${items.map((category) => `<button type="button" role="option" aria-selected="${category.id === selectedId}" class="category-picker-option ${category.id === selectedId ? "selected" : ""}" data-action="category-picker-select" data-category-id="${escapeHtml(category.id)}" data-category-name="${escapeHtml(category.name)}" data-group-name="${escapeHtml(groupName)}" data-search="${escapeHtml(`${groupName} ${category.name}`.toLocaleLowerCase())}"><span>${escapeHtml(category.name)}</span>${category.id === suggestedId ? "<i>Suggested</i>" : ""}</button>`).join("")}
    </section>`).join("");
  const label = selected
    ? `${selected.group_name ? `${selected.group_name} · ` : ""}${selected.name}`
    : "Choose a category…";
  return `<div class="category-picker" data-role="review-category" data-id="${escapeHtml(group.key)}" data-value="${escapeHtml(selectedId)}" data-suggestion-id="${escapeHtml(suggestedId)}">
    <button type="button" class="category-picker-trigger" data-action="category-picker-toggle" aria-haspopup="listbox" aria-expanded="false">
      <span data-role="category-picker-label">${escapeHtml(label)}</span><i aria-hidden="true">⌄</i>
    </button>
    <div class="category-picker-menu" data-role="category-picker-menu" hidden>
      <div class="category-picker-search-wrap"><span aria-hidden="true">⌕</span><input class="category-picker-search" data-role="category-picker-search" type="search" placeholder="Search categories" autocomplete="off" aria-label="Search categories" /></div>
      <div class="category-picker-options" role="listbox">${options}</div>
      <p class="category-picker-empty" data-role="category-picker-empty" hidden>No matching categories</p>
    </div>
  </div>`;
}

function reviewGroupRow(group) {
  const ids = group.items.map((item) => item.id).join(",");
  const dates = group.items.map((item) => item.transaction_date).sort();
  const span = dates.length > 1 ? `${shortDate(dates[0])} – ${shortDate(dates[dates.length - 1])}` : shortDate(dates[0]);
  const accounts = [...new Set(group.items.map((item) => item.account_name).filter(Boolean))];
  const { suggested, selected } = reviewSelection(group);
  const suggestedId = suggested?.id || "";
  const changed = Boolean(selected) && selected.id !== suggestedId;
  return `<article class="data-row review-row ${changed ? "changed" : ""}">
    <div class="row-title">
      <strong>${escapeHtml(group.label)}</strong>
      <small>${group.items.length} transaction${group.items.length === 1 ? "" : "s"} · ${span}${accounts.length ? ` · ${escapeHtml(accounts.slice(0, 2).join(", "))}` : ""}</small>
    </div>
    <div class="row-amount money">${money(group.total_cents)}</div>
    <div class="cell-select">${categoryPicker(group)}</div>
    <div class="cell-confidence"><span class="confidence ${group.confidence < 0.5 ? "low" : ""}">${percent(group.confidence)}</span><small>confidence</small></div>
    <div class="row-actions">
      ${!group.suggestion_id && group.proposed ? `<button class="button secondary small" data-action="create-category" data-name="${escapeHtml(group.proposed)}" title="The model found no existing category for this merchant">+ ${escapeHtml(group.proposed)}</button>` : ""}
      <button class="button ghost small" data-action="review-detail" data-id="${escapeHtml(group.items[0].id)}">Why</button>
      <button class="button ghost small" data-action="review-dismiss-group" data-ids="${escapeHtml(ids)}">Skip</button>
      <button class="button secondary small" data-action="review-accept-group" data-always="1" data-ids="${escapeHtml(ids)}" data-key="${escapeHtml(group.key)}" data-suggestion-id="${escapeHtml(suggestedId)}" title="Apply this category and make it a rule, so this merchant is always filed here without asking" ${selected && group.key && !group.key.startsWith(" ") ? "" : "disabled"}>Always</button>
      <button class="button primary small" data-action="review-accept-group" data-ids="${escapeHtml(ids)}" data-key="${escapeHtml(group.key)}" data-suggestion-id="${escapeHtml(suggestedId)}" ${selected ? "" : "disabled"}>Apply${group.items.length > 1 ? ` ${group.items.length}` : ""}</button>
    </div>
  </article>`;
}

async function renderReview({ poll = false } = {}) {
  const reviews = await api("/api/reviews");
  if (state.route !== "review") return;
  // The poll checks for an open picker before it asks the server, but the
  // picker can open while the answer is in flight; redrawing then would pull
  // the menu out from under the pointer.
  if (document.querySelector(".category-picker.open")) return;
  state.reviews = reviews;
  const groups = groupReviews(state.reviews);
  for (const key of [...state.reviewChoices.keys()]) {
    if (!groups.some((group) => group.key === key)) state.reviewChoices.delete(key);
  }
  const fingerprint = JSON.stringify([
    state.reviews.map((item) => [item.id, item.category_id, item.confidence]),
    (overview().categories || []).map((category) => [category.id, category.name, category.group_name]),
  ]);
  if (poll && fingerprint === state.reviewFingerprint) return;
  state.reviewFingerprint = fingerprint;
  const allIds = state.reviews.map((item) => item.id).join(",");
  const withSuggestion = groups.filter((group) => group.suggestion_id).length;
  content.innerHTML = `
    <section class="page-intro"><div><h2>Review</h2><p>Clerk follows your rules and reliable merchant history on its own; every first-time merchant requires your approval. <strong>Apply</strong> files these transactions and teaches Clerk; <strong>Always</strong> also makes a rule, so the merchant is never asked about again. Retry waiting items after changing the model; catch up older history only to search beyond the normal recent window.</p></div><div class="actions"><button class="button ghost" data-action="catch-up">Catch up older history</button><button class="button primary" data-action="retry-reviews" ${state.reviews.length ? "" : "disabled"}>Retry review queue${state.reviews.length ? ` (${state.reviews.length})` : ""}</button></div></section>
    <article class="panel review-panel">
      <header class="panel-head">
        <div><h2>Waiting on you</h2><p>${state.reviews.length} transaction${state.reviews.length === 1 ? "" : "s"} across ${groups.length} merchant${groups.length === 1 ? "" : "s"}${withSuggestion ? ` · ${withSuggestion} with a suggestion ready` : ""}</p></div>
        ${state.reviews.length ? `<div class="actions"><button class="button ghost small" data-action="review-dismiss-all" data-ids="${escapeHtml(allIds)}">Skip all ${state.reviews.length}</button></div>` : ""}
      </header>
      ${groups.length ? `<div class="row-head review-row">
        <span>Merchant</span><span class="align-right">Total</span><span>Category</span><span>Confidence</span><span></span>
      </div>` : ""}
      <div class="status-list">${groups.length ? groups.map(reviewGroupRow).join("") : emptyState("✓", "Nothing is waiting", "Known merchants with reliable history can be filed automatically. Every first-time merchant appears here for approval.")}</div>
    </article>`;
}


// --------------------------------------------------------------- intelligence

function categoryOptions(categories, selectedId = "", { blank = "" } = {}) {
  const groups = new Map();
  for (const category of categories) {
    const group = category.group_name || "";
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(category);
  }
  const options = [...groups.entries()].map(([group, items]) => `<optgroup label="${escapeHtml(group)}">${items.map((c) => `<option value="${escapeHtml(c.id)}" ${c.id === selectedId ? "selected" : ""}>${escapeHtml(c.name)}</option>`).join("")}</optgroup>`).join("");
  return `${blank ? `<option value="" ${selectedId ? "" : "selected"}>${escapeHtml(blank)}</option>` : ""}${options}`;
}

function proposalRow(item, categories) {
  const body = item.payload || {};
  const evidence = item.evidence || {};
  const known = categories.some((category) => category.id === body.category_id);
  const descriptors = (evidence.descriptors || []).filter((d) => d && d !== body.merchant_label).slice(0, 2);
  const merchant = body.merchant_label || body.merchant_key || item.merchant_key;
  const picker = (selectedId, blank) => `<select class="select-inline" data-role="proposal-category" data-id="${escapeHtml(item.id)}" aria-label="Category">${categoryOptions(categories, selectedId, { blank })}</select>`;
  let title = merchant;
  let detail = escapeHtml(evidence.reason || "");
  let control = "";
  let acceptLabel = "Yes";
  let ready = true;
  if (item.kind === "rule") {
    title = `${merchant} → ${body.category_name || "?"}`;
    detail = `Make it a rule? ${escapeHtml(evidence.reason || "")}${descriptors.length ? ` Seen as ${escapeHtml(descriptors.join(", "))}.` : ""}${known ? "" : " The category is no longer in Actual."}`;
    acceptLabel = "Make the rule";
    ready = known;
  } else if (item.kind === "rule_change") {
    title = `${merchant}: ${body.from_category_name || "?"} → ${body.category_name || "?"}`;
    detail = `Change the rule? ${escapeHtml(evidence.reason || "")}`;
    control = picker(known ? body.category_id : "", known ? "" : "Choose a category…");
    acceptLabel = "Change the rule";
  } else if (item.kind === "rule_retire") {
    title = `${merchant}: retire the ${body.from_category_name || ""} rule?`;
    acceptLabel = "Retire it";
  } else if (item.kind === "alias") {
    title = `Is “${body.alias_label || body.alias_key}” the same shop as “${body.merchant_key}”?`;
    detail = `${escapeHtml(evidence.reason || "")} The model is ${percent(evidence.confidence || 0)} sure. Yes makes an alias, so the known merchant's rules and history apply to this name.`;
    acceptLabel = "Same shop";
  } else if (item.kind === "repair") {
    title = `${merchant}: ${body.from_category_name || body.from_category_id || "a category"} is gone`;
    detail = `${escapeHtml(evidence.reason || "")} Pick where it goes now.`;
    control = picker("", "Choose a category…");
    acceptLabel = "Repair";
    ready = false;
  }
  return `<article class="data-row rule-row" data-proposal-id="${escapeHtml(item.id)}">
    <span class="mark">${item.kind === "repair" ? "!" : "?"}</span>
    <div class="row-title"><strong>${escapeHtml(title)}</strong><small>${detail}</small></div>
    <div class="row-meta">${control || `${evidence.observations ? `${evidence.observations} consistent filing(s)<br />` : evidence.disputes ? `${evidence.disputes} correction(s)<br />` : ""}`}<span>asked ${relativeTime(item.created_at)}</span></div>
    <div class="row-actions">
      <button class="button ghost small" data-action="proposal-decline" data-id="${escapeHtml(item.id)}">No</button>
      <button class="button primary small" data-action="proposal-accept" data-id="${escapeHtml(item.id)}" ${ready ? "" : "disabled"}>${acceptLabel}</button>
    </div>
  </article>`;
}

function ruleRow(item, categories, accounts) {
  const known = categories.some((category) => category.id === item.category_id);
  const account = accounts.find((entry) => entry.id === item.account_id);
  const scope = item.account_id ? (account ? account.name : "an account no longer in Actual") : "any account";
  const source = { user: "made by you", proposal: "from a proposal you accepted", imported: "imported from Actual" }[item.source] || item.source;
  const status = item.status === "active" ? (known ? ["ok", "Active"] : ["error", "Needs a category"]) : item.status === "paused" ? ["muted", "Paused"] : ["muted", "Retired"];
  const actions = item.status === "retired"
    ? `<button class="button ghost small" data-action="rule-status" data-status="active" data-id="${escapeHtml(item.id)}">Reinstate</button>`
    : `${item.status === "active"
      ? `<button class="button ghost small" data-action="rule-status" data-status="paused" data-id="${escapeHtml(item.id)}" title="Keep the rule but stop applying it">Pause</button>`
      : `<button class="button ghost small" data-action="rule-status" data-status="active" data-id="${escapeHtml(item.id)}">Resume</button>`}
      <button class="button ghost small" data-action="rule-status" data-status="retired" data-id="${escapeHtml(item.id)}">Retire</button>`;
  return `<article class="data-row rule-row" data-rule-search="${escapeHtml(`${item.merchant_label} ${item.merchant_key} ${item.category_name}`.toLocaleLowerCase())}">
    <span class="mark rule">⚡</span>
    <div class="row-title"><strong>${escapeHtml(item.merchant_label || item.merchant_key)}</strong><small>matches “${escapeHtml(item.merchant_key)}”${item.match === "family" ? " and its store variants" : ""} · ${escapeHtml(scope)} · ${escapeHtml(source)}</small></div>
    <div class="row-meta">${item.status === "retired"
      ? `<strong>${escapeHtml(item.category_name || "—")}</strong>`
      : `<select class="select-inline" data-action="rule-category" data-id="${escapeHtml(item.id)}" title="The category this rule files into">${known ? "" : `<option value="${escapeHtml(item.category_id)}" selected>${escapeHtml(item.category_name || "Missing category")} (gone)</option>`}${categoryOptions(categories, item.category_id)}</select>`}<br /><span>${item.applied_count ? `filed ${item.applied_count} · last ${relativeTime(item.last_applied_at)}` : "not used yet"}${item.disputed_count ? ` · ${item.disputed_count} disputed` : ""}</span></div>
    <div class="health-state">${statusChip(status[0], status[1])}</div>
    <div class="row-actions">${actions}</div>
  </article>`;
}

function filterRuleRows() {
  const query = state.ruleSearch.trim().toLocaleLowerCase();
  document.querySelectorAll("[data-rule-search]").forEach((row) => {
    row.hidden = Boolean(query) && !row.dataset.ruleSearch.includes(query);
  });
}

// One page, five things to look at. Each tab is its own list with its own
// filters, so the page is as long as the list being read rather than the sum
// of all of them.
const INTELLIGENCE_TABS = [
  ["proposals", "Proposals"],
  ["rules", "Rules"],
  ["merchants", "Merchants"],
  ["aliases", "Aliases"],
  ["actual", "From Actual"],
];
const PROPOSAL_KINDS = [
  ["rule", "New rules"],
  ["rule_change", "Changes"],
  ["rule_retire", "Retirements"],
  ["alias", "Aliases"],
  ["repair", "Repairs"],
];
const INTELLIGENCE_SEARCHES = ["rule-search", "merchant-search", "alias-search"];

function filterChips(action, current, chips) {
  return `<div class="filter-tabs" role="group">${chips.map(([id, label, count]) => `<button type="button" class="${id === current ? "active" : ""}" data-action="${action}" data-filter="${escapeHtml(id)}">${escapeHtml(label)}${count === undefined ? "" : ` (${count})`}</button>`).join("")}</div>`;
}

function intelligenceTabs(counts) {
  return `<div class="page-tabs" role="tablist" aria-label="Intelligence">${INTELLIGENCE_TABS.map(([id, label]) => {
    const count = counts[id];
    const badge = count === undefined || count === "" ? "" : `<b class="${id === "proposals" && count ? "attention" : ""}">${escapeHtml(String(count))}</b>`;
    return `<button type="button" role="tab" aria-selected="${id === state.intelligenceTab}" class="${id === state.intelligenceTab ? "active" : ""}" data-action="intelligence-tab" data-tab="${id}">${label}${badge}</button>`;
  }).join("")}</div>`;
}

// The page redraws every poll; a form the user has opened, or is typing in,
// must not be swept away under them.
function intelligenceFormBusy() {
  const active = document.activeElement;
  if (active?.closest?.("#rule-form, #alias-form")) return true;
  return Boolean(document.querySelector("#rule-form [name=merchant]")?.value
    || document.querySelector("#alias-form [name=alias]")?.value
    || document.querySelector("#alias-form [name=merchant]")?.value);
}

async function renderIntelligence({ poll = false } = {}) {
  const page = await api("/api/intelligence?include_retired=true");
  if (state.route !== "intelligence") return;
  if (poll && intelligenceFormBusy()) return;
  const fingerprint = JSON.stringify([page.proposals, page.rules, page.merchants, page.aliases, page.categories, page.accounts]);
  state.intelligence = page;
  if (poll && fingerprint === state.intelligenceFingerprint) return;
  state.intelligenceFingerprint = fingerprint;
  drawIntelligence();
}

// Draws from what was last read. A tab or filter change is a redraw, not a
// round trip.
function drawIntelligence() {
  const page = state.intelligence || {};
  const proposals = page.proposals || [];
  const rules = page.rules || [];
  const live = rules.filter((rule) => rule.status !== "retired");
  if (!INTELLIGENCE_TABS.some(([id]) => id === state.intelligenceTab)) state.intelligenceTab = proposals.length ? "proposals" : "rules";
  const counts = {
    proposals: proposals.length,
    rules: live.length,
    merchants: (page.merchants || []).length,
    aliases: (page.aliases || []).length,
    actual: state.actualRules ? (state.actualRules.counts?.in_actual || 0) : "",
  };
  const activeElement = document.activeElement;
  const restoreFocus = INTELLIGENCE_SEARCHES.includes(activeElement?.id) ? activeElement.id : "";
  const panels = { proposals: proposalsPanel, rules: rulesPanel, merchants: merchantsPanel, aliases: aliasesPanel, actual: actualPanel };
  content.innerHTML = `
    <section class="page-intro"><div><h2>Intelligence</h2><p>Everything Clerk knows about what your transactions mean, and everything it wants to know. A <strong>rule</strong> is your word: it files a merchant before any evidence or model is consulted. Clerk proposes rules from what it has filed consistently, and never makes one on its own.</p></div><div class="actions"><a class="button ghost" href="#review">Review queue</a></div></section>
    ${intelligenceTabs(counts)}
    <div role="tabpanel">${panels[state.intelligenceTab](page)}</div>`;
  if (state.ruleSearch) filterRuleRows();
  if (state.merchantSearch) filterMerchantRows();
  if (state.aliasSearch) filterAliasRows();
  if (restoreFocus) {
    const search = document.getElementById(restoreFocus);
    search?.focus();
    search?.setSelectionRange(search.value.length, search.value.length);
  }
}

function proposalsPanel(page) {
  const categories = page.categories || [];
  const proposals = page.proposals || [];
  const kinds = PROPOSAL_KINDS.map(([kind, label]) => [kind, label, proposals.filter((item) => item.kind === kind).length]).filter((chip) => chip[2]);
  if (!kinds.some(([kind]) => kind === state.proposalFilter)) state.proposalFilter = "all";
  const shown = proposals.filter((item) => state.proposalFilter === "all" || item.kind === state.proposalFilter);
  return `<article class="panel">
    <header class="panel-head"><div><h2>Proposals</h2><p>What Clerk wants to know: merchants filed the same way often enough to deserve a rule, rules your corrections in Actual disagree with, rules whose category has left the budget, and names the model believes belong to one shop.</p></div>
      <div class="panel-actions">
        <button class="button ghost small" data-action="propose-from-history" title="Ask for a rule wherever your history has filed a merchant one way, every time, often enough">Propose from history</button>
        ${proposals.length > 3 ? `<button class="button ghost small" data-action="proposals-decline-all" data-count="${proposals.length}">Decline all</button>` : ""}
      </div></header>
    ${kinds.length > 1 ? `<div class="toolbar">${filterChips("proposal-filter", state.proposalFilter, [["all", "All", proposals.length], ...kinds])}</div>` : ""}
    <div class="status-list">${shown.length ? shown.map((item) => proposalRow(item, categories)).join("") : emptyState("?", "Nothing to decide", "Clerk asks here when a merchant has been filed the same way a few times, when a correction in Actual disagrees with a rule, or when a name looks like a shop it already knows. Propose from history looks through everything filed so far.")}</div>
  </article>`;
}

function rulesPanel(page) {
  const categories = page.categories || [];
  const accounts = page.accounts || [];
  const rules = page.rules || [];
  const known = (rule) => categories.some((category) => category.id === rule.category_id);
  const buckets = {
    active: rules.filter((rule) => rule.status === "active"),
    paused: rules.filter((rule) => rule.status === "paused"),
    retired: rules.filter((rule) => rule.status === "retired"),
    orphaned: rules.filter((rule) => rule.status !== "retired" && !known(rule)),
  };
  if (!buckets[state.ruleFilter] || (state.ruleFilter === "orphaned" && !buckets.orphaned.length)) state.ruleFilter = "active";
  const shown = buckets[state.ruleFilter];
  const chips = [["active", "Active", buckets.active.length], ["paused", "Paused", buckets.paused.length], ["retired", "Retired", buckets.retired.length]];
  if (buckets.orphaned.length) chips.push(["orphaned", "Needs a category", buckets.orphaned.length]);
  const empty = {
    active: ["No rules yet", "Use Always on a review, Make it a rule on a decision, or add one here. Rules Clerk proposes appear under Proposals once a merchant has been filed the same way a few times."],
    paused: ["Nothing paused", "Pause keeps a rule without applying it."],
    retired: ["Nothing retired", "Retired rules stay here so they can be reinstated."],
    orphaned: ["Every rule has its category", ""],
  }[state.ruleFilter];
  return `<article class="panel">
    <header class="panel-head"><div><h2>Rules</h2><p>A merchant is matched on the key its statement name becomes, so one rule covers every store number and processor prefix. Rules apply even when known merchants are set to propose-only.</p></div>
      <div class="panel-actions"><button class="button ${state.ruleFormOpen ? "ghost" : "secondary"} small" data-action="rule-form-toggle" aria-expanded="${state.ruleFormOpen}" aria-controls="rule-form">${state.ruleFormOpen ? "Close" : "+ Add rule"}</button></div></header>
    <form class="toolbar inline-form" id="rule-form" autocomplete="off" ${state.ruleFormOpen ? "" : "hidden"}>
      <input class="search-input" name="merchant" type="text" placeholder="Merchant, as the statement shows it" required maxlength="200" aria-label="Merchant" />
      <span class="muted" id="rule-key-preview" style="font-size:10px;min-width:120px"></span>
      <select class="select-inline" name="category_id" required aria-label="Category">${categoryOptions(categories, "", { blank: "Category…" })}</select>
      <select class="select-inline" name="account_id" aria-label="Account scope"><option value="">Any account</option>${accounts.map((account) => `<option value="${escapeHtml(account.id)}">${escapeHtml(account.name)} only</option>`).join("")}</select>
      <label class="muted" style="font-size:10px;display:flex;gap:6px;align-items:center" title="Also match keys that add a store or city to this one, e.g. “starbucks seattle” for “starbucks”"><input type="checkbox" name="family" /> store variants</label>
      <button class="button primary small" type="submit">Add rule</button>
    </form>
    <div class="toolbar">${filterChips("rule-filter", state.ruleFilter, chips)}<input class="search-input spacer" id="rule-search" type="search" value="${escapeHtml(state.ruleSearch)}" placeholder="Filter rules" aria-label="Filter rules" /></div>
    <div class="status-list" id="rule-list">${shown.length ? shown.map((item) => ruleRow(item, categories, accounts)).join("") : emptyState("⚡", empty[0], empty[1])}</div>
  </article>`;
}

function merchantRow(item, categories) {
  const top = item.categories[0] || {};
  const known = categories.some((category) => category.id === top.category_id);
  const evidence = item.categories.map((c) => `${escapeHtml(c.category_name || c.category_id)} ×${c.hits}${c.corrections ? ` (+${c.corrections} corrected)` : ""}`).join(", ");
  return `<article class="data-row rule-row" data-merchant-search="${escapeHtml(`${item.label} ${item.merchant_key} ${item.categories.map((c) => c.category_name).join(" ")}`.toLocaleLowerCase())}">
    <span class="mark memory">↺</span>
    <div class="row-title"><strong>${escapeHtml(item.label || item.merchant_key)}</strong><small>“${escapeHtml(item.merchant_key)}” · ${evidence}</small></div>
    <div class="row-meta">${item.categories.length > 1 ? `<span class="negative">filed ${item.categories.length} ways</span>` : `<strong>${escapeHtml(top.category_name || "")}</strong>`}<br /><span>last ${relativeTime(item.last_seen)}</span></div>
    <div class="health-state">${item.rule ? statusChip("ok", "Has a rule") : ""}</div>
    <div class="row-actions">
      ${!item.rule && known && item.categories.length === 1 ? `<button class="button secondary small" data-action="rule-make" data-key="${escapeHtml(item.merchant_key)}" data-label="${escapeHtml(item.label || "")}" data-category-id="${escapeHtml(top.category_id)}" title="Always file this merchant as ${escapeHtml(top.category_name)}">Make it a rule</button>` : ""}
      <button class="button ghost small" data-action="alias-teach" data-alias="${escapeHtml(item.label || item.merchant_key)}" title="Name the payee the bank posts this merchant as">Posts as…</button>
      <button class="button ghost small" data-action="forget-merchant" data-key="${escapeHtml(item.merchant_key)}">Forget</button>
    </div>
  </article>`;
}

function aliasRow(alias) {
  const source = { taught: "taught by you", settled: "learned when a charge settled", actual_payee: "from your payees in Actual", proposed: "proposed" }[alias.source] || alias.source;
  return `<article class="data-row" style="grid-template-columns:30px minmax(0,1fr) auto" data-alias-search="${escapeHtml(`${alias.alias_label} ${alias.alias_key} ${alias.merchant_label} ${alias.merchant_key}`.toLocaleLowerCase())}">
    <span class="dot ok"></span>
    <div class="row-title"><strong>${escapeHtml(alias.alias_label || alias.alias_key)} → ${escapeHtml(alias.merchant_label || alias.merchant_key)}</strong><small>“${escapeHtml(alias.alias_key)}” resolves to “${escapeHtml(alias.merchant_key)}” · ${escapeHtml(source)}</small></div>
    <div class="row-actions"><button class="button ghost small" data-action="alias-delete" data-id="${escapeHtml(alias.alias_key)}">Forget</button></div>
  </article>`;
}

function filterMerchantRows() {
  const query = state.merchantSearch.trim().toLocaleLowerCase();
  document.querySelectorAll("[data-merchant-search]").forEach((row) => {
    row.hidden = Boolean(query) && !row.dataset.merchantSearch.includes(query);
  });
}

function filterAliasRows() {
  const query = state.aliasSearch.trim().toLocaleLowerCase();
  document.querySelectorAll("[data-alias-search]").forEach((row) => {
    row.hidden = Boolean(query) && !row.dataset.aliasSearch.includes(query);
  });
}

function merchantsPanel(page) {
  const categories = page.categories || [];
  const merchants = page.merchants || [];
  const ruleKeys = new Set((page.rules || []).filter((rule) => rule.status === "active").map((rule) => rule.merchant_key));
  const rows = merchants.map((item) => ({ ...item, rule: ruleKeys.has(item.merchant_key) }));
  const buckets = {
    all: rows,
    consistent: rows.filter((item) => item.categories.length === 1),
    split: rows.filter((item) => item.categories.length > 1),
    unruled: rows.filter((item) => !item.rule),
  };
  if (!buckets[state.merchantFilter]) state.merchantFilter = "all";
  const shown = buckets[state.merchantFilter];
  const empty = {
    all: ["Nothing learned yet", "Clerk records a merchant here when it applies a category or you approve one in Review."],
    consistent: ["No merchant filed one way", ""],
    split: ["No merchant filed several ways", "Every merchant Clerk knows has gone to one category."],
    unruled: ["Every merchant has a rule", ""],
  }[state.merchantFilter];
  return `<article class="panel" id="merchants-panel">
    <header class="panel-head"><div><h2>Merchants</h2><p>What Clerk has learned without being told: the merchants it has filed or you have approved, with the evidence. A merchant filed several ways is worth a rule; a merchant filed one way can become one with a click.</p></div></header>
    <div class="toolbar">${filterChips("merchant-filter", state.merchantFilter, [["all", "All", buckets.all.length], ["consistent", "Filed one way", buckets.consistent.length], ["split", "Filed several ways", buckets.split.length], ["unruled", "Without a rule", buckets.unruled.length]])}<input class="search-input spacer" id="merchant-search" type="search" value="${escapeHtml(state.merchantSearch)}" placeholder="Filter merchants" aria-label="Filter merchants" /></div>
    <div class="status-list">${shown.length ? shown.map((item) => merchantRow(item, categories)).join("") : emptyState("↺", empty[0], empty[1])}</div>
  </article>`;
}

function aliasesPanel(page) {
  const aliases = page.aliases || [];
  return `<article class="panel" id="aliases-panel">
    <header class="panel-head"><div><h2>Aliases</h2><p>One shop, several names. An alias is followed before rules and evidence are consulted, so a rule for the bank's name reaches the phone's name too.</p></div>
      <div class="panel-actions"><button class="button ${state.aliasFormOpen ? "ghost" : "secondary"} small" data-action="alias-form-toggle" aria-expanded="${state.aliasFormOpen}" aria-controls="alias-form">${state.aliasFormOpen ? "Close" : "+ Add alias"}</button></div></header>
    <form class="toolbar inline-form" id="alias-form" autocomplete="off" ${state.aliasFormOpen ? "" : "hidden"}>
      <input class="search-input" name="alias" type="text" placeholder="A name for the shop (as the phone or a statement shows it)" required maxlength="200" aria-label="Alias" />
      <span class="muted" style="font-size:11px">posts as</span>
      <input class="search-input" name="merchant" type="text" placeholder="The payee the bank posts it as" required maxlength="200" aria-label="Merchant" />
      <button class="button primary small" type="submit">Add alias</button>
    </form>
    <div class="toolbar"><input class="search-input" id="alias-search" type="search" value="${escapeHtml(state.aliasSearch)}" placeholder="Filter aliases" aria-label="Filter aliases" /><span class="muted spacer" style="font-size:11px">${aliases.length} alias${aliases.length === 1 ? "" : "es"}</span></div>
    <div class="status-list">${aliases.length ? aliases.map(aliasRow).join("") : emptyState("⇢", "No aliases yet", "Clerk learns one when a phone charge settles against a differently named payee, or when you teach one here or with Posts as… on a merchant.")}</div>
  </article>`;
}

function actualRuleRow(rule) {
  const move = rule.disposition === "move";
  const managed = rule.managed || [];
  const problems = rule.translations.filter((t) => t.problem);
  const state = managed.length
    ? (managed.every((m) => m.actual_status === "retired") ? ["muted", "Retired in Actual"] : ["ok", "In Clerk, still in Actual"])
    : move ? (problems.length === rule.translations.length ? ["error", "Cannot move"] : problems.length ? ["warning", "Partly"] : ["suggested", "Ready to move"]) : ["muted", "Stays in Actual"];
  const detail = move
    ? rule.translations.map((t) => {
      const replay = t.replay || {};
      const where = t.account_id ? ` on ${escapeHtml(t.account_name || "an account")}` : "";
      const played = replay.matched ? `${replay.matched} past row${replay.matched === 1 ? "" : "s"}, ${replay.agree} filed as ${escapeHtml(t.category_name)}${replay.disagree ? `, ${replay.disagree} as ${escapeHtml(Object.keys(replay.disagreeing || {}).join(", "))}` : ""}` : "no past rows to compare";
      return `<div><strong>“${escapeHtml(t.merchant_key || "—")}”</strong>${where} → ${escapeHtml(t.category_name || "?")} · ${played}${t.problem ? ` · <span class="negative">${escapeHtml(t.problem)}</span>` : t.existing ? " · already a Clerk rule" : ""}</div>`;
    }).join("")
    : escapeHtml(rule.reason);
  return `<article class="data-row" style="grid-template-columns:30px minmax(0,1fr) auto auto">
    <span class="dot ${state[0] === "suggested" ? "stale" : state[0]}"></span>
    <div class="row-title"><strong>${escapeHtml(rule.summary)}</strong><small>${detail}</small></div>
    <div class="health-state">${statusChip(state[0], state[1])}</div>
    <div class="row-actions">${move && !managed.length && problems.length < rule.translations.length ? `<button class="button ghost small" data-action="actual-import" data-ids="${escapeHtml(rule.id)}">Import</button>` : ""}${managed.length && managed.every((m) => m.actual_status !== "retired") ? `<button class="button ghost small" data-action="actual-retire" data-ids="${escapeHtml(rule.id)}">Retire in Actual</button>` : ""}</div>
  </article>`;
}

function actualPanel() {
  const reading = state.actualRules;
  const counts = reading?.counts || {};
  const rules = reading?.rules || [];
  const movable = rules.filter((rule) => rule.disposition === "move" && !(rule.managed || []).length);
  const present = rules.filter((rule) => (rule.managed || []).some((m) => m.actual_status !== "retired"));
  const kept = rules.filter((rule) => rule.disposition !== "move");
  const retiredCount = counts.retired || 0;
  return `<article class="panel" id="actual-rules-panel">
    <header class="panel-head"><div><h2>From Actual</h2><p>Actual's own rule table. Rules that only pick a category can move here; rules that set a transfer payee, delete a transaction, or depend on more than the payee stay in Actual. Each step has a preview and can be undone.</p></div>
      <div class="panel-actions">${reading
        ? `<button class="button ghost small" data-action="actual-read">Re-read</button>`
        : `<button class="button primary small" data-action="actual-read">Read Actual's rules</button>`}</div></header>
    ${state.actualRulesError ? `<div class="alert-banner"><span>!</span><div><strong>Could not read Actual</strong><p>${escapeHtml(state.actualRulesError)}</p></div></div>` : ""}
    ${reading ? `
      <div class="toolbar"><span class="muted" style="font-size:11px">${counts.in_actual || 0} rule${counts.in_actual === 1 ? "" : "s"} in Actual · ${movable.length} ready to move · ${present.length} imported and still in Actual · ${retiredCount} retired · ${kept.length} kept</span>
        <div class="actions spacer">
          <button class="button ghost small" data-action="actual-import" data-dry="1" ${movable.length ? "" : "disabled"}>Preview import</button>
          <button class="button secondary small" data-action="actual-import" ${movable.length ? "" : "disabled"}>Import ${movable.length || ""}</button>
          <button class="button ghost small" data-action="actual-retire" data-dry="1" ${present.length ? "" : "disabled"}>Preview retire</button>
          <button class="button danger small" data-action="actual-retire" ${present.length ? "" : "disabled"}>Retire ${present.length || ""} in Actual</button>
          <button class="button ghost small" data-action="actual-restore" ${retiredCount ? "" : "disabled"}>Restore ${retiredCount || ""} to Actual</button>
        </div></div>
      <div class="status-list" id="actual-rules-list">${rules.length ? [...movable, ...present, ...kept].map(actualRuleRow).join("") : emptyState("✓", "No rules in Actual", "Nothing to take over.")}</div>
      <div id="actual-rules-result"></div>`
      : `<div class="panel-body"><p class="muted" style="margin:0;font-size:11px">Reads the live rule table and replays each rule over your history to show what it would have matched. Nothing changes until you import, retire, or restore.</p></div>`}
  </article>`;
}

function renderActualResult(kind, result) {
  const box = document.querySelector("#actual-rules-result");
  if (!box) return;
  const lines = [];
  const label = result.dry_run ? "Would " : "";
  for (const item of result.imported || []) lines.push(`${label}import “${item.merchant_key}” → ${item.category_name}${item.account_id ? " (one account)" : ""}${item.already_in_clerk ? " · already a Clerk rule, adopted" : ""}`);
  for (const item of result.skipped || []) lines.push(`Skipped “${item.merchant_key || item.merchant_label}”: ${item.reason}`);
  for (const item of result.retired || []) lines.push(`${label}retire in Actual: ${(item.merchants || []).join(", ")}`);
  for (const item of result.restored || []) lines.push(`${label}restore to Actual: ${(item.merchants || []).join(", ")}`);
  for (const item of result.failed || []) lines.push(`Failed ${(item.merchants || []).join(", ")}: ${item.error}`);
  if (!lines.length) lines.push(`Nothing to ${kind}.`);
  box.innerHTML = `<div class="panel-body"><h3 style="margin:0 0 8px;font-size:12px">${result.dry_run ? "Preview" : "Done"}</h3><div class="change-list">${lines.map((line) => `<div class="change"><i>→</i><div><small style="color:var(--ink-soft)">${escapeHtml(line)}</small></div></div>`).join("")}</div></div>`;
}

async function readActualRules() {
  state.actualRulesError = "";
  try { state.actualRules = await api("/api/intelligence/actual"); }
  catch (error) { state.actualRulesError = error.message; }
}

let ruleKeyPreviewTimer = null;
async function previewRuleKey(text) {
  const preview = document.querySelector("#rule-key-preview");
  if (!preview) return;
  if (!text.trim()) { preview.textContent = ""; return; }
  try {
    const result = await api(`/api/intelligence/merchant?text=${encodeURIComponent(text)}`);
    preview.textContent = result.merchant_key ? `matches “${result.merchant_key}”` : "nothing left to match on";
  } catch { preview.textContent = ""; }
}

// ------------------------------------------------- anticipated charges

const CHARGE_STATUS = {
  open: ["stale", "Anticipated"],
  matched: ["ok", "Posted"],
  expired: ["muted", "Never posted"],
  dismissed: ["muted", "Dismissed"],
  ignored: ["muted", "Not counted"],
};

const IGNORED_LABEL = { declined: "Declined", unknown: "No amount read", notice: "Just a notice" };

function chargeLabel(item) {
  if (item.status === "ignored") return IGNORED_LABEL[item.kind] || "Not counted";
  return (CHARGE_STATUS[item.status] || ["muted", titleCase(item.status)])[1];
}

function categoryChip(item) {
  if (!item.category_id) return `<span class="status-chip muted" title="No category yet: counted as discretionary until one is known">Uncategorized</span>`;
  const how = item.category_source === "taught" ? "taught" : item.category_source === "rule" ? "by a rule you set" : `from memory · ${percent(item.category_confidence || 0)}`;
  return `<span class="status-chip ok" title="${escapeHtml(how)}">${escapeHtml(item.category_name || item.category_id)}</span>`;
}

function teachControls(item, categories, aliases) {
  if (item.status !== "open" || !categories.length) return "";
  const groups = new Map();
  for (const category of categories) {
    const group = category.group_name || "";
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group).push(category);
  }
  const options = [...groups.entries()].map(([group, items]) => `<optgroup label="${escapeHtml(group)}">${items.map((c) => `<option value="${escapeHtml(c.id)}" ${c.id === item.category_id ? "selected" : ""}>${escapeHtml(c.name)}</option>`).join("")}</optgroup>`).join("");
  const alias = aliases.find((a) => a.alias_key === item.merchant_key);
  return `<select class="select-inline" data-action="anticipated-teach-category" data-id="${escapeHtml(item.id)}" title="Always file this merchant here: a rule is made for it, for the bank's row when it lands, and for the next notification"><option value="" ${item.category_id ? "" : "selected"}>Always file as…</option>${options}</select>
    <button class="button ghost small" data-action="anticipated-teach-alias" data-id="${escapeHtml(item.id)}" data-merchant="${escapeHtml(item.merchant || "")}" title="${alias ? `Posts as ${escapeHtml(alias.merchant_label || alias.merchant_key)}` : "Name the payee the bank posts this merchant as"}">${alias ? `Posts as ${escapeHtml((alias.merchant_label || alias.merchant_key).slice(0, 18))}` : "Posts as…"}</button>`;
}

function anticipatedChargeRow(item, { sources = [], categories = [], aliases = [] } = {}) {
  const [status] = CHARGE_STATUS[item.status] || ["muted"];
  const source = sources.find((s) => s.id === item.source_id);
  const where = item.account_name || source?.account_name || "";
  const outcome = item.status === "matched"
    ? `Posted as ${escapeHtml(item.matched_payee || "a transaction")} on ${shortDate(item.matched_date)} (${escapeHtml(item.match_reason || "")})`
    : item.status === "open"
      ? `Waiting for the bank · counts as spent${item.kind === "credit" ? " (a credit, so not counted)" : ""}`
      : item.status === "expired"
        ? "Nothing matching arrived, so it stopped counting"
        : item.status === "dismissed" ? "Dismissed by hand" : escapeHtml((item.text || "").slice(0, 110));
  const actions = item.status === "open"
    ? `<button class="button ghost small" data-action="anticipated-dismiss" data-id="${escapeHtml(item.id)}" title="Stop counting this charge now">Dismiss</button>`
    : item.status === "matched" || item.status === "dismissed" || item.status === "expired"
      ? `<button class="button ghost small" data-action="anticipated-reopen" data-id="${escapeHtml(item.id)}" title="Count it again, for a match that was wrong">Reopen</button>`
      : "";
  return `<article class="data-row" style="grid-template-columns:30px minmax(0,1fr) auto auto auto" title="${escapeHtml(item.title || "")}: ${escapeHtml(item.text || "")}">
    <span class="dot ${status}"></span>
    <div class="row-title"><strong>${escapeHtml(item.merchant || item.title || "Charge")}</strong><small>${escapeHtml([[source?.app_label, where].filter(Boolean).join(" → "), `seen ${relativeTime(item.noticed_at)}`].filter(Boolean).join(" · "))}<br />${outcome}</small></div>
    <span class="row-amount money ${item.amount_cents < 0 ? "" : "positive"}">${money(item.amount_cents, { sign: true })}</span>
    <div class="health-state">${item.kind === "charge" ? categoryChip(item) : ""}${statusChip(status, chargeLabel(item))}</div>
    <div class="row-actions">${teachControls(item, categories, aliases)}${actions}</div>
  </article>`;
}

function anticipatedOverviewPanel(open) {
  const counted = open.filter((item) => item.counts);
  if (!open.length) return "";
  const total = counted.reduce((sum, item) => sum - item.amount_cents, 0);
  return `<article class="panel anticipated-panel">
    <header class="panel-head"><div><h2>Anticipated charges</h2><p>${counted.length} charge${counted.length === 1 ? "" : "s"} your phone has seen, ${money(total)} counted as spent until the bank posts ${counted.length === 1 ? "it" : "them"}. Nothing here is written into Actual.</p></div><a href="#accounts" class="panel-link">Phone sources</a></header>
    <div class="status-list">${open.map((item) => anticipatedChargeRow(item, { categories: overview().categories || [] })).join("")}</div>
  </article>`;
}

function sourceRow(source, accounts) {
  const options = accounts.map((account) => `<option value="${escapeHtml(account.id)}" ${account.id === source.actual_account_id ? "selected" : ""}>${escapeHtml(account.name)}${account.off_budget ? " (off budget)" : ""}</option>`).join("");
  const known = accounts.some((account) => account.id === source.actual_account_id);
  return `<article class="data-row" style="grid-template-columns:30px minmax(0,1fr) auto auto auto">
    <span class="dot ${source.enabled ? (known ? "ok" : "error") : "muted"}"></span>
    <div class="row-title"><strong>${escapeHtml(source.app_label || source.package_name)}</strong><small>${escapeHtml(source.device_name || source.device_id)} · ${escapeHtml(source.package_name)}${source.last_seen ? ` · last notification ${escapeHtml(relativeTime(source.last_seen))}` : " · nothing forwarded yet"}${known ? "" : " · the linked account is no longer in Actual"}</small></div>
    <select class="select-inline" data-action="anticipated-source-account" data-id="${escapeHtml(source.id)}" title="Which Actual account this app's charges land in">${known ? "" : `<option value="${escapeHtml(source.actual_account_id)}" selected>${escapeHtml(source.account_name || "Unknown account")}</option>`}${options}</select>
    <label class="toggle-control compact" title="${source.enabled ? "Forwarding is on" : "Forwarding is paused"}"><input type="checkbox" data-action="anticipated-source-toggle" data-id="${escapeHtml(source.id)}" ${source.enabled ? "checked" : ""} /><i aria-hidden="true"></i></label>
    <div class="row-actions"><button class="button danger small" data-action="anticipated-source-remove" data-id="${escapeHtml(source.id)}" data-name="${escapeHtml(source.app_label || source.package_name)}">Remove</button></div>
  </article>`;
}

function phonePanel(phone) {
  if (!phone) return "";
  const sources = phone.sources || [];
  const open = phone.open || [];
  const recent = (phone.recent || []).slice(0, 8);
  const accounts = phone.accounts || [];
  const categories = phone.categories || [];
  const aliases = phone.aliases || [];
  const intro = phone.enabled
    ? `Card-app notifications forwarded by the Actual Clerk phone app become anticipated charges: counted as spent the moment your card is charged, never written into Actual, and settled when the bank's own row arrives (within ${phone.match_window_days} days) or dropped after ${phone.expire_days}.${phone.token_configured ? "" : " No device token is set, so any device that can reach Clerk may forward notifications; set one under Settings → Phone app."}`
    : "Anticipated charges are turned off in Settings → Phone app.";
  return `<article class="panel" id="phone-panel">
    <header class="panel-head"><div><h2>Phone notifications</h2><p>${escapeHtml(intro)}</p></div><div class="panel-actions"><a class="button ghost small" href="#settings-phone">Phone app settings</a></div></header>
    <div class="status-list">${sources.length
      ? sources.map((source) => sourceRow(source, accounts)).join("")
      : emptyState("📱", "No phone sources yet", "Install the Actual Clerk phone app, point it at this server, and use Register a source to pick the card app's notification.")}</div>
    ${open.length ? `<header class="panel-head" style="margin-top:12px"><div><h3>Waiting for the bank</h3><p>Counted as spent now, against the provisional category when one is known (from your history, or taught here) and against free money otherwise. Each settles against the matching transaction when it is imported.</p></div></header><div class="status-list">${open.map((item) => anticipatedChargeRow(item, { sources, categories, aliases })).join("")}</div>` : ""}
    ${recent.length ? `<header class="panel-head" style="margin-top:12px"><div><h3>Recently settled</h3><p>What the last notifications became.</p></div></header><div class="status-list">${recent.map((item) => anticipatedChargeRow(item, { sources })).join("")}</div>` : ""}
    ${aliases.length ? `<p class="muted" style="margin:12px 20px;font-size:11px">${aliases.length} merchant alias${aliases.length === 1 ? "" : "es"} known · <a href="#intelligence">manage them under Intelligence</a></p>` : ""}
  </article>`;
}

function plaidItemStatus(item) {
  if (item.status === "needs_repair") return ["error", "Needs repair"];
  if (item.status === "error") return ["error", "Error"];
  if (item.status === "removed") return ["muted", "Removed"];
  return ["ok", "Connected"];
}

function plaidItemRow(item, { sandbox }) {
  const [status, label] = plaidItemStatus(item);
  const accounts = item.accounts || [];
  const mapped = accounts.filter((account) => account.link && account.link.enabled).length;
  const detail = item.error
    ? (item.error.display_message || item.error.error_message || item.error.message || item.error.error_code || "Plaid reported a problem")
    : `${accounts.length} account${accounts.length === 1 ? "" : "s"} · ${mapped} mapped to Actual${item.last_successful_update ? ` · bank data ${relativeTime(item.last_successful_update)}` : ""}`;
  return `<article class="data-row clickable" style="grid-template-columns:30px minmax(0,1fr) auto auto" data-action="plaid-item-detail" data-id="${escapeHtml(item.item_id)}">
    <span class="dot ${status}"></span>
    <div class="row-title"><strong>${escapeHtml(item.institution_name || item.institution_id || "Bank")}</strong><small>${escapeHtml(detail.slice(0, 140))}</small></div>
    <div class="health-state">${statusChip(status, label)}</div>
    <div class="row-actions">
      ${item.needs_repair ? `<button class="button secondary small" data-action="plaid-repair" data-id="${escapeHtml(item.item_id)}">Repair</button>` : ""}
      <button class="button ghost small" data-action="plaid-item-detail" data-id="${escapeHtml(item.item_id)}">Map accounts</button>
      ${sandbox ? `<button class="button ghost small" title="Sandbox only: make Plaid demand a fresh login" data-action="plaid-reset-login" data-id="${escapeHtml(item.item_id)}">Break login</button>` : ""}
      <button class="button danger small" data-action="plaid-remove-item" data-id="${escapeHtml(item.item_id)}" data-name="${escapeHtml(item.institution_name || "this bank")}">Remove</button>
    </div>
  </article>`;
}

function plaidPanel(plaid) {
  if (!plaid) return "";
  const configured = Boolean(plaid.configured);
  const sandbox = plaid.environment === "sandbox";
  const items = plaid.items || [];
  const slots = plaid.slots || {};
  const slotText = slots.limit ? `${slots.used} of ${slots.limit} lifetime production connections used` : `${plaid.environment} environment`;
  return `<article class="panel" id="plaid-panel">
    <header class="panel-head"><div><h2>Plaid connections</h2><p>${configured ? `Banks Clerk syncs itself and delivers into Actual · ${escapeHtml(slotText)}` : "Add a Plaid client id and secret in Settings to connect banks here."}${configured && !sandbox ? " · removing a connection does not give its slot back" : ""}</p></div>
      <div class="panel-actions">
        ${configured && sandbox ? `<button class="button ghost small" data-action="plaid-sandbox-item" title="Create a sandbox bank connection without the Link flow">Add sandbox bank</button>` : ""}
        ${configured ? `<button class="button primary small" data-action="plaid-connect">Connect a bank</button>` : `<a class="button ghost small" href="#settings-plaid">Configure Plaid</a>`}
      </div></header>
    <div class="status-list">${items.length
      ? items.map((item) => plaidItemRow(item, { sandbox })).join("")
      : emptyState("⇄", configured ? "No banks connected through Plaid yet" : "Plaid is not configured", configured ? "Connect a bank, then map each of its accounts onto an Actual account." : "Clerk keeps verifying SimpleFIN links either way.")}</div>
  </article>`;
}

async function renderAccounts() {
  const [accounts, plaid, phone] = await Promise.all([
    api("/api/accounts"),
    api("/api/plaid/items").catch((error) => ({ configured: true, items: [], error: error.message })),
    api("/api/anticipated").catch(() => null),
  ]);
  state.accounts = accounts;
  state.plaid = plaid;
  state.phone = phone;
  const health = state.accounts.health || [];
  const events = state.accounts.events || [];
  const linked = health.filter((item) => item.status !== "not_linked");
  const muted = health.filter((item) => item.status === "muted");
  const degraded = health.filter(isDegraded);
  const configured = state.data?.overview ? true : false;
  content.innerHTML = `
    <section class="page-intro"><div><h2>Bank connections</h2><p>Clerk asks each bank provider directly what an account looks like right now, then compares that with what Actual holds. A connection that quietly stops delivering data shows up here as a status change rather than as a slowly staler budget.</p></div><div class="actions"><button class="button ghost" data-action="check-connections">Check now</button><button class="button ghost" data-action="open-claim">Connect SimpleFIN</button>${plaid?.configured ? `<button class="button primary" data-action="plaid-connect">Connect a bank</button>` : ""}</div></section>
    ${degraded.length ? `<div class="alert-banner critical"><span>!</span><div><strong>${degraded.length} connection${degraded.length === 1 ? "" : "s"} need attention</strong>${connectionRemedies(degraded)}</div></div>` : ""}
    ${plaid?.error ? `<div class="alert-banner"><span>!</span><div><strong>Plaid could not be read</strong><p>${escapeHtml(plaid.error)}</p></div></div>` : ""}
    ${plaidPanel(plaid)}
    ${phonePanel(phone)}
    <article class="panel">
      <header class="panel-head"><div><h2>Accounts</h2><p>${linked.length} linked to bank sync, ${health.length - linked.length} manual${muted.length ? `, ${muted.length} not monitored` : ""} · the switch turns Clerk's watching on or off without unlinking anything in Actual</p></div></header>
      <div class="status-list">${health.length ? health.map((item) => healthRow(item, { toggle: true })).join("") : emptyState("⇄", "No accounts checked yet", configured ? "Run a connection check to populate this list." : "Connect Actual first in Settings.")}</div>
    </article>
    <article class="panel">
      <header class="panel-head"><div><h2>Status changes</h2><p>Every transition Clerk has recorded, newest first</p></div></header>
      <div class="status-list">${events.length ? events.map((event) => `<article class="data-row" style="grid-template-columns:30px minmax(0,1fr) auto">
        <span class="dot ${escapeHtml(event.status)}"></span>
        <div class="row-title"><strong>${escapeHtml(event.account_name)} · ${escapeHtml(titleCase(event.previous_status || "new"))} → ${escapeHtml(titleCase(event.status))}</strong><small>${escapeHtml((event.detail || "").slice(0, 120))}</small></div>
        <div class="row-meta">${relativeTime(event.created_at)}</div>
      </article>`).join("") : emptyState("✓", "No status changes recorded", "Clerk notes a line here whenever an account's connection health changes, and sends one notification for it.")}</div>
    </article>`;
}

function activityTimestamp(item) {
  const value = new Date(item.created_at || 0).getTime();
  return Number.isFinite(value) ? value : 0;
}

function filterDecisionRows() {
  const query = state.decisionSearch.toLocaleLowerCase();
  document.querySelectorAll("#decision-list .decision-row").forEach((row) => {
    row.hidden = !row.textContent.toLocaleLowerCase().includes(query);
  });
}

async function renderActivity({ poll = false, refreshDrawer = false } = {}) {
  const [fetchedJobs, fetchedDecisions] = await Promise.all([
    api("/api/jobs?limit=100"),
    api("/api/decisions?limit=150"),
  ]);
  if (state.route !== "activity") return;
  const fingerprint = JSON.stringify([
    fetchedJobs.map((job) => [job.id, job.status, job.phase, job.updated_at]),
    fetchedDecisions.map((decision) => [decision.id, decision.status, decision.resolved_at]),
  ]);
  state.jobs = fetchedJobs;
  state.decisions = fetchedDecisions;
  if (poll && fingerprint === state.activityFingerprint) return;
  state.activityFingerprint = fingerprint;

  const filters = ["all", "sync", "categorize", "health", "digest"];
  const filteredJobs = state.jobs.filter((job) => state.jobFilter === "all" || job.kind === state.jobFilter);
  const combined = [
    ...state.jobs.map((item) => ({ type: "job", item })),
    ...state.decisions.map((item) => ({ type: "decision", item })),
  ].sort((left, right) => activityTimestamp(right.item) - activityTimestamp(left.item));
  const activeElement = document.activeElement;
  const restoreSearchFocus = activeElement?.id === "decision-search";
  content.innerHTML = `
    <section class="page-intro"><div><h2>Activity</h2><p>Every task Clerk has performed and every filing decision it recorded, including the ones it withheld.</p></div><div class="actions"><button class="button ghost" data-action="run-health">Check connections</button><button class="button primary" data-action="sync-now">Sync now</button></div></section>
    <div class="toolbar panel" style="margin-bottom:18px"><div class="filter-tabs">
      <button class="${state.activityView === "all" ? "active" : ""}" data-action="activity-view" data-view="all">All (${combined.length})</button>
      <button class="${state.activityView === "decisions" ? "active" : ""}" data-action="activity-view" data-view="decisions">Filing decisions (${state.decisions.length})</button>
      <button class="${state.activityView === "tasks" ? "active" : ""}" data-action="activity-view" data-view="tasks">Tasks (${state.jobs.length})</button>
    </div></div>
    ${state.activityView === "all" ? `
    <article class="panel">
      <header class="panel-head"><div><h2>All activity</h2><p>Filing decisions and Clerk tasks together, newest first.</p></div><span class="muted" style="font-size:11px">${combined.length} item(s)</span></header>
      <div class="status-list">${combined.length ? combined.map(({ type, item }) => type === "job" ? jobRow(item) : decisionRow(item)).join("") : emptyState("↻", "No activity yet", "Clerk records tasks and filing decisions here as they happen.")}</div>
    </article>` : state.activityView === "decisions" ? `
    <article class="panel">
      <header class="panel-head"><div><h2>Filing decisions</h2><p>What Clerk applied, proposed, or withheld—and why.</p></div><span class="muted" style="font-size:11px">${state.decisions.length} decision(s)</span></header>
      <div class="toolbar"><input class="search-input" id="decision-search" type="search" value="${escapeHtml(state.decisionSearch)}" placeholder="Filter by merchant or category" /></div>
      <div class="status-list" id="decision-list">${state.decisions.length ? state.decisions.map(decisionRow).join("") : emptyState("✎", "No decisions yet", "Clerk records what it filed, what it withheld, and why, after the first categorization run.")}</div>
    </article>` : `
    <article class="panel">
      <header class="panel-head"><div><h2>Tasks</h2><p>Scheduled and manual work Clerk performed, newest first.</p></div></header>
      <div class="toolbar"><div class="filter-tabs">${filters.map((filter) => `<button class="${filter === state.jobFilter ? "active" : ""}" data-action="job-filter" data-filter="${filter}">${titleCase(filter)}</button>`).join("")}</div></div>
      <div class="status-list">${filteredJobs.length ? filteredJobs.map((job) => jobRow(job)).join("") : emptyState("↻", "No tasks yet", "Clerk records every scheduled and manual task here.")}</div>
    </article>`}`;
  if (state.activityView === "decisions" && state.decisionSearch) filterDecisionRows();
  if (restoreSearchFocus) {
    const search = document.querySelector("#decision-search");
    search?.focus();
    search?.setSelectionRange(search.value.length, search.value.length);
  }
  if (refreshDrawer && state.openJobId) await showJob(state.openJobId, { loading: false });
}

// ------------------------------------------------------------------ drawers

function openDrawer(html, { jobId = null } = {}) {
  state.openJobId = jobId;
  drawerContent.innerHTML = html;
  drawer.classList.add("open"); scrim.classList.add("visible"); drawer.setAttribute("aria-hidden", "false");
}
function closeDrawer() {
  state.openJobId = null;
  drawer.classList.remove("open"); scrim.classList.remove("visible"); drawer.setAttribute("aria-hidden", "true");
}
function drawerHeader(eyebrow, title) {
  return `<header class="drawer-head"><div><p class="eyebrow">${escapeHtml(eyebrow)}</p><h2>${escapeHtml(title)}</h2></div><button class="icon-button" data-action="close-drawer" aria-label="Close">×</button></header>`;
}

async function showDecision(id) {
  openDrawer(`${drawerHeader("Filing decision", "Loading…")}<div class="drawer-body"><div class="skeleton"></div></div>`);
  try {
    const item = await api(`/api/decisions/${id}`);
    const rationale = item.rationale || {};
    const memory = rationale.memory;
    const evidence = rationale.evidence || [];
    const actualUrl = (state.data?.actual_url || "").replace(/\/$/, "");
    openDrawer(`${drawerHeader(escapeHtml(item.account_name || "Transaction"), item.payee_name || item.merchant_key || "Transaction")}<div class="drawer-body">
      <section class="detail-section"><div class="detail-grid">
        <div class="detail-stat"><span>Amount</span><strong class="money">${money(item.amount_cents)}</strong></div>
        <div class="detail-stat"><span>Date</span><strong>${escapeHtml(item.transaction_date)}</strong></div>
        <div class="detail-stat"><span>Outcome</span><strong>${escapeHtml(titleCase(item.status))}</strong></div>
        <div class="detail-stat"><span>Decided by</span><strong>${escapeHtml(titleCase(item.source))}</strong></div>
        <div class="detail-stat"><span>Confidence</span><strong>${percent(item.confidence)}</strong></div>
        ${rationale.approval_required
          ? `<div class="detail-stat"><span>Approval required</span><strong>Yes</strong></div>`
          : `<div class="detail-stat"><span>Needed</span><strong>${percent(rationale.threshold || 0)}</strong></div>`}
      </div></section>
      <section class="detail-section"><h3>Category</h3><div class="change-list">
        <div class="change"><i>${item.category_id ? "✓" : "?"}</i><div><strong>${escapeHtml(item.category_name || item.proposed_category || "No category proposed")}</strong><small>${escapeHtml(rationale.reason || (memory ? "Chosen from this budget's own filing history." : "No explanation was recorded."))}</small></div></div>
        ${(item.tags || []).map((tag) => `<div class="change"><i>#</i><div><strong>#${escapeHtml(tag)}</strong><small>Written into the transaction notes in Actual.</small></div></div>`).join("")}
      </div></section>
      <div class="resolution-actions detail-section">
        ${actualUrl ? `<a class="button ghost" href="${escapeHtml(actualUrl)}" target="_blank" rel="noreferrer">Open Actual ↗</a>` : ""}
        ${item.merchant_key ? `<button class="button ghost" data-action="forget-merchant" data-key="${escapeHtml(item.merchant_key)}">Forget this merchant</button>` : ""}
        ${item.merchant_key && item.category_id && item.status !== "needs_review" && item.source !== "rule" ? `<button class="button secondary" data-action="rule-make" data-key="${escapeHtml(item.merchant_key)}" data-label="${escapeHtml(item.payee_name || "")}" data-category-id="${escapeHtml(item.category_id)}" title="Always file this merchant as ${escapeHtml(item.category_name)}">Make it a rule</button>` : ""}
        ${item.status === "needs_review" && item.merchant_key && item.category_id ? `<button class="button secondary" data-action="review-accept" data-always="1" data-id="${escapeHtml(item.id)}" title="Apply the proposal and make it a rule">Apply and always</button>` : ""}
        ${item.status === "needs_review" ? `<button class="button primary" data-action="review-accept" data-id="${escapeHtml(item.id)}">Apply proposal</button>` : ""}
      </div>
      ${item.observed && item.observed !== "standing" ? `<section class="detail-section"><h3>Seen in Actual afterwards</h3><div class="change-list"><div class="change"><i>${item.observed === "corrected" ? "✎" : "×"}</i><div><strong>${item.observed === "corrected" ? `Moved by hand to ${escapeHtml(item.observed_category_name || item.observed_category_id)}` : item.observed === "cleared" ? "Category cleared by hand" : "The transaction is gone or became a transfer"}</strong><small>Noticed ${escapeHtml(relativeTime(item.observed_at))}.${item.observed === "corrected" ? " Counted as a correction in memory" + (item.source === "rule" ? ", and as a dispute against the rule." : ".") : ""}</small></div></div></div></section>` : ""}
      ${rationale.rule ? `<section class="detail-section"><h3>Rule</h3><div class="change-list"><div class="change"><i>⚡</i><div><strong>Filed by a rule you set${rationale.rule.account_id ? " for this account" : ""}</strong><small>Matches “${escapeHtml(rationale.matched_key || rationale.rule.merchant_key)}”${rationale.rule.match === "family" ? " and its store variants" : ""}${rationale.alias ? `, reached through the alias “${escapeHtml(rationale.alias)}”` : ""}. <a href="#intelligence">Manage rules</a></small></div></div></div></section>` : ""}
      ${memory ? `<section class="detail-section"><h3>Memory evidence</h3><div class="change-list"><div class="change"><i>↺</i><div><strong>${memory.observations} prior sighting(s), ${percent(memory.share)} agreement</strong><small>Matched on “${escapeHtml(memory.matched_key)}”${memory.exact ? "" : " by merchant prefix"}. Confidence ${percent(memory.confidence)}.</small></div></div>${evidence.map((entry) => `<div class="change"><i>·</i><div><strong>${escapeHtml(entry.category_name || entry.category_id)}</strong><small>${percent(entry.share)} of this merchant's history · ${entry.sightings} sighting(s)</small></div></div>`).join("")}</div></section>` : ""}
      ${(rationale.examples || []).length ? `<section class="detail-section"><h3>Examples given to the model</h3><p class="muted" style="font-size:10px">${escapeHtml(rationale.examples.filter(Boolean).join(", "))}</p></section>` : ""}
      <section class="detail-section"><h3>Raw record</h3><pre class="code-block">${escapeHtml(JSON.stringify({ merchant_key: item.merchant_key, source: item.source, status: item.status, rationale }, null, 2))}</pre></section>
    </div>`);
  } catch (error) { toast("Could not load the decision", error.message, "error"); closeDrawer(); }
}

function showAccount(accountId) {
  const item = (state.accounts?.health || overview().health || []).find((entry) => entry.account_id === accountId);
  if (!item) return;
  openDrawer(`${drawerHeader(item.status_label || titleCase(item.status), item.account_name)}<div class="drawer-body">
    <section class="detail-section"><div class="detail-grid">
      <div class="detail-stat"><span>Actual balance</span><strong class="money">${money(item.actual_balance_cents)}</strong></div>
      <div class="detail-stat"><span>Of that, cleared</span><strong class="money">${money(item.actual_cleared_balance_cents ?? item.actual_balance_cents)}</strong></div>
      <div class="detail-stat"><span>Not yet cleared</span><strong class="money">${money(item.uncleared_balance_cents || 0)}</strong></div>
      ${item.transfer_adjusted ? `<div class="detail-stat"><span>Inferred transfer held out</span><strong class="money">${money(item.unconfirmed_transfer_cents || 0, { sign: true })}</strong></div>
      <div class="detail-stat"><span>Bank-comparable balance</span><strong class="money">${money(item.comparison_balance_cents)}</strong></div>` : ""}
      <div class="detail-stat"><span>Bank balance</span><strong class="money">${item.remote_balance_cents === null || item.remote_balance_cents === undefined ? "—" : money(item.remote_balance_cents)}</strong></div>
      <div class="detail-stat"><span>${item.transfer_adjusted ? "Compared vs bank" : "Cleared vs bank"}</span><strong class="money ${item.drift_cents ? "negative" : ""}">${item.drift_cents === null || item.drift_cents === undefined ? "—" : money(item.drift_cents, { sign: true })}</strong></div>
      <div class="detail-stat"><span>Bank data age</span><strong${item.remote_balance_date ? ` title="Bank balance dated ${escapeHtml(fullTime(item.remote_balance_date))}"` : ""}>${escapeHtml(balanceAge(item) || "—")}</strong></div>
      <div class="detail-stat"><span>Last transaction</span><strong>${escapeHtml(item.last_transaction_date || "—")}</strong></div>
      <div class="detail-stat"><span>Actual last sync</span><strong>${item.last_sync ? relativeTime(item.last_sync) : "—"}</strong></div>
    </div></section>
    <section class="detail-section"><h3>What Clerk sees</h3><ul class="note-list">${(item.signals || []).length ? item.signals.map((signal) => `<li>${escapeHtml(signal)}</li>`).join("") : `<li>Balances agree and data is current.</li>`}</ul></section>
    <section class="detail-section"><h3>Link</h3><div class="change-list">
      <div class="change"><i>⇄</i><div><strong>${escapeHtml(item.sync_source || "Not linked")}</strong><small>${escapeHtml(item.external_id ? `External account ${item.external_id}` : "Clerk cannot verify a manual account against a bank.")}</small></div></div>
      <div class="change"><i>${item.monitored === false ? "✗" : "✓"}</i><div><strong>${item.monitored === false ? "Monitoring is off" : "Monitoring is on"}</strong><small>${item.monitored === false ? `Clerk still reads this account but will not alert on it${item.underlying_status ? `. Unmonitored, it would currently read as ${escapeHtml(titleCase(item.underlying_status))}.` : "."}` : "Clerk scores this connection and alerts when its status changes."}</small></div></div>
      <div class="change"><i>◷</i><div><strong>Status since ${escapeHtml(fullTime(item.since))}</strong><small>Last checked ${relativeTime(item.checked_at)}.</small></div></div>
    </div></section>
    <section class="detail-section"><h3>Bank feed</h3><div class="change-list">
      ${feedDescription(item)}
    </div>
    <div class="resolution-actions" style="margin-top:10px">
      ${state.health?.configured?.plaid ? `<button class="button secondary small" data-action="migrate-open" data-direction="to-plaid" data-id="${escapeHtml(item.account_id)}">${item.managed_by_clerk ? "Change Plaid account…" : "Move to Plaid…"}</button>` : ""}
      ${item.managed_by_clerk || !item.sync_source ? `<button class="button ghost small" data-action="migrate-open" data-direction="to-simplefin" data-id="${escapeHtml(item.account_id)}">${item.actual_sync_source === "simpleFin" ? "Relink SimpleFIN…" : "Move to SimpleFIN…"}</button>` : ""}
    </div></section>
    <div class="resolution-actions"><button class="button primary" data-action="sync-and-recheck">Sync bank and recheck</button></div>
  </div>`);
}

function feedDescription(item) {
  const rows = [];
  if (item.managed_by_clerk) {
    rows.push(`<div class="change"><i>⇄</i><div><strong>Clerk delivers this account from ${escapeHtml(item.provider_label || item.sync_source)}</strong><small>${escapeHtml(item.institution || "")}${item.external_id ? ` · bank account ${escapeHtml(item.external_id.slice(0, 12))}…` : ""}. Manage the mapping on the Plaid connection.</small></div></div>`);
  }
  if (item.actual_sync_source) {
    rows.push(`<div class="change"><i>${item.managed_by_clerk ? "!" : "⇄"}</i><div><strong>Actual links it to ${escapeHtml(item.actual_sync_source === "simpleFin" ? "SimpleFIN" : item.actual_sync_source)}${item.managed_by_clerk ? " as well" : ""}</strong><small>${item.managed_by_clerk ? "Two feeds into one account will duplicate transactions once the cutover window passes. Move it to Plaid again with the SimpleFIN link removed, or move it back to SimpleFIN." : "Actual imports it itself; Clerk verifies the link and files what arrives."}</small></div></div>`);
  }
  if (!rows.length) rows.push(`<div class="change"><i>◌</i><div><strong>Manual account</strong><small>No bank feeds this account. Map it to a Plaid account, or link it to SimpleFIN, to have transactions delivered.</small></div></div>`);
  return rows.join("");
}

// ------------------------------------------------------------- migration

async function showMigration(accountId, direction) {
  openDrawer(`${drawerHeader("Bank feed", "Loading…")}<div class="drawer-body"><div class="skeleton"></div></div>`);
  let overview;
  try { overview = await api("/api/migration"); }
  catch (error) { return openDrawer(`${drawerHeader("Bank feed", "Could not read the feeds")}<div class="drawer-body"><p class="muted">${escapeHtml(error.message)}</p></div>`); }
  state.migration = overview;
  const account = (overview.accounts || []).find((entry) => entry.id === accountId);
  if (!account) return openDrawer(`${drawerHeader("Bank feed", "Account not found")}<div class="drawer-body"><p class="muted">Run a sync and try again.</p></div>`);
  const today = new Date().toISOString().slice(0, 10);
  let form;
  if (direction === "to-plaid") {
    const candidates = [];
    for (const item of overview.plaid?.items || []) {
      for (const plaidAccount of item.accounts || []) {
        if (plaidAccount.link && plaidAccount.link.enabled && plaidAccount.link.actual_account_id !== account.id) continue;
        candidates.push({ item, plaidAccount });
      }
    }
    form = `<div class="form-grid">
      <div class="field full"><label>Plaid account</label><select name="plaid_target">${candidates.length ? candidates.map(({ item, plaidAccount }) => `<option value="${escapeHtml(item.item_id)}|${escapeHtml(plaidAccount.id)}" ${account.link?.external_account_id === plaidAccount.id ? "selected" : ""}>${escapeHtml(item.institution_name || "Bank")} · ${escapeHtml(plaidAccountLabel(plaidAccount))}${plaidAccount.balance_cents !== null && plaidAccount.balance_cents !== undefined ? ` · ${money(plaidAccount.balance_cents)}` : ""}</option>`).join("") : `<option value="">No unmapped Plaid accounts — connect a bank first</option>`}</select></div>
      <div class="field"><label>Import from Plaid starting</label><input type="date" name="cutover_date" value="${escapeHtml(account.link?.cutover_date || today)}" /><small>Older history stays as it is in Actual.</small></div>
      <div class="field"><label>Actual's own link</label>${account.actual_sync_source ? `<label class="check-row" style="min-height:39px"><input type="checkbox" name="unlink_actual" checked /><span><strong>Unlink ${escapeHtml(account.actual_sync_source === "simpleFin" ? "SimpleFIN" : account.actual_sync_source)} in Actual</strong><small>Recommended: the account should be fed once. Nothing already imported is touched.</small></span></label>` : `<p class="muted" style="font-size:10px;min-height:39px;display:flex;align-items:center">Actual does not link this account itself.</p>`}</div>
    </div>`;
  } else {
    const remembered = account.link?.previous_external_id || account.actual_external_id || "";
    const simplefinAccounts = overview.simplefin_accounts || [];
    form = `<div class="form-grid">
      <div class="field full"><label>SimpleFIN account</label><select name="simplefin_target">${simplefinAccounts.length ? simplefinAccounts.map((remote) => `<option value="${escapeHtml(remote.account_id)}" ${remote.account_id === remembered ? "selected" : ""}>${escapeHtml(remote.institution || "")} · ${escapeHtml(remote.name)}${remote.balance ? ` · ${escapeHtml(remote.balance)}` : ""}</option>`).join("") : `<option value="">${overview.simplefin_server?.configured ? "SimpleFIN returned no accounts" : "The Actual server holds no SimpleFIN token — store one in Settings"}</option>`}</select><small>${remembered ? "Preselected: the account this one followed before." : "Pick the bank account this Actual account should follow."}</small></div>
      <div class="field"><label>Actual imports from SimpleFIN starting</label><input type="date" name="starting_date" value="${escapeHtml(account.link?.cutover_date || today)}" /><small>Rows Clerk delivered from Plaid in that window are matched by Actual, not duplicated.</small></div>
      <div class="field"><label>Plaid mapping</label><p class="muted" style="font-size:10px;min-height:39px;display:flex;align-items:center">${account.link?.enabled ? "Paused, so Clerk stops delivering from Plaid. It can be resumed from the Plaid connection." : "None to pause."}</p></div>
    </div>`;
  }
  openDrawer(`${drawerHeader(direction === "to-plaid" ? "Move to Plaid" : "Move to SimpleFIN", account.name)}<div class="drawer-body">
    <section class="detail-section"><div class="change-list">${feedDescription({ managed_by_clerk: Boolean(account.link?.enabled), provider_label: "Plaid", sync_source: account.link?.enabled ? "plaid" : account.actual_sync_source, actual_sync_source: account.actual_sync_source, institution: account.bank_name, external_id: account.link?.external_account_id || "" })}</div></section>
    <section class="detail-section migration-form" data-direction="${direction}" data-id="${escapeHtml(account.id)}">${form}
      <div class="resolution-actions" style="margin-top:12px">
        <button class="button ghost" data-action="migrate-preview" data-direction="${direction}" data-id="${escapeHtml(account.id)}">Preview</button>
        <button class="button primary" data-action="migrate-apply" data-direction="${direction}" data-id="${escapeHtml(account.id)}">${direction === "to-plaid" ? "Move to Plaid" : "Move to SimpleFIN"}</button>
      </div>
      <div id="migration-preview" style="margin-top:14px"></div>
    </section>
  </div>`);
}

function migrationBody(accountId, direction) {
  const form = document.querySelector(`.migration-form[data-id="${CSS.escape(accountId)}"]`);
  if (!form) return null;
  if (direction === "to-plaid") {
    const target = form.querySelector('[name="plaid_target"]').value;
    if (!target) return toast("Choose a Plaid account", "Connect a bank on the Connections page first.", "error") && null;
    const [itemId, externalId] = target.split("|");
    const unlink = form.querySelector('[name="unlink_actual"]');
    return { actual_account_id: accountId, item_id: itemId, external_account_id: externalId, cutover_date: form.querySelector('[name="cutover_date"]').value || "", unlink_actual: unlink ? unlink.checked : false };
  }
  return { actual_account_id: accountId, simplefin_account_id: form.querySelector('[name="simplefin_target"]').value || "", starting_date: form.querySelector('[name="starting_date"]').value || "" };
}

function renderMigrationPreview(direction, result) {
  const lines = [];
  if (direction === "to-plaid") {
    const p = result.preview || {};
    if (p.not_ready) lines.push("Plaid is still preparing this connection's history; the first delivery will wait for it.");
    lines.push(`${p.would_import ?? 0} transaction${p.would_import === 1 ? "" : "s"} would be imported${p.import_range ? ` (${escapeHtml(p.import_range[0])} to ${escapeHtml(p.import_range[1])})` : ""}${p.matched_by_actual ? `, and Actual would match ${p.matched_by_actual} more to rows it already holds` : ""}.`);
    if (p.counts?.skipped_before_cutover) lines.push(`${p.counts.skipped_before_cutover} older Plaid transaction${p.counts.skipped_before_cutover === 1 ? "" : "s"} stay out, dated before ${escapeHtml(p.cutover_date)}.`);
    if (p.adoptions?.length) lines.push(`${p.counts.adoptions} existing row${p.counts.adoptions === 1 ? "" : "s"} would be adopted rather than duplicated: ${p.adoptions.slice(0, 5).map((a) => `${escapeHtml(a.date)} ${money(a.amount_cents)} ${escapeHtml(a.payee_name || "")}`).join("; ")}${p.adoptions.length > 5 ? "; …" : ""}.`);
    if (p.deletions?.length) lines.push(`${p.deletions.length} withdrawn pending charge${p.deletions.length === 1 ? "" : "s"} would be removed.`);
    if (p.opening_balance_cents !== null && p.opening_balance_cents !== undefined) lines.push(`The account is empty, so an opening balance of ${money(p.opening_balance_cents)} would be added to match the bank's ${money(p.bank_balance_cents || 0)}.`);
    lines.push(result.will_unlink_actual ? "Actual's own link is removed first, so the account is fed once. Nothing already imported changes." : result.keeps_actual_link ? "Actual keeps its own link as well: both feeds will write to this account. Not recommended beyond the cutover window." : "Actual has no link of its own to remove.");
  } else {
    if (result.simplefin_account) lines.push(`Actual will follow ${escapeHtml(result.simplefin_account.institution || "")} · ${escapeHtml(result.simplefin_account.name)} from ${escapeHtml(result.starting_date)}.`);
    if (result.will_pause_plaid_link) lines.push("Clerk's Plaid mapping is paused; it can be resumed from the Plaid connection.");
    lines.push(result.note);
    for (const warning of result.warnings || []) lines.push(`⚠ ${escapeHtml(warning)}`);
  }
  document.querySelector("#migration-preview").innerHTML = `<div class="change-list">${lines.map((line) => `<div class="change"><i>→</i><div><small style="color:var(--ink-soft)">${line}</small></div></div>`).join("")}</div>`;
}

function plaidAccountLabel(account) {
  return `${account.name}${account.mask ? ` ••${account.mask}` : ""}`;
}

function plaidMappingForm(item, account) {
  const link = account.link;
  const today = new Date().toISOString().slice(0, 10);
  if (link) {
    return `<div class="plaid-map-form" data-account-id="${escapeHtml(link.actual_account_id)}">
      <div class="form-grid">
        <div class="field"><label>Import from</label><input type="date" name="cutover_date" value="${escapeHtml(link.cutover_date || "")}" /></div>
        <div class="field"><label>Mapping</label><div class="resolution-actions" style="min-height:39px">
          <button class="button ghost small" data-action="plaid-cutover-save" data-account-id="${escapeHtml(link.actual_account_id)}">Save date</button>
          <button class="button ghost small" data-action="plaid-link-toggle" data-account-id="${escapeHtml(link.actual_account_id)}" data-enabled="${link.enabled ? "0" : "1"}">${link.enabled ? "Pause" : "Resume"}</button>
          <button class="button danger small" data-action="plaid-unmap" data-account-id="${escapeHtml(link.actual_account_id)}">Unmap</button>
        </div></div>
      </div></div>`;
  }
  const choices = (state.plaid?.actual_accounts || []).filter((candidate) => !candidate.linked_to || candidate.linked_to.external_account_id === account.id);
  return `<div class="plaid-map-form" data-item-id="${escapeHtml(item.item_id)}" data-external-id="${escapeHtml(account.id)}">
    <div class="form-grid">
      <div class="field"><label>Actual account</label><select name="target" data-role="plaid-target">
        <option value="">Choose…</option>
        ${choices.map((candidate) => `<option value="${escapeHtml(candidate.id)}">${escapeHtml(candidate.name)}${candidate.actual_sync_source ? ` (Actual links it to ${escapeHtml(candidate.actual_sync_source === "simpleFin" ? "SimpleFIN" : candidate.actual_sync_source)})` : ""}${candidate.off_budget ? " · off budget" : ""}</option>`).join("")}
        <option value="__new__">Create a new Actual account…</option>
      </select></div>
      <div class="field"><label>Import from</label><input type="date" name="cutover_date" value="${today}" /><small>Plaid transactions dated before this stay out of Actual.</small></div>
      <div class="field full plaid-new-account" hidden><label>New account name</label><input name="new_name" value="${escapeHtml(plaidAccountLabel(account))}" maxlength="100" /><label class="check-row" style="margin-top:6px"><input type="checkbox" name="new_off_budget" /><span><strong>Off budget</strong></span></label></div>
      <div class="field full"><button class="button primary small" data-action="plaid-map" data-item-id="${escapeHtml(item.item_id)}" data-external-id="${escapeHtml(account.id)}">Map account</button></div>
    </div></div>`;
}

function showPlaidItem(itemId) {
  const item = (state.plaid?.items || []).find((entry) => entry.item_id === itemId);
  if (!item) return;
  const [status, label] = plaidItemStatus(item);
  const accounts = item.accounts || [];
  const sandbox = state.plaid?.environment === "sandbox";
  openDrawer(`${drawerHeader(label, item.institution_name || "Bank connection")}<div class="drawer-body">
    ${item.error ? `<div class="alert-banner critical"><span>!</span><div><strong>${escapeHtml(item.error.error_code || "Plaid error")}</strong><p>${escapeHtml(item.error.display_message || item.error.error_message || item.error.message || "")}</p></div>${item.needs_repair ? `<button class="button primary small" data-action="plaid-repair" data-id="${escapeHtml(item.item_id)}">Repair</button>` : ""}</div>` : ""}
    <section class="detail-section"><div class="detail-grid">
      <div class="detail-stat"><span>Status</span><strong>${statusChip(status, label)}</strong></div>
      <div class="detail-stat"><span>Bank data</span><strong>${item.last_successful_update ? escapeHtml(relativeTime(item.last_successful_update)) : "not yet"}</strong></div>
      <div class="detail-stat"><span>Connected</span><strong>${item.created_at ? escapeHtml(relativeTime(item.created_at)) : "—"}</strong></div>
      <div class="detail-stat"><span>Consent expires</span><strong>${item.consent_expiration_time ? escapeHtml(shortDate(item.consent_expiration_time)) : "—"}</strong></div>
      <div class="detail-stat"><span>Last delivery</span><strong>${item.last_sync_at ? escapeHtml(relativeTime(item.last_sync_at)) : "never"}</strong></div>
      <div class="detail-stat"><span>Last refresh asked</span><strong>${item.last_refresh_at ? escapeHtml(relativeTime(item.last_refresh_at)) : "never"}</strong></div>
    </div>${item.last_error && !item.error ? `<p class="muted" style="font-size:10px;margin:8px 0 0">Last problem: ${escapeHtml(item.last_error)}</p>` : ""}</section>
    <section class="detail-section"><h3>Accounts</h3><p class="muted" style="font-size:10px;margin:0 0 10px">Map each bank account onto the Actual account it should feed. Nothing is imported until a mapping exists, and only transactions dated on or after the import date are ever taken from Plaid.</p>
    <div class="change-list">${accounts.length ? accounts.map((account) => `<div class="change" style="display:block">
      <div style="display:flex;gap:10px;align-items:center"><i>◍</i><div style="min-width:0;flex:1"><strong>${escapeHtml(plaidAccountLabel(account))}<span class="muted"> · ${escapeHtml(account.subtype || account.type || "")}</span></strong><small>${account.balance_cents === null || account.balance_cents === undefined ? "No balance" : `Balance ${money(account.balance_cents)}`}${account.link ? ` · mapped to <strong>${escapeHtml((state.plaid?.actual_accounts || []).find((candidate) => candidate.id === account.link.actual_account_id)?.name || account.link.actual_account_id)}</strong>${account.link.enabled ? "" : " (paused)"}${account.link.cutover_date ? ` from ${escapeHtml(account.link.cutover_date)}` : ""}${account.link.last_import_at ? ` · delivered ${escapeHtml(relativeTime(account.link.last_import_at))}` : ""}${account.link.last_error ? ` · <span class="negative">${escapeHtml(account.link.last_error.slice(0, 80))}</span>` : ""}` : " · not mapped"}</small></div></div>
      <div style="margin-top:10px">${plaidMappingForm(item, account)}</div>
    </div>`).join("") : `<div class="change"><i>?</i><div><strong>No accounts returned</strong><small>${escapeHtml(item.error ? "Repair the connection, then check again." : "Plaid returned no accounts for this connection.")}</small></div></div>`}</div></section>
    <div class="resolution-actions">
      <button class="button primary" data-action="sync-and-recheck">Sync now</button>
      ${item.needs_repair ? `<button class="button secondary" data-action="plaid-repair" data-id="${escapeHtml(item.item_id)}">Repair connection</button>` : ""}
      ${sandbox ? `<button class="button ghost" data-action="plaid-reset-login" data-id="${escapeHtml(item.item_id)}">Break login (sandbox)</button>` : ""}
      <button class="button danger" data-action="plaid-remove-item" data-id="${escapeHtml(item.item_id)}" data-name="${escapeHtml(item.institution_name || "this bank")}">Remove connection</button>
    </div>
  </div>`);
}

async function refreshPlaidDrawer(itemId) {
  await renderAccounts();
  if (itemId) showPlaidItem(itemId);
}

// ------------------------------------------------------------- Plaid Link
//
// Link runs in the browser against cdn.plaid.com. Clerk mints the token, the
// browser hands Plaid's one-time public token straight back to Clerk, and only
// Clerk ever holds the resulting access token. OAuth banks bounce through a
// registered redirect URI; the token is kept in sessionStorage so Link can
// resume with the same token when the browser comes back.
const PLAID_LINK_STORAGE = "actual-clerk-plaid-link";

async function startPlaidLink({ itemId = "" } = {}) {
  if (!window.Plaid) return toast("Plaid Link did not load", "This page needs to reach cdn.plaid.com to open Link.", "error");
  try {
    const token = await api("/api/plaid/link-token", { method: "POST", body: JSON.stringify({ item_id: itemId }) });
    try { sessionStorage.setItem(PLAID_LINK_STORAGE, JSON.stringify({ link_token: token.link_token, item_id: itemId })); } catch { /* no resume after OAuth */ }
    openPlaidLink(token.link_token, itemId);
  } catch (error) { toast("Could not open Plaid Link", error.message, "error"); }
}

function openPlaidLink(linkToken, itemId, receivedRedirectUri = "") {
  const handler = window.Plaid.create({
    token: linkToken,
    ...(receivedRedirectUri ? { receivedRedirectUri } : {}),
    onSuccess: async (publicToken, metadata) => {
      try { sessionStorage.removeItem(PLAID_LINK_STORAGE); } catch { /* fine */ }
      try {
        if (itemId) {
          // Update mode: the Item keeps its access token, so the public token is not exchanged.
          const result = await api(`/api/plaid/items/${encodeURIComponent(itemId)}/repaired`, { method: "POST" });
          toast(result.repaired ? "Connection repaired" : "Connection still needs attention", result.repaired ? "Clerk is checking the accounts again." : (result.error?.error_message || ""), result.repaired ? "success" : "error");
          await refreshPlaidDrawer(itemId);
        } else {
          const result = await api("/api/plaid/exchange", { method: "POST", body: JSON.stringify({
            public_token: publicToken,
            institution_id: metadata?.institution?.institution_id || "",
            institution_name: metadata?.institution?.name || "",
            accounts: (metadata?.accounts || []).map((account) => ({ id: account.id || "", name: account.name || "", mask: account.mask || "", type: account.type || "", subtype: account.subtype || "" })),
          }) });
          toast("Bank connected", "Now map its accounts onto Actual accounts.");
          await refreshPlaidDrawer(result.item.item_id);
        }
      } catch (error) { toast("Could not finish connecting", error.message, "error"); }
    },
    onExit: (error) => {
      try { sessionStorage.removeItem(PLAID_LINK_STORAGE); } catch { /* fine */ }
      if (error) toast("Plaid Link closed", error.display_message || error.error_message || error.error_code || "The connection was not completed.", "error");
    },
  });
  handler.open();
}

async function resumePlaidLinkAfterOAuth() {
  const params = new URLSearchParams(location.search);
  if (!params.has("oauth_state_id")) return false;
  let saved = null;
  try { saved = JSON.parse(sessionStorage.getItem(PLAID_LINK_STORAGE) || "null"); } catch { saved = null; }
  const redirectUri = location.href;
  history.replaceState(null, "", "/#accounts");
  if (!saved?.link_token || !window.Plaid) {
    toast("Could not resume Plaid Link", "Start the connection again from this browser.", "error");
    return false;
  }
  state.route = "accounts";
  await renderRoute();
  openPlaidLink(saved.link_token, saved.item_id || "", redirectUri);
  return true;
}

async function showJob(id, { loading = true } = {}) {
  if (loading) openDrawer(`${drawerHeader("Task detail", "Loading…")}<div class="drawer-body"><div class="skeleton"></div></div>`, { jobId: id });
  try {
    const job = await api(`/api/jobs/${id}`);
    if (state.openJobId !== id) return;
    openDrawer(`${drawerHeader(titleCase(job.kind), jobSummary(job))}<div class="drawer-body">
      <section class="detail-section"><div class="detail-grid">
        <div class="detail-stat"><span>Status</span><strong>${escapeHtml(titleCase(job.status))}</strong></div>
        <div class="detail-stat"><span>Trigger</span><strong>${escapeHtml(titleCase(job.trigger))}</strong></div>
        <div class="detail-stat"><span>Attempt</span><strong>${job.attempt} / ${job.max_attempts}</strong></div>
        <div class="detail-stat"><span>Phase</span><strong>${escapeHtml(titleCase(job.phase))}</strong></div>
        <div class="detail-stat"><span>Started</span><strong>${relativeTime(job.started_at)}</strong></div>
        <div class="detail-stat"><span>Finished</span><strong>${relativeTime(job.completed_at)}</strong></div>
      </div></section>
      ${job.error_message ? `<section class="detail-section"><h3>Last error</h3><div class="resolution-box"><p class="danger-text">${escapeHtml(job.error_message)}</p></div></section>` : ""}
      <section class="detail-section"><h3>Result</h3><pre class="code-block">${escapeHtml(JSON.stringify(job.result || {}, null, 2))}</pre></section>
      <section class="detail-section"><h3>Timeline</h3><div class="event-list">${(job.events || []).length ? job.events.map((event) => `<div class="event ${escapeHtml(event.level)}"><strong>${escapeHtml(titleCase(event.event_type))}</strong><span>${fullTime(event.created_at)}</span><p>${escapeHtml(event.message)}</p></div>`).join("") : `<p class="muted">No events retained.</p>`}</div></section>
      <div class="resolution-actions">${["failed", "cancelled"].includes(job.status) ? `<button class="button primary" data-action="retry-job" data-id="${escapeHtml(job.id)}">Retry task</button>` : ""}</div>
    </div>`, { jobId: id });
  } catch (error) {
    if (state.openJobId !== id) return;
    toast("Could not load the task", error.message, "error"); closeDrawer();
  }
}

// ------------------------------------------------------------- diagnostics

// The report is one block of text on purpose: it is meant to be copied whole
// into a bug report, where a screenshot of a dashboard explains nothing.
async function runDiagnostics(redact = false) {
  state.diagnosticsRedact = redact;
  openDrawer(`${drawerHeader("Diagnostics", "Reading Actual…")}<div class="drawer-body"><p class="muted">Reading the budget live and comparing it with the stored overview. This takes a few seconds.</p><div class="skeleton"></div></div>`);
  try {
    const result = await api(`/api/diagnostics?redact=${redact ? "true" : "false"}`);
    state.diagnosticsReport = result.report;
    openDrawer(`${drawerHeader("Diagnostics", "Report")}<div class="drawer-body">
      <section class="detail-section">
        <p class="muted">Findings are listed at the top. Copy the whole report if you are asking someone for help with it.</p>
        <div class="resolution-actions">
          <button class="button primary" type="button" data-action="copy-diagnostics">Copy report</button>
          <button class="button ghost" type="button" data-action="rerun-diagnostics">Run again</button>
          <button class="button ghost" type="button" data-action="toggle-diagnostics-redact">${redact ? "Show real names" : "Hide names"}</button>
        </div>
        <p class="muted">${redact ? "Account, category, and group names are replaced with stable labels such as “Group 3”. Amounts and structure are kept, since those are what the report is for." : "Real names are shown. Use “Hide names” before sharing this report with anyone."}</p>
      </section>
      <section class="detail-section"><pre class="code-block diagnostic-output" id="diagnostic-output">${escapeHtml(result.report)}</pre></section>
    </div>`);
  } catch (error) {
    toast("Could not run diagnostics", error.message, "error");
    closeDrawer();
  }
}

async function copyDiagnostics() {
  const text = state.diagnosticsReport || "";
  if (!text) return;
  try {
    // Only available on a secure origin, which a self-hosted Clerk often is not.
    await navigator.clipboard.writeText(text);
    toast("Copied", "The whole report is on your clipboard.");
    return;
  } catch { /* fall through to the selection fallback */ }
  const output = document.querySelector("#diagnostic-output");
  if (output && window.getSelection) {
    const range = document.createRange();
    range.selectNodeContents(output);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    toast("Select and copy", "This browser blocked clipboard access, so the report is selected for you.", "error");
  }
}

// ----------------------------------------------------------------- settings

function settingInput(name, label, value, options = {}) {
  const type = options.type || "text";
  const configured = options.configured;
  const locked = Boolean(state.settings?.environment_overrides?.includes(name));
  const inputId = `setting-${name}`;
  const noteText = locked ? "Managed by an environment variable; remove it and restart to edit here." : options.note;
  const note = noteText ? `<small class="${locked ? "environment-note" : ""}">${escapeHtml(noteText)}</small>` : "";
  if (type === "select") {
    return `<div class="field ${options.full ? "full" : ""} ${locked ? "locked" : ""}"><label for="${inputId}">${escapeHtml(label)}</label><select id="${inputId}" name="${name}" ${locked ? "disabled" : ""}>${options.choices.map(([key, text]) => `<option value="${escapeHtml(key)}" ${value === key ? "selected" : ""}>${escapeHtml(text)}</option>`).join("")}</select>${note}</div>`;
  }
  const clearSecret = type === "password" && configured && !locked
    ? `<label class="clear-secret" for="clear-${name}"><input id="clear-${name}" type="checkbox" name="clear_${name}" /> Clear saved secret</label>` : "";
  return `<div class="field ${options.full ? "full" : ""} ${locked ? "locked" : ""}"><label for="${inputId}">${escapeHtml(label)}</label><div class="${configured !== undefined ? "input-with-status" : ""}"><input id="${inputId}" name="${name}" type="${type}" value="${type === "password" ? "" : escapeHtml(value ?? "")}" ${options.min !== undefined ? `min="${options.min}"` : ""} ${options.max !== undefined ? `max="${options.max}"` : ""} ${type === "number" ? `step="${options.step || "any"}"` : ""} ${type === "password" ? `placeholder="${configured ? "Leave blank to keep saved secret" : "Enter secret"}" autocomplete="new-password"` : ""} ${locked ? "disabled" : ""} />${configured !== undefined ? `<span>${configured ? "configured" : "not set"}</span>` : ""}</div>${clearSecret}${note}</div>`;
}

function settingCheck(name, title, description, checked) {
  const locked = Boolean(state.settings?.environment_overrides?.includes(name));
  const note = locked ? "Managed by an environment variable; remove it and restart to edit here." : description;
  return `<label class="check-row ${locked ? "locked" : ""}"><input type="checkbox" name="${name}" ${checked ? "checked" : ""} ${locked ? "disabled" : ""} /><span><strong>${escapeHtml(title)}</strong><small class="${locked ? "environment-note" : ""}">${escapeHtml(note)}</small></span></label>`;
}

function settingToggle(name, title, description, checked) {
  const locked = Boolean(state.settings?.environment_overrides?.includes(name));
  const note = locked ? "Managed by an environment variable; remove it and restart to edit here." : description;
  return `<label class="toggle-row ${locked ? "locked" : ""}"><span><strong>${escapeHtml(title)}</strong><small class="${locked ? "environment-note" : ""}">${escapeHtml(note)}</small></span><span class="toggle-control"><input type="checkbox" name="${name}" ${checked ? "checked" : ""} ${locked ? "disabled" : ""} /><i aria-hidden="true"></i></span></label>`;
}

async function renderSettings() {
  const [loadedSettings, server] = await Promise.all([
    api("/api/settings"),
    api("/api/simplefin/server").catch((error) => ({ configured: false, error: error.message, unreachable: true })),
  ]);
  state.settings = loadedSettings;
  state.simplefinServer = server;
  const s = state.settings;
  applyAppearance(s);
  content.innerHTML = `<section class="page-intro"><div><h2>Settings</h2><p>Connect Actual, a bank provider, and a local model, then decide how much Clerk should do on its own. Save changed values before testing a connection.</p></div></section>
    <div class="settings-layout">
      <nav class="settings-nav">
        <a href="#settings-actual">Actual Budget</a>
        <a href="#settings-simplefin">SimpleFIN</a>
        <a href="#settings-plaid">Plaid</a>
        <a href="#settings-phone">Phone app</a>
        <a href="#settings-model">Local model</a>
        <a href="#settings-filing">Filing</a>
        <a href="#settings-tags">Tags</a>
        <a href="#settings-sync">Sync</a>
        <a href="#settings-digest">Morning report</a>
        <a href="#settings-notifications">Notifications</a>
        <a href="#settings-appearance">Appearance</a>
        <a href="#settings-advanced">Limits &amp; reliability</a>
      </nav>
      <form class="settings-form" id="settings-form">
        <section class="panel settings-section" id="settings-actual"><header class="panel-head"><div><h3>Actual Budget</h3><p class="section-description">Clerk treats Actual as the system of record and writes back through its sync protocol.</p></div><button class="button ghost small" type="button" data-action="test-connection" data-target="actual">Test connection</button></header><div class="panel-body"><div class="form-grid">
          ${settingInput("actual_url", "Server URL", s.actual_url, { full: true, note: "The Actual server, for example http://actual_server:5006." })}
          ${settingInput("actual_password", "Server password", "", { type: "password", configured: s.actual_password_configured })}
          ${settingInput("actual_budget_id", "Budget sync ID", s.actual_budget_id, { note: "Actual: Settings → Advanced → Sync ID." })}
          ${settingInput("actual_encryption_password", "End-to-end encryption password", "", { type: "password", configured: s.actual_encryption_password_configured, full: true, note: "Only needed if your budget file is encrypted." })}
        </div>${settingCheck("actual_verify_ssl", "Verify TLS certificates", "Disable only for a trusted local server with a self-signed certificate.", s.actual_verify_ssl)}</div></section>

        <section class="panel settings-section" id="settings-simplefin"><header class="panel-head"><div><h3>SimpleFIN</h3><p class="section-description">Read-only access used to verify that each bank link is still alive. Actual keeps doing the importing.</p></div><button class="button ghost small" type="button" data-action="test-connection" data-target="simplefin">Test connection</button></header><div class="panel-body"><div class="form-grid">
          ${settingInput("simplefin_access_url", "Access URL", "", { type: "password", configured: s.simplefin_access_url_configured, full: true, note: "Claim a setup token below, or paste an access URL you already hold." })}
        </div><div class="section-actions"><button class="button secondary small" type="button" data-action="open-claim">Claim a setup token</button><span class="muted" style="font-size:10px">A setup token can only be claimed once. Claiming here does not affect the token Actual already uses.</span></div>
        <div class="change-list" style="margin-top:14px"><div class="change"><i>${server.configured ? "✓" : "○"}</i><div style="flex:1"><strong>Actual server token: ${server.unreachable ? "unknown" : server.configured ? "stored" : "not stored"}</strong><small>${escapeHtml(server.error ? server.error : server.configured ? "The Actual server imports SimpleFIN accounts with this token. Managing it here is the same as Actual's own Settings, and needs an admin login on the server." : "Actual cannot link or sync SimpleFIN accounts without a token on the server. Store one here when moving an account back to SimpleFIN.")}</small></div><div class="row-actions"><button class="button ghost small" type="button" data-action="simplefin-server-token">${server.configured ? "Replace" : "Store token"}</button>${server.configured ? `<button class="button danger small" type="button" data-action="simplefin-server-clear">Remove</button>` : ""}</div></div></div></div></section>

        <section class="panel settings-section" id="settings-plaid"><header class="panel-head"><div><h3>Plaid</h3><p class="section-description">The bank feed Clerk delivers itself. Connect banks on the Connections page once the keys are saved.</p></div><button class="button ghost small" type="button" data-action="test-connection" data-target="plaid">Test credentials</button></header><div class="panel-body"><div class="form-grid">
          ${settingInput("plaid_client_id", "Client ID", s.plaid_client_id, { note: "Plaid dashboard → Developers → Keys." })}
          ${settingInput("plaid_secret", "Secret", "", { type: "password", configured: s.plaid_secret_configured, note: "Specific to the environment below." })}
          ${settingInput("plaid_env", "Environment", s.plaid_env, { type: "select", choices: [["sandbox", "Sandbox — fake banks, unlimited"], ["production", "Production — real banks, ten lifetime connections on the Trial plan"]], note: "Changing this needs the matching secret; existing connections belong to the environment they were made in." })}
          ${settingInput("plaid_days_requested", "History to request (days)", s.plaid_days_requested, { type: "number", min: 1, max: 730, note: "Asked for when a bank is first connected. Clerk still imports only from each account's import date." })}
          ${settingInput("plaid_redirect_uri", "OAuth redirect URI", s.plaid_redirect_uri, { full: true, note: "Only for OAuth banks: an https URL registered in the Plaid dashboard that brings the browser back to Clerk, e.g. https://clerk.example.net/plaid-oauth" })}
          ${settingInput("plaid_client_name", "Name shown in Link", s.plaid_client_name, { full: true })}
        </div>
        ${settingToggle("plaid_sync_enabled", "Deliver Plaid transactions into Actual", "Every sync reads each connection's change stream and imports it through Actual's own reconciliation and rules. Off, Clerk still checks connection health.", s.plaid_sync_enabled)}
        ${settingToggle("plaid_refresh_enabled", "Ask Plaid to refresh before reading", "Requests an on-demand extraction so a sync sees today's transactions instead of Plaid's one-to-four-times-a-day schedule. Included in the Trial plan; rate limited per connection.", s.plaid_refresh_enabled)}
        <div class="form-grid">
          ${settingInput("plaid_refresh_min_interval_minutes", "Refresh at most every (minutes)", s.plaid_refresh_min_interval_minutes, { type: "number", min: 1, max: 1440 })}
          ${settingInput("plaid_refresh_wait_seconds", "Wait for the refresh up to (seconds)", s.plaid_refresh_wait_seconds, { type: "number", min: 0, max: 300, note: "Clerk polls the connection for a newer update, then reads whatever is there." })}
          ${settingInput("plaid_adopt_window_days", "Cutover adoption window (days)", s.plaid_adopt_window_days, { type: "number", min: 0, max: 90, note: "How far either side of an account's import date a row from the previous provider may be adopted instead of duplicated." })}
        </div>
        <div class="check-grid">
          ${settingCheck("plaid_delete_removed_pending", "Delete pending charges the bank withdraws", "Only while the row is still uncleared and unreconciled in Actual. A cleared row is never deleted; it is reported instead.", s.plaid_delete_removed_pending)}
          ${settingCheck("plaid_starting_balance", "Add an opening balance to a new account", "On the first import into an empty Actual account, so it matches the bank the way Actual's own linking does.", s.plaid_starting_balance)}
        </div></div></section>

        <section class="panel settings-section" id="settings-phone"><header class="panel-head"><div><h3>Phone app</h3><p class="section-description">The Actual Clerk phone app forwards a card app's notifications, so a charge counts as spent the moment it is made rather than when the bank posts it. Anticipated charges are never written into Actual.</p></div></header><div class="panel-body">
        ${settingToggle("anticipated_enabled", "Count charges the phone has seen", "Off, the phone is refused and nothing anticipated counts; the ledger is kept.", s.anticipated_enabled)}
        <div class="form-grid">
          ${settingInput("anticipated_device_token", "Device token", "", { type: "password", configured: s.anticipated_device_token_configured, full: true, note: "Any string you choose; enter the same one in the phone app. Clerk has no login of its own, and these are the only endpoints an outside device writes to, so leaving this blank means any device that can reach Clerk may forward notifications." })}
          ${settingInput("anticipated_match_window_days", "Match window (days)", s.anticipated_match_window_days, { type: "number", min: 1, max: 60, note: "How long after the notification the bank's transaction may still be dated and be recognised as the same charge." })}
          ${settingInput("anticipated_expire_days", "Stop counting after (days)", s.anticipated_expire_days, { type: "number", min: 1, max: 90, note: "An anticipation nothing has settled by then is evidently never going to post. Must be at least the match window." })}
        </div>
        <p class="muted" style="font-size:12px;margin-top:10px">Build the phone app with <code>scripts/build-android.sh</code>; the APK lands in <code>dist/android/</code>. Register sources on the Connections page or from the phone.</p>
        </div></section>

        <section class="panel settings-section" id="settings-model"><header class="panel-head"><div><h3>Local model</h3><p class="section-description">An OpenAI-compatible endpoint, asked one question per unfamiliar merchant.</p></div><button class="button ghost small" type="button" data-action="test-connection" data-target="model">Test model</button></header><div class="panel-body"><div class="form-grid">
          ${settingInput("openai_base_url", "Base URL", s.openai_base_url, { full: true, note: "Usually ends in /v1; Clerk appends /chat/completions." })}
          ${settingInput("openai_api_key", "API key", "", { type: "password", configured: s.openai_api_key_configured, full: true })}
          ${settingInput("model", "Model name", s.model, { full: true })}
          ${settingInput("model_reasoning", "Reasoning effort", s.model_reasoning, { type: "select", full: true, choices: [["", "Server default"], ["off", "Off — answer without thinking"], ["low", "Low"], ["medium", "Medium"], ["high", "High"]], note: "Clerk asks bounded questions about records it supplies, so thinking costs output tokens and wall clock without adding much. Sent as reasoning_effort, or as a chat-template argument for Off; a server that does not recognise it is asked without it for the rest of the run." })}
          ${settingInput("model_context_tokens", "Context limit", s.model_context_tokens, { type: "number", min: 2048 })}
          ${settingInput("model_max_output_tokens", "Maximum output tokens", s.model_max_output_tokens, { type: "number", min: 256 })}
        </div></div></section>

        <section class="panel settings-section" id="settings-filing"><header class="panel-head"><div><h3>Filing</h3><p class="section-description">How Clerk decides, and how much it does without asking.</p></div></header><div class="panel-body">
          ${settingToggle("categorization_enabled", "File uncategorized transactions", "Runs after every sync. Transactions you have already categorized are never touched.", s.categorization_enabled)}
          ${settingToggle("ai_enabled", "Ask the local model about new merchants", "With this off, Clerk still files merchants it recognizes and queues the rest for review.", s.ai_enabled)}
          ${settingToggle("ai_alias_questions", "Also ask whether a new merchant is a known one by another name", "One more model call per unfamiliar merchant. A yes becomes an alias proposal under Intelligence, never an alias.", s.ai_alias_questions)}
          <div class="form-grid">
            ${settingInput("apply_mode", "For merchants Clerk already knows", s.apply_mode, { type: "select", full: true, choices: [["automatic", "Apply reliable history in Actual"], ["review", "Propose every category for review"]], note: "First-time merchants always require approval, regardless of this setting." })}
            ${settingInput("memory_min_confidence", "Confidence needed from memory", s.memory_min_confidence, { type: "number", min: 0, max: 1, step: 0.01, note: "Evidence from your own filing history." })}
            ${settingInput("memory_min_observations", "Sightings needed from memory", s.memory_min_observations, { type: "number", min: 1, max: 50 })}
            ${settingInput("categorize_lookback_days", "Only file transactions newer than (days)", s.categorize_lookback_days, { type: "number", min: 1, max: 730 })}
            ${settingInput("ai_example_count", "Examples shown to the model", s.ai_example_count, { type: "number", min: 0, max: 40, note: "Your own filed transactions, which is what makes the model match your habits." })}
            ${settingInput("category_candidate_limit", "Categories offered to the model", s.category_candidate_limit, { type: "number", min: 10, max: 400 })}
          </div>
          ${settingToggle("rule_promotion_enabled", "Propose rules for settled merchants", "After the same merchant is filed the same way a few times, Clerk asks under Intelligence whether to make it a rule. Only you can say yes.", s.rule_promotion_enabled)}
          <div class="form-grid">${settingInput("rule_promote_after", "Consistent decisions before proposing a rule", s.rule_promote_after, { type: "number", min: 2, max: 25, full: true })}</div>
          ${settingToggle("memory_learn_from_actual", "Learn from corrections made in Actual", "Every filing run compares what Clerk applied with what the transaction holds now. A category you changed by hand counts as a correction, and a correction to a rule's decision counts as a dispute against that rule.", s.memory_learn_from_actual)}
          <div class="form-grid">${settingInput("memory_dispute_threshold", "Disputes before Clerk asks to change a rule", s.memory_dispute_threshold, { type: "number", min: 1, max: 20, full: true })}</div>
        </div></section>

        <section class="panel settings-section" id="settings-tags"><header class="panel-head"><div><h3>Tags</h3><p class="section-description">Written into transaction notes as #tags, which is how Actual stores them.</p></div></header><div class="panel-body">
          ${settingToggle("tagging_enabled", "Tag transactions Clerk files", "Adds descriptive tags alongside the category.", s.tagging_enabled)}
          <div class="check-grid">
            ${settingCheck("tag_provenance", "Mark Clerk's own work", "Adds your Clerk tag so you can find, review, or undo everything Clerk touched from inside Actual.", s.tag_provenance)}
            ${settingCheck("tag_anomalies", "Tag refunds and outsized charges", "#refund for money coming back, #unusual for a charge far above a merchant's normal size.", s.tag_anomalies)}
          </div>
          <div class="form-grid">${settingInput("clerk_tag", "Clerk tag", s.clerk_tag, { full: true, note: "Written without the leading #." })}</div>
        </div></section>

        <section class="panel settings-section" id="settings-sync"><header class="panel-head"><div><h3>Sync</h3><p class="section-description">How often Clerk pulls from Actual and checks your bank connections.</p></div></header><div class="panel-body">
          ${settingToggle("sync_enabled", "Sync on a schedule", "Pulls the budget, asks Actual to run bank sync, then files what arrived.", s.sync_enabled)}
          ${settingToggle("bank_sync_enabled", "Run Actual's bank sync", "Turn this off if something else already triggers bank sync on a schedule.", s.bank_sync_enabled)}
          <div class="form-grid">
            ${settingInput("sync_interval_minutes", "Sync every (minutes)", s.sync_interval_minutes, { type: "number", min: 5, max: 1440 })}
            ${settingInput("health_interval_minutes", "Check connections every (minutes)", s.health_interval_minutes, { type: "number", min: 5, max: 1440 })}
          </div>
        </div></section>

        <section class="panel settings-section" id="settings-digest"><header class="panel-head"><div><h3>Morning report</h3><p class="section-description">One notification a day. The header stays the same; everything below goes in the body.</p></div><button class="button ghost small" type="button" data-action="send-digest">Send report now</button></header><div class="panel-body">
          ${settingToggle("digest_enabled", "Send a morning report", "Delivered through ntfy at the time below. One per day, unless you move the time.", s.digest_enabled)}
          <div class="form-grid">
            ${settingInput("digest_title", "Notification header", s.digest_title, { full: true, note: "Shown as the notification title every morning, so it is recognisable at a glance." })}
            ${settingInput("digest_time", "Delivery time", s.digest_time, { note: "24-hour local time, for example 07:30." })}
            ${settingInput("timezone", "Time zone", s.timezone, { note: "An IANA name such as America/New_York." })}
          </div>
          <h4 class="field-group-title">What the report includes</h4>
          <div class="check-grid">
            ${settingCheck("digest_show_headline", "Free money left", "The headline figure and how much of the month's free money remains.", s.digest_show_headline)}
            ${settingCheck("digest_show_spending", "Spent so far", "How much has gone out since the 1st, against what was free to spend.", s.digest_show_spending)}
            ${settingCheck("digest_show_safe_to_spend", "Safe to spend a day", "What you can spend daily and still finish the month level.", s.digest_show_safe_to_spend)}
            ${settingCheck("digest_show_pace", "Pace for the month", "Whether you are ahead of or behind an even spend across the month.", s.digest_show_pace)}
            ${settingCheck("digest_show_projection", "Projected month end", "Where this month lands if the current pace holds.", s.digest_show_projection)}
            ${settingCheck("digest_show_commitments", "Committed overspend", "Named when a bill or subscription has gone past what you budgeted.", s.digest_show_commitments)}
            ${settingCheck("digest_show_balances", "Account balances", "The current Actual balance for every bank-linked account with monitoring on. One switch controls the whole list.", s.digest_show_balances)}
            ${settingCheck("digest_show_connections", "Bank connections", "Lists connections needing attention. A broken connection still raises the alert priority either way.", s.digest_show_connections)}
            ${settingCheck("digest_show_attention", "Waiting for you", "Transactions to review and anything still uncategorized this month.", s.digest_show_attention)}
          </div>
        </div></section>

        <section class="panel settings-section" id="settings-notifications"><header class="panel-head"><div><h3>Notifications</h3><p class="section-description">ntfy delivers the morning digest and connection alerts.</p></div><button class="button ghost small" type="button" data-action="test-connection" data-target="notifications">Send test</button></header><div class="panel-body">
          ${settingToggle("notifications_enabled", "Enable ntfy notifications", "Without this, Clerk still builds the digest and shows it on the overview.", s.notifications_enabled)}
          ${settingToggle("health_alerts_enabled", "Alert when a bank connection changes", "One message when a connection breaks and one when it recovers, never a repeat every hour.", s.health_alerts_enabled)}
          <div class="form-grid">
            ${settingInput("ntfy_url", "ntfy server URL", s.ntfy_url, { full: true, note: "Use https://ntfy.sh unless you run your own." })}
            ${settingInput("ntfy_topic", "Topic", s.ntfy_topic, { full: true, note: "Pick something hard to guess: anyone who knows the topic can read it." })}
            ${settingInput("ntfy_token", "Access token (optional)", "", { type: "password", configured: s.ntfy_token_configured, full: true })}
          </div>
        </div></section>

        <section class="panel settings-section" id="settings-appearance"><header class="panel-head"><div><h3>Appearance</h3><p class="section-description">Personalizes this browser without changing anything in Actual.</p></div></header><div class="panel-body"><div class="form-grid">
          ${settingInput("budget_currency", "Currency", s.budget_currency, { note: "Three-letter code, used for display only." })}
          ${settingInput("appearance_theme", "Color theme", s.appearance_theme, { type: "select", choices: [["system", "Follow system"], ["light", "Light"], ["dark", "Dark"]] })}
          ${settingInput("appearance_density", "Interface density", s.appearance_density, { type: "select", choices: [["comfortable", "Comfortable"], ["compact", "Compact"]] })}
          ${settingInput("appearance_motion", "Animation and motion", s.appearance_motion, { type: "select", full: true, choices: [["system", "Follow system"], ["full", "Full motion"], ["reduced", "Reduced motion"]] })}
        </div></div></section>

        <section class="panel settings-section" id="settings-advanced"><header class="panel-head"><div><h3>Limits &amp; reliability</h3><p class="section-description">Bounds on how much history Clerk reads and how hard it retries.</p></div><button class="button ghost small" type="button" data-action="run-diagnostics">Run diagnostics</button></header><div class="panel-body"><div class="form-grid">
          ${settingInput("history_lookback_days", "History read from Actual (days)", s.history_lookback_days, { type: "number", min: 30, max: 3650, note: "Feeds the memory and the income average." })}
          ${settingInput("transaction_stale_days", "Call an account quiet after (days)", s.transaction_stale_days, { type: "number", min: 1, max: 365, note: "Drives \u201CNo recent transactions\u201D. Raise it for accounts that only see action monthly." })}
          ${settingInput("balance_stale_hours", "Call a bank balance stale after (hours)", s.balance_stale_hours, { type: "number", min: 2, max: 720, note: "Drives \u201CStale data\u201D, which is about the age of the balance the bank reports \u2014 not how many transactions arrive." })}
          ${settingInput("balance_tolerance", "Balance difference to ignore", s.balance_tolerance, { type: "number", min: 0, step: 0.01, note: "Pending transactions make small differences normal." })}
          ${settingInput("request_timeout_seconds", "Request timeout (seconds)", s.request_timeout_seconds, { type: "number", min: 10 })}
          ${settingInput("model_max_retries", "Request retries", s.model_max_retries, { type: "number", min: 0, max: 10 })}
          ${settingInput("job_max_attempts", "Attempts per run", s.job_max_attempts, { type: "number", min: 1, max: 10 })}
          ${settingInput("log_level", "Container log detail", s.log_level, { type: "select", full: true, choices: [["DEBUG", "Debug"], ["INFO", "Info (recommended)"], ["WARNING", "Warnings only"], ["ERROR", "Errors only"]], note: "Changing this requires a restart." })}
        </div>${settingCheck("allow_new_categories", "Let Clerk propose new categories", "Clerk never creates a category on its own; with this on, a merchant that fits nowhere produces a suggestion you can accept in one click.", s.allow_new_categories)}</div></section>

        <div class="settings-save"><p>Appearance previews immediately. Everything else applies to the next run.</p><button class="button primary" type="submit">Save settings</button></div>
      </form>
    </div>`;
}

// ------------------------------------------------------------------ actions

async function enqueue(kind, label, extra = {}) {
  try {
    const result = await api("/api/jobs", { method: "POST", body: JSON.stringify({ kind, ...extra }) });
    toast(result.created ? `${label} started` : `${label} already running`, result.created ? "Progress appears under Activity." : "");
    await renderRoute({ quiet: true });
  } catch (error) { toast(`Could not start ${label.toLowerCase()}`, error.message, "error"); }
}

async function resolveReviewGroup(idList, action, categoryId, always = false) {
  const ids = (idList || "").split(",").filter(Boolean);
  if (!ids.length) return;
  try {
    const body = { ids, action, always, ...(categoryId ? { category_id: categoryId } : {}) };
    const result = await api("/api/reviews/resolve", { method: "POST", body: JSON.stringify(body) });
    if (action === "dismiss") toast("Skipped", `${result.resolved} transaction(s) will not be asked about again.`);
    else if (!result.resolved) toast("Nothing to change", "Those transactions were already resolved or removed in Actual.");
    else if (result.rules) toast("Applied, and now a rule", `${result.resolved} transaction(s) categorized. This merchant is filed here from now on; the rule lives under Intelligence.`);
    else toast("Applied in Actual", `${result.resolved} transaction(s) categorized, and Clerk will remember this merchant.`);
    closeDrawer();
    await renderRoute({ quiet: true });
    return true;
  } catch (error) { toast("Could not apply", error.message, "error"); return false; }
}

async function resolveReview(id, action, categoryId, always = false) {
  try {
    const body = { action, always, ...(categoryId ? { category_id: categoryId } : {}) };
    const result = await api(`/api/reviews/${id}/resolve`, { method: "POST", body: JSON.stringify(body) });
    if (result.status === "skipped") toast("Nothing to change", "The transaction was removed in Actual.");
    else if (action === "dismiss") toast("Skipped", "Clerk will not ask about this transaction again.");
    else if (result.rule) toast("Applied, and now a rule", `${result.category_name || "Category"} saved. This merchant is filed here from now on; the rule lives under Intelligence.`);
    else toast("Applied in Actual", `${result.category_name || "Category"} saved, and Clerk will remember it.`);
    closeDrawer();
    await renderRoute({ quiet: true });
  } catch (error) { toast("Could not apply", error.message, "error"); }
}

function closeCategoryPickers(except = null) {
  document.querySelectorAll('.category-picker.open').forEach((picker) => {
    if (picker === except) return;
    picker.classList.remove("open");
    picker.querySelector('[data-role="category-picker-menu"]').hidden = true;
    picker.querySelector('[data-action="category-picker-toggle"]').setAttribute("aria-expanded", "false");
  });
}

function filterCategoryPicker(picker) {
  const query = picker.querySelector('[data-role="category-picker-search"]').value.trim().toLocaleLowerCase();
  let visible = 0;
  picker.querySelectorAll(".category-picker-option").forEach((option) => {
    option.hidden = !option.dataset.search.includes(query);
    if (!option.hidden) visible += 1;
  });
  picker.querySelectorAll("[data-category-group]").forEach((group) => {
    group.hidden = !group.querySelector(".category-picker-option:not([hidden])");
  });
  picker.querySelector('[data-role="category-picker-empty"]').hidden = visible !== 0;
}

document.addEventListener("click", async (event) => {
  // A control that owns its own event lives inside a clickable row.
  if (event.target.closest("[data-stop]")) { event.stopPropagation(); return; }
  const categoryPickerRoot = event.target.closest(".category-picker");
  if (!categoryPickerRoot) closeCategoryPickers();
  const target = event.target.closest("[data-action]");
  if (!target) return;
  const action = target.dataset.action;

  if (action === "close-drawer") closeDrawer();
  if (action === "open-claim") claimDialog.showModal();
  if (action === "close-claim") claimDialog.close();
  if (action === "close-category") categoryDialog.close();
  if (action === "create-category") {
    event.stopPropagation();
    const options = document.querySelector("#group-options");
    options.innerHTML = (overview().groups || []).map((group) => `<option value="${escapeHtml(group)}"></option>`).join("");
    categoryForm.querySelector('[name="name"]').value = target.dataset.name || "";
    categoryForm.querySelector('[name="group_name"]').value = "";
    categoryDialog.showModal();
  }
  if (action === "category-picker-toggle") {
    event.stopPropagation();
    const picker = target.closest(".category-picker");
    const opening = !picker.classList.contains("open");
    closeCategoryPickers(picker);
    picker.classList.toggle("open", opening);
    picker.querySelector('[data-role="category-picker-menu"]').hidden = !opening;
    target.setAttribute("aria-expanded", String(opening));
    if (opening) {
      const search = picker.querySelector('[data-role="category-picker-search"]');
      search.value = "";
      filterCategoryPicker(picker);
      search.focus();
    }
  }
  if (action === "category-picker-select") {
    event.stopPropagation();
    const picker = target.closest(".category-picker");
    picker.dataset.value = target.dataset.categoryId;
    state.reviewChoices.set(picker.dataset.id, target.dataset.categoryId);
    picker.querySelector('[data-role="category-picker-label"]').textContent = `${target.dataset.groupName} · ${target.dataset.categoryName}`;
    picker.querySelectorAll(".category-picker-option").forEach((option) => {
      option.classList.toggle("selected", option === target);
      option.setAttribute("aria-selected", String(option === target));
    });
    const row = picker.closest(".review-row");
    row.classList.toggle("changed", target.dataset.categoryId !== picker.dataset.suggestionId);
    row.querySelectorAll('[data-action="review-accept-group"]').forEach((button) => { button.disabled = false; });
    closeCategoryPickers();
    picker.querySelector('[data-action="category-picker-toggle"]').focus();
  }
  if (action === "sync-now") enqueue("sync", "Sync");
  if (action === "sync-and-recheck") {
    closeDrawer();
    enqueue("sync", "Bank sync and connection check");
  }
  if (action === "retry-reviews") {
    if (!window.confirm(`Ask Clerk to classify the ${state.reviews.length} waiting transaction(s) again?\n\nModel suggestions will stay in Review for approval. Only reliable history for an established merchant can apply automatically.`)) return;
    enqueue("categorize", "Review retry", { reviews: true });
  }
  if (action === "catch-up") {
    if (!window.confirm("Find uncategorized transactions across the whole retained history, including items older than the normal filing window?\n\nThis is mainly a first-run or occasional catch-up. Existing Review items stay unchanged; use Retry review queue for those.")) return;
    enqueue("categorize", "History catch-up", { full: true });
  }
  if (action === "send-digest") {
    event.preventDefault();
    // A rehearsal, not the real thing: it must not consume today's delivery.
    await enqueue("digest", "Morning report", { force: true });
  }
  if (action === "run-diagnostics") { event.preventDefault(); await runDiagnostics(Boolean(state.diagnosticsRedact)); }
  if (action === "rerun-diagnostics") { event.preventDefault(); await runDiagnostics(Boolean(state.diagnosticsRedact)); }
  if (action === "copy-diagnostics") { event.preventDefault(); await copyDiagnostics(); }
  if (action === "toggle-diagnostics-redact") { event.preventDefault(); await runDiagnostics(!state.diagnosticsRedact); }
  if (action === "run-health" || action === "check-connections") enqueue("health", "Connection check");

  if (action === "migrate-open") { event.preventDefault(); await showMigration(target.dataset.id, target.dataset.direction); }
  if (action === "migrate-preview" || action === "migrate-apply") {
    event.preventDefault();
    const direction = target.dataset.direction;
    const body = migrationBody(target.dataset.id, direction);
    if (!body) return;
    const apply = action === "migrate-apply";
    if (apply && !window.confirm(direction === "to-plaid"
      ? `Move this account's feed to Plaid?${body.unlink_actual ? "\n\nActual's own link is removed first. Nothing already imported changes." : ""}\n\nA sync runs right away.`
      : "Hand this account back to Actual's SimpleFIN link?\n\nClerk pauses its Plaid mapping and Actual imports from the starting date. A sync runs right away.")) return;
    target.disabled = true;
    try {
      const result = await api(`/api/migration/${direction}`, { method: "POST", body: JSON.stringify({ ...body, dry_run: !apply }) });
      if (!apply) { renderMigrationPreview(direction, result); return; }
      toast(direction === "to-plaid" ? "Feed moved to Plaid" : "Feed handed back to SimpleFIN", "A sync is running; the Connections page updates as it finishes.");
      closeDrawer();
      await renderRoute({ quiet: true });
    } catch (error) { toast(apply ? "The move was not made" : "Preview failed", error.message, "error"); }
    finally { target.disabled = false; }
  }
  if (action === "simplefin-server-token") {
    event.preventDefault();
    const token = window.prompt("Paste a SimpleFIN setup token for the Actual server.\n\nThis is the token Actual's own Settings would take; the server claims it itself. It is separate from the read-only access URL Clerk holds.");
    if (!token || !token.trim()) return;
    target.disabled = true;
    try {
      const result = await api("/api/simplefin/server-token", { method: "POST", body: JSON.stringify({ setup_token: token.trim() }) });
      toast("Token stored on the Actual server", result.configured ? "Actual can link SimpleFIN accounts again." : "Stored, but the server does not report it as configured yet.");
      await renderSettings();
    } catch (error) { toast("Could not store the token", error.message, "error"); }
    finally { target.disabled = false; }
  }
  if (action === "simplefin-server-clear") {
    event.preventDefault();
    if (!window.confirm("Remove the SimpleFIN token from the Actual server?\n\nAccounts Actual still links to SimpleFIN stay linked but stop syncing until a token is stored again. Clerk's own read-only access URL is unaffected.")) return;
    target.disabled = true;
    try {
      await api("/api/simplefin/server-token", { method: "DELETE" });
      toast("Server token removed");
      await renderSettings();
    } catch (error) { toast("Could not remove the token", error.message, "error"); }
    finally { target.disabled = false; }
  }
  if (action === "plaid-connect") { event.preventDefault(); await startPlaidLink(); }
  if (action === "plaid-repair") { event.stopPropagation(); await startPlaidLink({ itemId: target.dataset.id }); }
  if (action === "plaid-item-detail") { event.stopPropagation(); showPlaidItem(target.dataset.id); }
  if (action === "plaid-sandbox-item") {
    target.disabled = true;
    try {
      const result = await api("/api/plaid/sandbox/items", { method: "POST", body: JSON.stringify({}) });
      toast("Sandbox bank connected", "Plaid prepares its transaction history for a minute or so.");
      await refreshPlaidDrawer(result.item.item_id);
    } catch (error) { toast("Could not create the sandbox bank", error.message, "error"); }
    finally { target.disabled = false; }
  }
  if (action === "plaid-reset-login") {
    event.stopPropagation();
    if (!window.confirm("Break this sandbox connection's login?\n\nPlaid will start answering ITEM_LOGIN_REQUIRED for it, which is how the Repair flow is rehearsed.")) return;
    try {
      await api(`/api/plaid/sandbox/items/${encodeURIComponent(target.dataset.id)}/reset-login`, { method: "POST" });
      toast("Login broken", "Run a connection check or open the connection to see it flagged.");
      closeDrawer();
      await renderRoute({ quiet: true });
    } catch (error) { toast("Could not reset the login", error.message, "error"); }
  }
  if (action === "plaid-remove-item") {
    event.stopPropagation();
    const limited = state.plaid?.slots?.limit;
    if (!window.confirm(`Remove ${target.dataset.name || "this bank"} from Plaid?\n\nIts account mappings are paused, and everything already imported into Actual stays.${limited ? " On the Trial plan this does not give the connection slot back." : ""}`)) return;
    try {
      const result = await api(`/api/plaid/items/${encodeURIComponent(target.dataset.id)}/remove`, { method: "POST" });
      toast("Connection removed", result.plaid_error ? `Plaid said: ${result.plaid_error}` : `${result.links_disabled} mapping(s) paused.`);
      closeDrawer();
      await renderRoute({ quiet: true });
    } catch (error) { toast("Could not remove the connection", error.message, "error"); }
  }
  if (action === "plaid-map") {
    event.preventDefault();
    const form = target.closest(".plaid-map-form");
    const targetValue = form.querySelector('[name="target"]').value;
    if (!targetValue) return toast("Choose an Actual account first", "", "error");
    const body = { item_id: target.dataset.itemId, external_account_id: target.dataset.externalId, cutover_date: form.querySelector('[name="cutover_date"]').value || "" };
    if (targetValue === "__new__") {
      body.new_account = { name: form.querySelector('[name="new_name"]').value.trim(), off_budget: form.querySelector('[name="new_off_budget"]').checked };
      if (!body.new_account.name) return toast("Name the new account", "", "error");
    } else body.actual_account_id = targetValue;
    target.disabled = true;
    try {
      await api("/api/plaid/links", { method: "POST", body: JSON.stringify(body) });
      toast("Account mapped", "The next sync delivers this account's transactions into Actual.");
      await refreshPlaidDrawer(target.dataset.itemId);
    } catch (error) { toast("Could not map the account", error.message, "error"); target.disabled = false; }
  }
  if (action === "plaid-unmap") {
    event.preventDefault();
    if (!window.confirm("Forget this mapping?\n\nThe Actual account and everything imported into it stay; Clerk just stops delivering this bank account to it.")) return;
    const itemId = state.plaid?.items?.find((item) => (item.accounts || []).some((account) => account.link?.actual_account_id === target.dataset.accountId))?.item_id;
    try {
      await api(`/api/plaid/links/${encodeURIComponent(target.dataset.accountId)}`, { method: "DELETE" });
      toast("Mapping removed");
      await refreshPlaidDrawer(itemId);
    } catch (error) { toast("Could not remove the mapping", error.message, "error"); }
  }
  if (action === "plaid-link-toggle" || action === "plaid-cutover-save") {
    event.preventDefault();
    const form = target.closest(".plaid-map-form");
    const body = action === "plaid-link-toggle"
      ? { enabled: target.dataset.enabled === "1" }
      : { cutover_date: form.querySelector('[name="cutover_date"]').value || "" };
    const itemId = state.plaid?.items?.find((item) => (item.accounts || []).some((account) => account.link?.actual_account_id === target.dataset.accountId))?.item_id;
    try {
      await api(`/api/plaid/links/${encodeURIComponent(target.dataset.accountId)}`, { method: "PATCH", body: JSON.stringify(body) });
      toast(action === "plaid-link-toggle" ? (body.enabled ? "Mapping resumed" : "Mapping paused") : "Import date saved");
      await refreshPlaidDrawer(itemId);
    } catch (error) { toast("Could not update the mapping", error.message, "error"); }
  }
  if (action === "anticipated-dismiss" || action === "anticipated-reopen") {
    event.stopPropagation();
    const verb = action === "anticipated-dismiss" ? "dismiss" : "reopen";
    try {
      await api(`/api/anticipated/charges/${encodeURIComponent(target.dataset.id)}/${verb}`, { method: "POST" });
      toast(verb === "dismiss" ? "Charge dismissed" : "Charge counted again", verb === "dismiss" ? "It no longer counts as spent." : "It counts as spent until the bank posts it.");
      await renderRoute({ quiet: true });
    } catch (error) { toast("Could not update the charge", error.message, "error"); }
  }
  if (action === "anticipated-teach-alias") {
    event.stopPropagation();
    const payee = window.prompt(`What payee does the bank post "${target.dataset.merchant || "this merchant"}" as? Type it as it appears in Actual.`);
    if (!payee || !payee.trim()) return;
    try {
      await api(`/api/anticipated/charges/${encodeURIComponent(target.dataset.id)}/alias`, { method: "POST", body: JSON.stringify({ payee: payee.trim() }) });
      toast("Alias taught", "The merchant's history and memory now apply to this notification.");
      await renderRoute({ quiet: true });
    } catch (error) { toast("Could not teach the alias", error.message, "error"); }
  }
  if (action === "anticipated-alias-delete") {
    event.stopPropagation();
    try {
      await api(`/api/anticipated/aliases/${encodeURIComponent(target.dataset.id)}`, { method: "DELETE" });
      await renderRoute({ quiet: true });
    } catch (error) { toast("Could not forget the alias", error.message, "error"); }
  }
  if (action === "anticipated-source-remove") {
    event.stopPropagation();
    if (!window.confirm(`Remove ${target.dataset.name} as a source? Its anticipated charges are dropped; nothing in Actual changes.`)) return;
    try {
      await api(`/api/anticipated/sources/${encodeURIComponent(target.dataset.id)}`, { method: "DELETE" });
      toast("Source removed", "The phone will need to register it again to forward from it.");
      await renderRoute({ quiet: true });
    } catch (error) { toast("Could not remove the source", error.message, "error"); }
  }
  if (action === "job-detail") showJob(target.dataset.id);
  if (action === "review-detail") showDecision(target.dataset.id);
  if (action === "account-detail") showAccount(target.dataset.id);
  if (action === "activity-view") { state.activityView = target.dataset.view; renderActivity(); }
  if (action === "job-filter") { state.jobFilter = target.dataset.filter; renderActivity(); }

  if (action === "review-accept") {
    event.stopPropagation();
    const select = document.querySelector(`[data-role="review-category"][data-id="${CSS.escape(target.dataset.id)}"]`);
    const chosen = select?.value || "";
    await resolveReview(target.dataset.id, chosen ? "recategorize" : "accept", chosen || undefined, target.dataset.always === "1");
  }
  if (action === "review-dismiss") { event.stopPropagation(); await resolveReview(target.dataset.id, "dismiss"); }
  if (action === "review-accept-group") {
    event.stopPropagation();
    const key = target.dataset.key;
    const picker = document.querySelector(`[data-role="review-category"][data-id="${CSS.escape(key)}"]`);
    // The choice is read from state, not the row: the row may have been
    // redrawn since it was made, and the state is what the redraw drew from.
    const chosen = state.reviewChoices.get(key) || picker?.dataset.value || "";
    const corrected = chosen && chosen !== (target.dataset.suggestionId || "");
    if (await resolveReviewGroup(target.dataset.ids, corrected ? "recategorize" : "accept", chosen || undefined, target.dataset.always === "1")) state.reviewChoices.delete(key);
  }
  if (action === "review-dismiss-group") {
    event.stopPropagation();
    await resolveReviewGroup(target.dataset.ids, "dismiss");
  }
  if (action === "review-dismiss-all") {
    event.stopPropagation();
    const ids = (target.dataset.ids || "").split(",").filter(Boolean);
    if (!window.confirm(`Skip all ${ids.length} waiting transaction(s)?\n\nThey keep whatever category they already have in Actual, and Clerk stops asking about them.`)) return;
    await resolveReviewGroup(target.dataset.ids, "dismiss");
  }

  if (action === "proposal-accept" || action === "proposal-decline") {
    event.stopPropagation();
    const accept = action === "proposal-accept";
    const picker = document.querySelector(`[data-role="proposal-category"][data-id="${CSS.escape(target.dataset.id)}"]`);
    const chosen = picker?.value || "";
    if (accept && picker && !chosen) return toast("Choose a category first", "", "error");
    target.disabled = true;
    try {
      const result = await api(`/api/intelligence/proposals/${encodeURIComponent(target.dataset.id)}/resolve`, { method: "POST", body: JSON.stringify({ action: accept ? "accept" : "decline", ...(chosen ? { category_id: chosen } : {}) }) });
      toast(accept ? "Done" : "Proposal declined", accept ? (result.rule ? `${result.rule.merchant_label || result.rule.merchant_key} → ${result.rule.category_name}${result.rule.status === "retired" ? " (retired)" : ""}` : "") : "Clerk will not ask this again.");
      await renderRoute({ quiet: true });
    } catch (error) { toast("Could not resolve the proposal", error.message, "error"); target.disabled = false; }
  }
  if (action === "rule-status") {
    event.stopPropagation();
    const next = target.dataset.status;
    if (next === "retired" && !window.confirm("Retire this rule?\n\nClerk stops filing the merchant by it and falls back to what it has learned. The rule stays listed under retired rules so it can be reinstated.")) return;
    target.disabled = true;
    try {
      await api(`/api/intelligence/rules/${encodeURIComponent(target.dataset.id)}`, { method: "PATCH", body: JSON.stringify({ status: next }) });
      toast({ active: "Rule active", paused: "Rule paused", retired: "Rule retired" }[next] || "Rule updated");
      await renderRoute({ quiet: true });
    } catch (error) { toast("Could not update the rule", error.message, "error"); target.disabled = false; }
  }
  if (action === "rule-make") {
    event.stopPropagation();
    target.disabled = true;
    try {
      const result = await api("/api/intelligence/rules", { method: "POST", body: JSON.stringify({ merchant_key: target.dataset.key, merchant: target.dataset.label || "", category_id: target.dataset.categoryId }) });
      toast("Rule made", `${result.rule.merchant_label || result.rule.merchant_key} is filed as ${result.rule.category_name} from now on.`);
      closeDrawer();
      await renderRoute({ quiet: true });
    } catch (error) { toast("Could not make the rule", error.message, "error"); target.disabled = false; }
  }
  if (action === "actual-read") {
    event.preventDefault();
    target.disabled = true;
    await readActualRules();
    await renderIntelligence();
  }
  if (action === "actual-import" || action === "actual-retire" || action === "actual-restore") {
    event.preventDefault();
    const kind = action.replace("actual-", "");
    const dry = target.dataset.dry === "1";
    const ids = (target.dataset.ids || "").split(",").filter(Boolean);
    if (!dry && kind === "retire" && !window.confirm(`Delete ${ids.length ? "this rule" : "every imported rule"} from Actual?\n\nClerk keeps a copy and files these merchants itself from the next sync on. Restore puts them back.`)) return;
    if (!dry && kind === "restore" && !window.confirm("Recreate the retired rules in Actual from the copies Clerk kept?\n\nActual will apply them at import again; Clerk's rules stay too.")) return;
    target.disabled = true;
    try {
      const result = await api(`/api/intelligence/actual/${kind}`, { method: "POST", body: JSON.stringify({ rule_ids: ids, dry_run: dry }) });
      if (!dry) {
        await readActualRules();
        await renderIntelligence();
        toast({ import: "Imported into Clerk", retire: "Retired in Actual", restore: "Restored to Actual" }[kind], "");
      }
      renderActualResult(kind, result);
    } catch (error) { toast(`Could not ${kind}`, error.message, "error"); }
    finally { target.disabled = false; }
  }
  if (action === "alias-teach") {
    event.stopPropagation();
    const payee = window.prompt(`What payee does the bank post "${target.dataset.alias}" as? Type it as it appears in Actual.`);
    if (!payee || !payee.trim()) return;
    try {
      const result = await api("/api/intelligence/aliases", { method: "POST", body: JSON.stringify({ alias: target.dataset.alias, merchant: payee.trim() }) });
      toast("Alias taught", `“${result.alias.alias_key}” now resolves to “${result.alias.merchant_key}”.`);
      await renderRoute({ quiet: true });
    } catch (error) { toast("Could not teach the alias", error.message, "error"); }
  }
  if (action === "alias-delete") {
    event.stopPropagation();
    try {
      await api(`/api/intelligence/aliases/${encodeURIComponent(target.dataset.id)}`, { method: "DELETE" });
      await renderRoute({ quiet: true });
    } catch (error) { toast("Could not forget the alias", error.message, "error"); }
  }
  if (action === "propose-from-history") {
    event.preventDefault();
    if (!window.confirm("Look through your whole filing history and propose a rule for every merchant that has only ever been filed one way?\n\nNothing is declared until you accept each one; Decline all waves the rest away.")) return;
    enqueue("categorize", "Rule proposals from history", { propose_rules: true });
  }
  if (action === "proposals-decline-all") {
    event.preventDefault();
    if (!window.confirm(`Decline all ${target.dataset.count} open proposals?\n\nClerk will not ask these again.`)) return;
    try {
      const result = await api("/api/intelligence/proposals/decline", { method: "POST", body: JSON.stringify({}) });
      toast("Proposals declined", `${result.declined} closed.`);
      await renderIntelligence();
    } catch (error) { toast("Could not decline", error.message, "error"); }
  }
  if (action === "intelligence-tab") {
    state.intelligenceTab = target.dataset.tab;
    // A tab is a place on the page, so the address follows it without the
    // hashchange round trip that would redraw from a blank loader.
    history.replaceState(null, "", `#intelligence-${target.dataset.tab}`);
    drawIntelligence();
  }
  if (action === "proposal-filter") { state.proposalFilter = target.dataset.filter; drawIntelligence(); }
  if (action === "rule-filter") { state.ruleFilter = target.dataset.filter; drawIntelligence(); }
  if (action === "merchant-filter") { state.merchantFilter = target.dataset.filter; drawIntelligence(); }
  if (action === "rule-form-toggle" || action === "alias-form-toggle") {
    const key = action === "rule-form-toggle" ? "ruleFormOpen" : "aliasFormOpen";
    state[key] = !state[key];
    drawIntelligence();
    if (state[key]) document.querySelector(action === "rule-form-toggle" ? "#rule-form [name=merchant]" : "#alias-form [name=alias]")?.focus();
  }

  if (action === "forget-merchant") {
    event.stopPropagation();
    try {
      await api(`/api/memory/${encodeURIComponent(target.dataset.key)}`, { method: "DELETE" });
      toast("Merchant forgotten", "Clerk will work this merchant out again from scratch.");
      if (state.route === "intelligence") await renderIntelligence();
    } catch (error) { toast("Could not forget the merchant", error.message, "error"); }
  }

  if (action === "retry-job") {
    event.stopPropagation();
    try { await api(`/api/jobs/${target.dataset.id}/retry`, { method: "POST" }); toast("Task queued"); closeDrawer(); await renderRoute({ quiet: true }); }
    catch (error) { toast("Retry failed", error.message, "error"); }
  }
  if (action === "cancel-job") {
    event.stopPropagation();
    try { await api(`/api/jobs/${target.dataset.id}/cancel`, { method: "POST" }); toast("Task cancelled"); await renderRoute({ quiet: true }); }
    catch (error) { toast("Cancel failed", error.message, "error"); }
  }

  if (action === "test-connection") {
    target.disabled = true;
    const original = target.textContent;
    target.textContent = "Testing…";
    try {
      const result = await api(`/api/settings/test/${target.dataset.target}`, { method: "POST" });
      toast("Connection succeeded", result.message || result.response || "");
    } catch (error) { toast("Connection failed", error.message, "error"); }
    finally { target.disabled = false; target.textContent = original; }
  }
});

document.addEventListener("change", (event) => {
  if (!event.target.matches('[data-role="plaid-target"]')) return;
  const field = event.target.closest(".plaid-map-form")?.querySelector(".plaid-new-account");
  if (field) field.hidden = event.target.value !== "__new__";
});

scrim.addEventListener("click", closeDrawer);
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  const picker = document.querySelector(".category-picker.open");
  if (picker) {
    closeCategoryPickers();
    picker.querySelector('[data-action="category-picker-toggle"]').focus();
  } else {
    closeDrawer();
  }
});
document.querySelector("#mobile-menu").addEventListener("click", () => document.querySelector(".sidebar").classList.toggle("mobile-open"));
document.querySelectorAll(".nav-list a").forEach((item) => item.addEventListener("click", () => document.querySelector(".sidebar").classList.remove("mobile-open")));

claimForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const token = String(new FormData(claimForm).get("setup_token") || "").trim();
  if (!token) return toast("Paste a setup token first", "", "error");
  const button = claimForm.querySelector('[type="submit"]');
  button.disabled = true;
  try {
    await api("/api/settings/simplefin/claim", { method: "POST", body: JSON.stringify({ setup_token: token }) });
    toast("SimpleFIN connected", "Clerk claimed the token and is checking your accounts.");
    claimDialog.close(); claimForm.reset();
    await renderRoute({ quiet: true });
  } catch (error) { toast("Could not claim the token", error.message, "error"); }
  finally { button.disabled = false; }
});

categoryForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(categoryForm);
  const button = categoryForm.querySelector('[type="submit"]');
  button.disabled = true;
  try {
    await api("/api/categories", { method: "POST", body: JSON.stringify({ name: form.get("name"), group_name: form.get("group_name") }) });
    toast("Category created", "Give it a budget in Actual so Clerk counts it as committed.");
    categoryDialog.close(); categoryForm.reset();
    await renderRoute({ quiet: true });
  } catch (error) { toast("Could not create the category", error.message, "error"); }
  finally { button.disabled = false; }
});

content.addEventListener("submit", async (event) => {
  if (event.target.id !== "alias-form") return;
  event.preventDefault();
  const form = event.target;
  const data = new FormData(form);
  const button = form.querySelector('[type="submit"]');
  button.disabled = true;
  try {
    const result = await api("/api/intelligence/aliases", { method: "POST", body: JSON.stringify({ alias: String(data.get("alias") || ""), merchant: String(data.get("merchant") || "") }) });
    toast("Alias taught", `“${result.alias.alias_key}” now resolves to “${result.alias.merchant_key}”.`);
    state.aliasFormOpen = false;
    await renderIntelligence();
  } catch (error) { toast("Could not teach the alias", error.message, "error"); }
  finally { button.disabled = false; }
});

content.addEventListener("submit", async (event) => {
  if (event.target.id !== "rule-form") return;
  event.preventDefault();
  const form = event.target;
  const data = new FormData(form);
  const button = form.querySelector('[type="submit"]');
  button.disabled = true;
  try {
    const result = await api("/api/intelligence/rules", { method: "POST", body: JSON.stringify({
      merchant: String(data.get("merchant") || ""),
      category_id: String(data.get("category_id") || ""),
      account_id: String(data.get("account_id") || ""),
      match: data.get("family") === "on" ? "family" : "exact",
    }) });
    toast("Rule made", `“${result.rule.merchant_key}” is filed as ${result.rule.category_name} from now on.`);
    state.ruleFormOpen = false;
    state.ruleFilter = "active";
    await renderIntelligence();
  } catch (error) { toast("Could not make the rule", error.message, "error"); }
  finally { button.disabled = false; }
});

content.addEventListener("submit", async (event) => {
  if (event.target.id !== "settings-form") return;
  event.preventDefault();
  const form = event.target;
  const data = new FormData(form);
  const values = {};
  const locked = new Set(state.settings?.environment_overrides || []);
  const integers = new Set(["model_context_tokens", "model_max_output_tokens", "memory_min_observations", "categorize_lookback_days", "history_lookback_days", "ai_example_count", "category_candidate_limit", "rule_promote_after", "memory_dispute_threshold", "income_lookback_months", "sync_interval_minutes", "health_interval_minutes", "transaction_stale_days", "balance_stale_hours", "request_timeout_seconds", "model_max_retries", "job_max_attempts", "plaid_days_requested", "plaid_refresh_min_interval_minutes", "plaid_refresh_wait_seconds", "plaid_adopt_window_days", "anticipated_match_window_days", "anticipated_expire_days"]);
  const decimals = new Set(["memory_min_confidence", "ai_min_confidence", "monthly_income_override", "balance_tolerance"]);
  const checks = ["actual_verify_ssl", "categorization_enabled", "ai_enabled", "ai_alias_questions", "rule_promotion_enabled", "memory_learn_from_actual", "tagging_enabled", "tag_provenance", "tag_anomalies", "allow_new_categories", "sync_enabled", "bank_sync_enabled", "digest_enabled", "digest_show_headline", "digest_show_spending", "digest_show_safe_to_spend", "digest_show_pace", "digest_show_projection", "digest_show_commitments", "digest_show_balances", "digest_show_connections", "digest_show_attention", "notifications_enabled", "health_alerts_enabled", "plaid_sync_enabled", "plaid_refresh_enabled", "plaid_delete_removed_pending", "plaid_starting_balance", "anticipated_enabled"];

  for (const [key, value] of data.entries()) {
    if (key.startsWith("clear_") || key === "committed_groups") continue;
    values[key] = integers.has(key) ? Number.parseInt(value, 10) : decimals.has(key) ? Number.parseFloat(value) : value;
  }
  for (const key of checks) if (!locked.has(key)) values[key] = data.get(key) === "on";
  for (const key of ["actual_password", "actual_encryption_password", "simplefin_access_url", "plaid_secret", "anticipated_device_token", "openai_api_key", "ntfy_token"]) {
    if (data.get(`clear_${key}`) === "on") values[key] = "";
    else if (!values[key]) delete values[key];
  }

  const button = form.querySelector('[type="submit"]');
  button.disabled = true;
  try {
    const result = await api("/api/settings", { method: "PATCH", body: JSON.stringify({ values }) });
    toast("Settings saved", result.restart_required.length ? `Restart required for: ${result.restart_required.join(", ")}` : "New tasks will use these values.");
    state.settings = result.settings;
    applyAppearance(result.settings);
    const scrollPosition = window.scrollY;
    await renderSettings();
    requestAnimationFrame(() => window.scrollTo({ top: scrollPosition }));
  } catch (error) { toast("Settings were not saved", error.message, "error"); }
  finally { button.disabled = false; }
});

content.addEventListener("change", async (event) => {
  const toggle = event.target.closest('[data-action="toggle-monitoring"]');
  if (!toggle) return;
  const monitored = toggle.checked;
  toggle.disabled = true;
  try {
    await api(`/api/accounts/${encodeURIComponent(toggle.dataset.id)}/monitoring`, {
      method: "POST",
      body: JSON.stringify({ monitored, account_name: toggle.dataset.name || "" }),
    });
    toast(
      monitored ? "Monitoring on" : "Monitoring off",
      monitored
        ? "Clerk will score this connection again and alert when it changes."
        : "The account stays linked in Actual; Clerk just stops alerting on it.",
    );
    await renderRoute({ quiet: true });
  } catch (error) {
    toggle.checked = !monitored;
    toast("Could not change monitoring", error.message, "error");
  } finally {
    toggle.disabled = false;
  }
});

content.addEventListener("change", (event) => {
  const picker = event.target.closest('[data-role="proposal-category"]');
  if (!picker) return;
  const row = picker.closest("[data-proposal-id]");
  const accept = row?.querySelector('[data-action="proposal-accept"]');
  if (accept) accept.disabled = !picker.value;
});

content.addEventListener("change", async (event) => {
  const select = event.target.closest('[data-action="rule-category"]');
  if (!select) return;
  select.disabled = true;
  try {
    await api(`/api/intelligence/rules/${encodeURIComponent(select.dataset.id)}`, { method: "PATCH", body: JSON.stringify({ category_id: select.value }) });
    toast("Rule changed", "The next filing run uses the new category.");
    await renderIntelligence();
  } catch (error) {
    toast("Could not change the rule", error.message, "error");
    select.disabled = false;
  }
});

content.addEventListener("change", async (event) => {
  const teach = event.target.closest('[data-action="anticipated-teach-category"]');
  if (teach) {
    teach.disabled = true;
    try {
      await api(`/api/anticipated/charges/${encodeURIComponent(teach.dataset.id)}/category`, { method: "POST", body: JSON.stringify({ category_id: teach.value }) });
      toast(teach.value ? "Rule made" : "Category cleared", teach.value ? "Counted against that category now, and this merchant is always filed there from now on." : "The rule is retired; rules and memory decide again.");
      await renderRoute({ quiet: true });
    } catch (error) {
      toast("Could not teach the category", error.message, "error");
      teach.disabled = false;
    }
    return;
  }
  const control = event.target.closest('[data-action="anticipated-source-account"], [data-action="anticipated-source-toggle"]');
  if (!control) return;
  const body = control.dataset.action === "anticipated-source-toggle"
    ? { enabled: control.checked }
    : { actual_account_id: control.value };
  control.disabled = true;
  try {
    await api(`/api/anticipated/sources/${encodeURIComponent(control.dataset.id)}`, { method: "PATCH", body: JSON.stringify(body) });
    toast(body.enabled === undefined ? "Source moved" : body.enabled ? "Forwarding on" : "Forwarding paused", body.enabled === undefined ? "Open anticipated charges follow it to the new account." : "");
    await renderRoute({ quiet: true });
  } catch (error) {
    toast("Could not update the source", error.message, "error");
    await renderRoute({ quiet: true });
  } finally {
    control.disabled = false;
  }
});

content.addEventListener("input", (event) => {
  if (event.target.id === "decision-search") {
    state.decisionSearch = event.target.value;
    filterDecisionRows();
  }
  if (event.target.id === "rule-search") {
    state.ruleSearch = event.target.value;
    filterRuleRows();
  }
  if (event.target.id === "merchant-search") {
    state.merchantSearch = event.target.value;
    filterMerchantRows();
  }
  if (event.target.id === "alias-search") {
    state.aliasSearch = event.target.value;
    filterAliasRows();
  }
  if (event.target.form?.id === "rule-form" && event.target.name === "merchant") {
    clearTimeout(ruleKeyPreviewTimer);
    const text = event.target.value;
    ruleKeyPreviewTimer = setTimeout(() => previewRuleKey(text), 250);
  }
  const picker = event.target.closest(".category-picker");
  if (picker && event.target.matches('[data-role="category-picker-search"]')) {
    filterCategoryPicker(picker);
  }
});

content.addEventListener("change", (event) => {
  if (event.target.form?.id !== "settings-form") return;
  if (!event.target.name?.startsWith("appearance_")) return;
  const form = new FormData(event.target.form);
  applyAppearance({
    appearance_theme: form.get("appearance_theme"),
    appearance_density: form.get("appearance_density"),
    appearance_motion: form.get("appearance_motion"),
  }, false);
});

async function navigateFromHash() {
  const anchor = location.hash.slice(1);
  const requested = anchor.split("-")[0] || "overview";
  state.route = pageMeta[requested] ? requested : "overview";
  if (state.route === "intelligence") {
    const tab = anchor.slice("intelligence-".length);
    if (INTELLIGENCE_TABS.some(([id]) => id === tab)) state.intelligenceTab = tab;
    // A link straight to the From Actual tab reads the rule table on arrival.
    if (tab === "actual" && !state.actualRules && !state.actualRulesError) await readActualRules();
  }
  await renderRoute();
  if (anchor.startsWith("settings-")) {
    requestAnimationFrame(() => document.getElementById(anchor)?.scrollIntoView({
      behavior: document.documentElement.dataset.motion === "reduced" || (document.documentElement.dataset.motion === "system" && reducedMotionQuery.matches) ? "auto" : "smooth",
      block: "start",
    }));
  }
}

window.addEventListener("hashchange", navigateFromHash);

async function pollVisibleRoute() {
  try {
    if (document.hidden || state.route === "settings" || document.querySelector(".category-picker.open")) return;
    if (state.route === "intelligence" && intelligenceFormBusy()) return;
    if (state.route === "activity") {
      await renderActivity({ poll: true, refreshDrawer: Boolean(state.openJobId) });
    } else if (!drawer.classList.contains("open")) {
      await renderRoute({ quiet: true, poll: true });
    }
  } catch { /* keep the last good screen */ }
  finally {
    state.poll = setTimeout(pollVisibleRoute, state.route === "activity" ? 3000 : 8000);
  }
}

async function initialize() {
  try { state.settings = await api("/api/settings"); applyAppearance(state.settings); }
  catch { /* cached or system appearance remains active */ }
  try {
    state.health = await api("/api/health");
    document.querySelector("#app-version").textContent = `Actual Clerk ${state.health.version}`;
  } catch { /* the main route reports the error */ }
  if (!(await resumePlaidLinkAfterOAuth())) await navigateFromHash();
  state.poll = setTimeout(pollVisibleRoute, state.route === "activity" ? 3000 : 8000);
}

initialize();
