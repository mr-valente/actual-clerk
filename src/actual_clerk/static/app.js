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
  rules: [],
  accounts: null,
  decisions: [],
  jobs: [],
  settings: null,
  health: null,
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
  error: "SimpleFIN reported an error for this bank. Reauthorize it in your SimpleFIN Bridge, then run a check here.",
  missing: "SimpleFIN is no longer returning this account. Relink it in your SimpleFIN Bridge, then run a check here.",
  stale: "The bank has stopped sending SimpleFIN fresh data for this account. Nothing needs fixing in Actual, and it usually clears on its own once the bank refreshes.",
  drifted: "Actual and the bank disagree about posted money, so a transaction is missing on one side. Compare the account with the bank and reconcile it in Actual.",
};
function connectionRemedies(degraded) {
  const byStatus = new Map();
  for (const item of degraded) {
    if (!byStatus.has(item.status)) byStatus.set(item.status, []);
    byStatus.get(item.status).push(item.account_name);
  }
  return [...byStatus.entries()].map(([status, names]) => {
    const remedy = STATUS_REMEDIES[status] || "Run a check here for the current detail.";
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
          : `${money(spent)} of ${money(available)} spent since the 1st.`} Free money is ${money(report.expected_income_cents)} expected income (${escapeHtml(incomeNote)}) minus ${money(report.committed_cents)} already budgeted for bills.${returned ? ` A further ${money(returned)} came back this month, refunding a purchase from an earlier one.` : ""}</p>
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
    <div class="row-title"><strong>${escapeHtml(item.account_name || "Account")}</strong><small>${escapeHtml(item.institution || item.sync_source || "Manual account")}</small></div>
    <div class="row-meta">${escapeHtml((item.detail || "").slice(0, 90))}${age ? `<br /><span${item.remote_balance_date ? ` title="Bank balance dated ${escapeHtml(fullTime(item.remote_balance_date))}"` : ""}>Bank data ${escapeHtml(age)}</span>` : ""}</div>
    <div class="row-meta">${item.remote_balance_cents === null || item.remote_balance_cents === undefined
      ? "—"
      : `<strong>${money(item.remote_balance_cents)}</strong><br /><span>${balanceComparison}</span>`}</div>
    <div class="health-state">${statusChip(item.status, item.status_label)}${monitorControl}</div>
  </article>`;
}

function ruleRow(item) {
  return `<article class="data-row rule-row">
    <span class="mark">⚡</span>
    <div class="row-title"><strong>${escapeHtml(item.merchant_label || item.merchant_key)} → ${escapeHtml(item.category_name)}</strong><small>Matches payees containing “${escapeHtml(item.match_value)}” · ${item.observations} consistent decision(s)</small></div>
    <div class="row-meta">Created ${relativeTime(item.created_at)}</div>
    <div class="row-actions">
      <button class="button ghost small" data-action="rule-decline" data-id="${escapeHtml(item.id)}">No thanks</button>
      <button class="button primary small" data-action="rule-create" data-id="${escapeHtml(item.id)}">Create rule</button>
    </div>
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
    <span class="mark ${escapeHtml(item.source)}">${{ memory: "↺", model: "✦", unresolved: "?" }[item.source] || "•"}</span>
    <div class="row-title"><strong>${escapeHtml(item.payee_name || item.merchant_key || "Transaction")}</strong><small>${shortDate(item.transaction_date)} · <span class="money">${money(item.amount_cents)}</span> · ${escapeHtml(item.account_name)}</small></div>
    <div class="row-meta"><strong>${escapeHtml(item.category_name || item.proposed_category || "—")}</strong><br /><span>${escapeHtml(titleCase(item.source))} · ${percent(item.confidence)}</span></div>
    <div>${statusChip(item.status)}</div>
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
  setBadge("nav-review-count", (counts.needs_review || 0) + (counts.rule_suggestions || 0));
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

async function renderRoute({ quiet = false } = {}) {
  if (!quiet) content.innerHTML = `<div class="initial-loader"><span class="loader-mark"></span><p>Loading…</p></div>`;
  try {
    state.data = await api("/api/overview");
    if (state.route === "overview") {
      try { state.health = await api("/api/health"); } catch { /* keep the last answer */ }
    }
    if (state.route === "overview") await renderOverview();
    if (state.route === "review") await renderReview();
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
    : !configured.simplefin
      ? `<div class="alert-banner"><span>2</span><div><strong>Connect SimpleFIN to verify your bank links</strong><p>Without it Clerk can only tell that transactions stopped arriving, not that a bank connection is the reason.</p></div><a class="button ghost" href="#accounts">Connect</a></div>`
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
          <header class="panel-head"><div><h2>Bank connections</h2><p>Checked directly against SimpleFIN, not just Actual's last sync</p></div><a href="#accounts" class="panel-link">All accounts</a></header>
          <div class="status-list">${health.length ? health.slice(0, 6).map((item) => healthRow(item)).join("") : emptyState("⇄", "No connection data yet", "Clerk checks every linked account against SimpleFIN. Run a sync, or add your SimpleFIN access URL in Settings.")}</div>
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

function categoryPicker(group) {
  const categories = overview().categories || [];
  const selected = categories.find((category) => category.id === group.suggestion_id);
  const suggestedId = selected?.id || "";
  const grouped = new Map();
  for (const category of categories) {
    const name = category.group_name || "Other";
    if (!grouped.has(name)) grouped.set(name, []);
    grouped.get(name).push(category);
  }
  const options = [...grouped.entries()].map(([groupName, items]) => `
    <section class="category-picker-group" data-category-group>
      <p>${escapeHtml(groupName)}</p>
      ${items.map((category) => `<button type="button" role="option" aria-selected="${category.id === suggestedId}" class="category-picker-option ${category.id === suggestedId ? "selected" : ""}" data-action="category-picker-select" data-category-id="${escapeHtml(category.id)}" data-category-name="${escapeHtml(category.name)}" data-group-name="${escapeHtml(groupName)}" data-search="${escapeHtml(`${groupName} ${category.name}`.toLocaleLowerCase())}"><span>${escapeHtml(category.name)}</span>${category.id === suggestedId ? "<i>Suggested</i>" : ""}</button>`).join("")}
    </section>`).join("");
  const label = selected
    ? `${selected.group_name ? `${selected.group_name} · ` : ""}${selected.name}`
    : "Choose a category…";
  return `<div class="category-picker" data-role="review-category" data-id="${escapeHtml(group.key)}" data-value="${escapeHtml(suggestedId)}" data-suggestion-id="${escapeHtml(suggestedId)}">
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
  const suggestedId = (overview().categories || []).some((category) => category.id === group.suggestion_id) ? group.suggestion_id : "";
  return `<article class="data-row review-row">
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
      <button class="button primary small" data-action="review-accept-group" data-ids="${escapeHtml(ids)}" data-key="${escapeHtml(group.key)}" data-suggestion-id="${escapeHtml(suggestedId)}" ${suggestedId ? "" : "disabled"}>Apply${group.items.length > 1 ? ` ${group.items.length}` : ""}</button>
    </div>
  </article>`;
}

async function renderReview() {
  [state.reviews, state.rules] = await Promise.all([api("/api/reviews"), api("/api/rules")]);
  const suggestions = state.rules;
  const groups = groupReviews(state.reviews);
  const allIds = state.reviews.map((item) => item.id).join(",");
  const withSuggestion = groups.filter((group) => group.suggestion_id).length;
  content.innerHTML = `
    <section class="page-intro"><div><h2>Review</h2><p>Clerk can automatically follow reliable merchant history, but every first-time merchant requires your approval. Retry waiting items after changing the model or fixing classification; catch up older history only when you want to search beyond the normal recent window.</p></div><div class="actions"><button class="button ghost" data-action="catch-up">Catch up older history</button><button class="button primary" data-action="retry-reviews" ${state.reviews.length ? "" : "disabled"}>Retry review queue${state.reviews.length ? ` (${state.reviews.length})` : ""}</button></div></section>
    ${suggestions.length ? `<article class="panel">
      <header class="panel-head"><div><h2>Rules worth promoting</h2><p>Merchants Clerk has filed the same way repeatedly. A native Actual rule handles them at import, before Clerk or any model is involved.</p></div>${statusChip("suggested", `${suggestions.length} suggested`)}</header>
      <div class="status-list">${suggestions.map(ruleRow).join("")}</div>
    </article>` : ""}
    <article class="panel review-panel" style="margin-top:18px">
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

async function renderAccounts() {
  state.accounts = await api("/api/accounts");
  const health = state.accounts.health || [];
  const events = state.accounts.events || [];
  const linked = health.filter((item) => item.status !== "not_linked");
  const muted = health.filter((item) => item.status === "muted");
  const degraded = health.filter(isDegraded);
  const configured = state.data?.overview ? true : false;
  content.innerHTML = `
    <section class="page-intro"><div><h2>Bank connections</h2><p>Clerk asks SimpleFIN directly what each account looks like right now, then compares that with what Actual holds. A connection that quietly stops delivering data shows up here as a status change rather than as a slowly staler budget.</p></div><div class="actions"><button class="button ghost" data-action="check-connections">Check now</button><button class="button primary" data-action="open-claim">Connect SimpleFIN</button></div></section>
    ${degraded.length ? `<div class="alert-banner critical"><span>!</span><div><strong>${degraded.length} connection${degraded.length === 1 ? "" : "s"} need attention</strong>${connectionRemedies(degraded)}</div></div>` : ""}
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
        ${item.status === "needs_review" ? `<button class="button primary" data-action="review-accept" data-id="${escapeHtml(item.id)}">Apply proposal</button>` : ""}
      </div>
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
    <div class="resolution-actions"><button class="button primary" data-action="sync-and-recheck">Sync bank and recheck</button></div>
  </div>`);
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
  state.settings = await api("/api/settings");
  const s = state.settings;
  applyAppearance(s);
  content.innerHTML = `<section class="page-intro"><div><h2>Settings</h2><p>Connect Actual, SimpleFIN, and a local model, then decide how much Clerk should do on its own. Save changed values before testing a connection.</p></div></section>
    <div class="settings-layout">
      <nav class="settings-nav">
        <a href="#settings-actual">Actual Budget</a>
        <a href="#settings-simplefin">SimpleFIN</a>
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
        </div><div class="section-actions"><button class="button secondary small" type="button" data-action="open-claim">Claim a setup token</button><span class="muted" style="font-size:10px">A setup token can only be claimed once. Claiming here does not affect the token Actual already uses.</span></div></div></section>

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
          <div class="form-grid">
            ${settingInput("apply_mode", "For merchants Clerk already knows", s.apply_mode, { type: "select", full: true, choices: [["automatic", "Apply reliable history in Actual"], ["review", "Propose every category for review"]], note: "First-time merchants always require approval, regardless of this setting." })}
            ${settingInput("memory_min_confidence", "Confidence needed from memory", s.memory_min_confidence, { type: "number", min: 0, max: 1, step: 0.01, note: "Evidence from your own filing history." })}
            ${settingInput("memory_min_observations", "Sightings needed from memory", s.memory_min_observations, { type: "number", min: 1, max: 50 })}
            ${settingInput("categorize_lookback_days", "Only file transactions newer than (days)", s.categorize_lookback_days, { type: "number", min: 1, max: 730 })}
            ${settingInput("ai_example_count", "Examples shown to the model", s.ai_example_count, { type: "number", min: 0, max: 40, note: "Your own filed transactions, which is what makes the model match your habits." })}
            ${settingInput("category_candidate_limit", "Categories offered to the model", s.category_candidate_limit, { type: "number", min: 10, max: 400 })}
          </div>
          ${settingToggle("rule_promotion_enabled", "Suggest Actual rules for settled merchants", "After the same merchant is filed the same way a few times, Clerk offers to write a native Actual rule so the answer costs nothing.", s.rule_promotion_enabled)}
          <div class="form-grid">${settingInput("rule_promote_after", "Consistent decisions before suggesting a rule", s.rule_promote_after, { type: "number", min: 2, max: 25, full: true })}</div>
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

async function resolveReviewGroup(idList, action, categoryId) {
  const ids = (idList || "").split(",").filter(Boolean);
  if (!ids.length) return;
  try {
    const body = { ids, action, ...(categoryId ? { category_id: categoryId } : {}) };
    const result = await api("/api/reviews/resolve", { method: "POST", body: JSON.stringify(body) });
    if (action === "dismiss") toast("Skipped", `${result.resolved} transaction(s) will not be asked about again.`);
    else if (!result.resolved) toast("Nothing to change", "Those transactions were already resolved or removed in Actual.");
    else toast("Applied in Actual", `${result.resolved} transaction(s) categorized, and Clerk will remember this merchant.`);
    closeDrawer();
    await renderRoute({ quiet: true });
  } catch (error) { toast("Could not apply", error.message, "error"); }
}

async function resolveReview(id, action, categoryId) {
  try {
    const body = { action, ...(categoryId ? { category_id: categoryId } : {}) };
    const result = await api(`/api/reviews/${id}/resolve`, { method: "POST", body: JSON.stringify(body) });
    if (result.status === "skipped") toast("Nothing to change", "The transaction was removed in Actual.");
    else if (action === "dismiss") toast("Skipped", "Clerk will not ask about this transaction again.");
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
    picker.querySelector('[data-role="category-picker-label"]').textContent = `${target.dataset.groupName} · ${target.dataset.categoryName}`;
    picker.querySelectorAll(".category-picker-option").forEach((option) => {
      option.classList.toggle("selected", option === target);
      option.setAttribute("aria-selected", String(option === target));
    });
    picker.closest(".review-row").querySelector('[data-action="review-accept-group"]').disabled = false;
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
  if (action === "job-detail") showJob(target.dataset.id);
  if (action === "review-detail") showDecision(target.dataset.id);
  if (action === "account-detail") showAccount(target.dataset.id);
  if (action === "activity-view") { state.activityView = target.dataset.view; renderActivity(); }
  if (action === "job-filter") { state.jobFilter = target.dataset.filter; renderActivity(); }

  if (action === "review-accept") {
    event.stopPropagation();
    const select = document.querySelector(`[data-role="review-category"][data-id="${CSS.escape(target.dataset.id)}"]`);
    const chosen = select?.value || "";
    await resolveReview(target.dataset.id, chosen ? "recategorize" : "accept", chosen || undefined);
  }
  if (action === "review-dismiss") { event.stopPropagation(); await resolveReview(target.dataset.id, "dismiss"); }
  if (action === "review-accept-group") {
    event.stopPropagation();
    const picker = document.querySelector(`[data-role="review-category"][data-id="${CSS.escape(target.dataset.key)}"]`);
    const chosen = picker?.dataset.value || "";
    const corrected = chosen && chosen !== (target.dataset.suggestionId || "");
    await resolveReviewGroup(target.dataset.ids, corrected ? "recategorize" : "accept", chosen || undefined);
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

  if (action === "rule-create" || action === "rule-decline") {
    const create = action === "rule-create";
    target.disabled = true;
    try {
      await api(`/api/rules/${target.dataset.id}/resolve`, { method: "POST", body: JSON.stringify({ action: create ? "create" : "decline" }) });
      toast(create ? "Rule created in Actual" : "Suggestion dismissed", create ? "Actual now applies it during import." : "");
      await renderRoute({ quiet: true });
    } catch (error) { toast("Could not update the rule", error.message, "error"); target.disabled = false; }
  }

  if (action === "forget-merchant") {
    try {
      await api(`/api/memory/${encodeURIComponent(target.dataset.key)}`, { method: "DELETE" });
      toast("Merchant forgotten", "Clerk will work this merchant out again from scratch.");
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
  if (event.target.id !== "settings-form") return;
  event.preventDefault();
  const form = event.target;
  const data = new FormData(form);
  const values = {};
  const locked = new Set(state.settings?.environment_overrides || []);
  const integers = new Set(["model_context_tokens", "model_max_output_tokens", "memory_min_observations", "categorize_lookback_days", "history_lookback_days", "ai_example_count", "category_candidate_limit", "rule_promote_after", "income_lookback_months", "sync_interval_minutes", "health_interval_minutes", "transaction_stale_days", "balance_stale_hours", "request_timeout_seconds", "model_max_retries", "job_max_attempts"]);
  const decimals = new Set(["memory_min_confidence", "ai_min_confidence", "monthly_income_override", "balance_tolerance"]);
  const checks = ["actual_verify_ssl", "categorization_enabled", "ai_enabled", "rule_promotion_enabled", "tagging_enabled", "tag_provenance", "tag_anomalies", "allow_new_categories", "sync_enabled", "bank_sync_enabled", "digest_enabled", "digest_show_headline", "digest_show_spending", "digest_show_safe_to_spend", "digest_show_pace", "digest_show_projection", "digest_show_commitments", "digest_show_balances", "digest_show_connections", "digest_show_attention", "notifications_enabled", "health_alerts_enabled"];

  for (const [key, value] of data.entries()) {
    if (key.startsWith("clear_") || key === "committed_groups") continue;
    values[key] = integers.has(key) ? Number.parseInt(value, 10) : decimals.has(key) ? Number.parseFloat(value) : value;
  }
  for (const key of checks) if (!locked.has(key)) values[key] = data.get(key) === "on";
  for (const key of ["actual_password", "actual_encryption_password", "simplefin_access_url", "openai_api_key", "ntfy_token"]) {
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

content.addEventListener("input", (event) => {
  if (event.target.id === "decision-search") {
    state.decisionSearch = event.target.value;
    filterDecisionRows();
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
    if (state.route === "activity") {
      await renderActivity({ poll: true, refreshDrawer: Boolean(state.openJobId) });
    } else if (!drawer.classList.contains("open")) {
      await renderRoute({ quiet: true });
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
  await navigateFromHash();
  state.poll = setTimeout(pollVisibleRoute, state.route === "activity" ? 3000 : 8000);
}

initialize();
