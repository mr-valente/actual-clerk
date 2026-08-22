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
  jobFilter: "all",
  recurringFilter: "all",
  poll: null,
};

const pageMeta = {
  overview: ["Budget", "Overview"],
  review: ["Needs a decision", "Review"],
  accounts: ["Bank sync", "Connections"],
  recurring: ["Commitments", "Recurring"],
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
// A manual account has nothing to watch and a muted one was opted out, so
// neither belongs in the "connected" ratio.
function isWatched(item) { return item.status !== "not_linked" && item.status !== "muted"; }
function isQuiet(item) { return item.status === "no_transactions"; }

// -------------------------------------------------------------- components

function budgetHero() {
  const report = budget();
  if (!report || !Object.keys(report).length) {
    return `<section class="hero"><div class="hero-top"><div class="hero-headline"><p class="eyebrow">Free money</p><div class="hero-amount"><strong>—</strong></div><p class="hero-sub">Clerk has not read the budget yet. Run a sync, or check the Actual connection in Settings.</p></div></div></section>`;
  }
  if (!report.configured) {
    return `<section class="hero"><div class="hero-top"><div class="hero-headline"><p class="eyebrow">Free money</p><div class="hero-amount"><strong>Set your income</strong></div><p class="hero-sub">Clerk works out free money as expected income minus everything you have budgeted. It cannot find any income yet. Budget your expected income against an income category in Actual (the tracking budget asks for exactly this), or enter a monthly figure in Clerk's settings.</p></div><div class="hero-side"><a class="button primary" href="#settings-budget">Set monthly income</a></div></div></section>`;
  }

  const free = report.free_cents || 0;
  const spent = report.spent_cents || 0;
  const remaining = report.remaining_cents || 0;
  const spentRatio = free > 0 ? Math.min(1.4, spent / free) : 1;
  const paceRatio = report.days_in_month ? report.day_of_month / report.days_in_month : 0;
  const overspent = remaining < 0;
  const paceClass = report.on_track ? "pace-ahead" : "pace-behind";
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
          <span class="pct">${percent(report.remaining_percent)} left</span>
        </div>
        <p class="hero-sub">${overspent
          ? `You are ${money(Math.abs(remaining))} past the free money for this month.`
          : `${money(spent)} of ${money(free)} spent since the 1st.`} Free money is ${money(report.expected_income_cents)} expected income (${escapeHtml(incomeNote)}) minus ${money(report.committed_cents)} already budgeted for bills.</p>
      </div>
      <div class="hero-side">
        <div class="hero-chip"><span>Safe to spend daily</span><strong>${money(report.daily_safe_to_spend_cents)}</strong></div>
        <div class="hero-chip ${paceClass}"><span>${report.on_track ? "Ahead of pace" : "Behind pace"}</span><strong>${money(Math.abs(report.pace_delta_cents))}</strong></div>
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
      <div><span>Uncategorized</span><strong class="money">${money(report.uncategorized_cents)}${report.uncategorized_count ? ` · ${report.uncategorized_count}` : ""}</strong></div>
    </div>
  </section>`;
}

function healthRow(item, { actions = true, toggle = false } = {}) {
  const drift = item.drift_cents;
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
    <div class="row-meta">${escapeHtml((item.detail || "").slice(0, 90))}</div>
    <div class="row-meta">${item.remote_balance_cents === null || item.remote_balance_cents === undefined
      ? "—"
      : `<strong>${money(item.remote_balance_cents)}</strong><br /><span>${drift ? `${money(drift, { sign: true })} vs cleared` : "matches cleared"}</span>`}</div>
    <div class="health-state">${statusChip(item.status, item.status_label)}${monitorControl}</div>
  </article>`;
}

function reviewRow(item) {
  const proposal = item.category_name || item.proposed_category || "No suggestion";
  const options = (overview().categories || []).map((category) =>
    `<option value="${escapeHtml(category.id)}" ${category.id === item.category_id ? "selected" : ""}>${escapeHtml(category.group_name ? `${category.group_name} · ${category.name}` : category.name)}</option>`).join("");
  return `<article class="data-row review-row">
    <div class="row-title"><strong>${escapeHtml(item.payee_name || item.merchant_key || "Transaction")}</strong><small>${shortDate(item.transaction_date)} · ${escapeHtml(item.account_name)} · <span class="money">${money(item.amount_cents)}</span></small>${(item.tags || []).length ? `<span class="tag-list">${item.tags.map((tag) => `<span class="tag">#${escapeHtml(tag)}</span>`).join("")}</span>` : ""}</div>
    <div><select class="select-inline" data-role="review-category" data-id="${escapeHtml(item.id)}"><option value="">${escapeHtml(item.category_id ? proposal : "Choose a category…")}</option>${options}</select></div>
    <div>${statusChip(item.source)}</div>
    <div class="row-meta"><span class="confidence ${item.confidence < 0.5 ? "low" : ""}">${percent(item.confidence)}</span><br /><span>confidence</span></div>
    <div class="row-actions">
      ${item.proposed_category ? `<button class="button secondary small" data-action="create-category" data-name="${escapeHtml(item.proposed_category)}" title="The model found no existing category for this merchant">+ ${escapeHtml(item.proposed_category)}</button>` : ""}
      <button class="button ghost small" data-action="review-detail" data-id="${escapeHtml(item.id)}">Why</button>
      <button class="button ghost small" data-action="review-dismiss" data-id="${escapeHtml(item.id)}">Skip</button>
      <button class="button primary small" data-action="review-accept" data-id="${escapeHtml(item.id)}">Apply</button>
    </div>
  </article>`;
}

function ruleRow(item) {
  return `<article class="data-row rule-row">
    <span class="mark">⚡</span>
    <div class="row-title"><strong>${escapeHtml(item.merchant_label || item.merchant_key)} → ${escapeHtml(item.category_name)}</strong><small>Matches imported descriptions containing “${escapeHtml(item.match_value)}” · ${item.observations} consistent decision(s)</small></div>
    <div class="row-meta">Created ${relativeTime(item.created_at)}</div>
    <div class="row-actions">
      <button class="button ghost small" data-action="rule-decline" data-id="${escapeHtml(item.id)}">No thanks</button>
      <button class="button primary small" data-action="rule-create" data-id="${escapeHtml(item.id)}">Create rule</button>
    </div>
  </article>`;
}

function recurringRow(item) {
  return `<article class="data-row recurring-row">
    <span class="mark ${escapeHtml(item.kind)}">${item.kind === "subscription" ? "∞" : "↻"}</span>
    <div class="row-title"><strong>${escapeHtml(item.label)}</strong><small>${escapeHtml(titleCase(item.cadence))} · ${escapeHtml(item.category_name || "Uncategorized")} · ${item.occurrences} charge(s)</small>${item.flags.length ? `<span class="tag-list">${item.flags.map((flag) => `<span class="tag">${escapeHtml(flag)}</span>`).join("")}</span>` : ""}</div>
    <div class="row-amount money">${money(item.latest_amount_cents)}</div>
    <div class="row-amount money">${money(item.monthly_cost_cents)}<br /><span class="row-meta">per month</span></div>
    <div class="row-meta">Next ${shortDate(item.next_expected)}${item.days_overdue ? `<br /><span class="danger-text">${item.days_overdue} day(s) late</span>` : ""}</div>
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
  if (job.kind === "sync") return `${result.transactions ?? 0} transaction(s) read${result.imported ? `, ${result.imported} imported` : ""}`;
  if (job.kind === "categorize") {
    const scope = result.full_history ? "full history · " : "";
    const base = `${scope}${result.applied ?? 0} applied · ${result.needs_review ?? 0} to review · ${result.model_calls ?? 0} model call(s)`;
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
    ? (state.data.apply_mode === "automatic" ? "Filing automatically" : "Proposing for review")
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
    if (state.route === "recurring") renderRecurring();
    if (state.route === "activity") await renderActivity();
    if (state.route === "settings") await renderSettings();
  } catch (error) {
    if (!quiet) content.innerHTML = `<div class="panel">${emptyState("!", "Could not load this page", error.message)}</div>`;
  }
  updateChrome();
}

async function renderOverview() {
  const data = state.data;
  const view = overview();
  const counts = data.counts || {};
  // Everything read straight off the snapshot first, then anything derived
  // from it -- a derived value placed above its source throws.
  const health = view.health || [];
  const fresh = view.freshness || {};
  const recurringSummary = view.recurring_summary || {};
  const upcoming = view.upcoming || [];
  const trend = view.trend || [];
  const report = budget();

  const degraded = health.filter(isDegraded);
  const watched = health.filter(isWatched);
  const quiet = watched.filter(isQuiet);
  const mutedCount = health.filter((item) => item.status === "muted").length;
  const staleAccounts = (fresh.accounts || []).filter((item) => item.stale);
  const maxTrend = Math.max(1, ...trend.map((item) => item.spent_cents));

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
      <article class="metric-card ${counts.needs_review ? "warning" : "good"}"><span class="metric-icon">◇</span><span class="metric-label">Waiting for you</span><div class="metric-value">${counts.needs_review || 0}</div><span class="metric-note">${counts.applied_today || 0} filed automatically today</span></article>
      <article class="metric-card ${degraded.length ? "error" : "good"}"><span class="metric-icon">⇄</span><span class="metric-label">Bank connections</span><div class="metric-value">${watched.filter((item) => item.status === "ok").length}/${watched.length}</div><span class="metric-note">${degraded.length ? `${degraded.length} need attention` : quiet.length ? `${quiet.length} quiet, none broken` : "All reporting normally"}${mutedCount ? ` · ${mutedCount} not monitored` : ""}</span></article>
      <article class="metric-card"><span class="metric-icon">∞</span><span class="metric-label">Recurring per month</span><div class="metric-value">${money(recurringSummary.monthly_total_cents || 0, { compact: true })}</div><span class="metric-note">${recurringSummary.count || 0} commitment(s), ${recurringSummary.subscription_count || 0} subscription(s)</span></article>
      <article class="metric-card ${report.uncategorized_count ? "warning" : ""}"><span class="metric-icon">✎</span><span class="metric-label">Uncategorized this month</span><div class="metric-value">${report.uncategorized_count || 0}</div><span class="metric-note">${money(report.uncategorized_cents || 0)} unaccounted for</span></article>
    </section>

    <section class="grid overview-grid">
      <div>
        <article class="panel">
          <header class="panel-head"><div><h2>Bank connections</h2><p>Checked directly against SimpleFIN, not just Actual's last sync</p></div><a href="#accounts" class="panel-link">All accounts</a></header>
          <div class="status-list">${health.length ? health.slice(0, 6).map((item) => healthRow(item)).join("") : emptyState("⇄", "No connection data yet", "Clerk checks every linked account against SimpleFIN. Run a sync, or add your SimpleFIN access URL in Settings.")}</div>
        </article>
        <article class="panel">
          <header class="panel-head"><div><h2>Where the free money went</h2><p>Discretionary spending this month, largest first</p></div></header>
          <div class="quick-list">${(report.top_categories || []).length
            ? report.top_categories.map((item) => `<div class="quick-item"><span>${escapeHtml((item.category_name || "?").slice(0, 1).toUpperCase())}</span><div><strong>${escapeHtml(item.category_name)}</strong><p>${escapeHtml(item.group_name || "")}</p></div><span class="row-amount money">${money(item.spent_cents)}</span></div>`).join("")
            : emptyState("✓", "Nothing discretionary yet", "Spending outside your committed categories shows up here as the month goes on.")}</div>
        </article>
        ${trend.length > 1 ? `<article class="panel">
          <header class="panel-head"><div><h2>Monthly spending</h2><p>Total on-budget outflow per month</p></div></header>
          <div class="panel-body"><div class="trend">${trend.map((item, index) => `<div class="trend-bar ${index === trend.length - 1 ? "current" : ""}"><i style="height:${Math.max(3, (item.spent_cents / maxTrend) * 92)}px" title="${escapeHtml(money(item.spent_cents))}"></i><span>${escapeHtml(item.month.slice(5))}</span></div>`).join("")}</div></div>
        </article>` : ""}
      </div>
      <div>
        <article class="panel">
          <header class="panel-head"><div><h2>Due in the next week</h2><p>Recurring charges Clerk expects</p></div><a href="#recurring" class="panel-link">All recurring</a></header>
          <div class="quick-list">${upcoming.length
            ? upcoming.slice(0, 5).map((item) => `<div class="quick-item"><span>${item.kind === "subscription" ? "∞" : "↻"}</span><div><strong>${escapeHtml(item.label)}</strong><p>${shortDate(item.next_expected)} · ${escapeHtml(item.category_name || "Uncategorized")}</p></div><span class="row-amount money">${money(item.typical_amount_cents)}</span></div>`).join("")
            : emptyState("↻", "Nothing due this week", "Clerk lists a charge here once it has seen the same merchant bill on a schedule three times.")}</div>
        </article>
        <article class="panel">
          <header class="panel-head"><div><h2>Recent activity</h2><p>Sync, filing, and connection checks</p></div><a href="#activity" class="panel-link">History</a></header>
          <div class="status-list">${(data.jobs || []).length ? data.jobs.slice(0, 5).map((job) => jobRow(job, { actions: false })).join("") : emptyState("↻", "No runs yet", "Clerk syncs on a schedule. Use Sync now to start the first run.")}</div>
        </article>
        ${data.digest ? `<article class="panel">
          <header class="panel-head"><div><h2>This morning's digest</h2><p>Sent ${escapeHtml(data.digest.local_date)}</p></div></header>
          <div class="panel-body"><strong style="font-size:12px">${escapeHtml(data.digest.payload?.title || "")}</strong><p class="muted" style="white-space:pre-line;font-size:11px;line-height:1.6;margin:8px 0 0">${escapeHtml(data.digest.payload?.message || "")}</p></div>
        </article>` : ""}
      </div>
    </section>`;
}

async function renderReview() {
  [state.reviews, state.rules] = await Promise.all([api("/api/reviews"), api("/api/rules")]);
  const suggestions = state.rules;
  content.innerHTML = `
    <section class="page-intro"><div><h2>Review</h2><p>Clerk files what it is confident about and asks about the rest. Applying a category here also teaches Clerk, so the same merchant is handled on its own next time.</p></div><div class="actions"><button class="button ghost" data-action="catch-up">Catch up on all history</button><button class="button ghost" data-action="run-categorize">Re-run filing</button></div></section>
    ${suggestions.length ? `<article class="panel">
      <header class="panel-head"><div><h2>Rules worth promoting</h2><p>Merchants Clerk has filed the same way repeatedly. A native Actual rule handles them at import, before Clerk or any model is involved.</p></div>${statusChip("suggested", `${suggestions.length} suggested`)}</header>
      <div class="status-list">${suggestions.map(ruleRow).join("")}</div>
    </article>` : ""}
    <article class="panel" style="margin-top:18px">
      <header class="panel-head"><div><h2>Transactions to categorize</h2><p>${state.reviews.length} waiting</p></div></header>
      <div class="status-list">${state.reviews.length ? state.reviews.map(reviewRow).join("") : emptyState("✓", "Nothing is waiting", "Every recent transaction has a category, or Clerk was confident enough to file it. New spending appears here when Clerk is unsure.")}</div>
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
    ${degraded.length ? `<div class="alert-banner critical"><span>!</span><div><strong>${degraded.length} connection${degraded.length === 1 ? "" : "s"} need attention</strong><p>Reauthorize the bank in your SimpleFIN Bridge, then run a check here.</p></div></div>` : ""}
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

function renderRecurring() {
  const view = overview();
  const series = view.recurring || [];
  const summary = view.recurring_summary || {};
  const filters = ["all", "subscription", "recurring", "unbudgeted", "overdue"];
  const filtered = series.filter((item) => {
    if (state.recurringFilter === "all") return true;
    if (state.recurringFilter === "unbudgeted") return !item.budgeted;
    if (state.recurringFilter === "overdue") return item.days_overdue > 0;
    return item.kind === state.recurringFilter;
  });
  content.innerHTML = `
    <section class="page-intro"><div><h2>Recurring commitments</h2><p>Everything Clerk has seen bill on a schedule at least three times. These are the amounts to budget for in Actual: once a category carries a budget, Clerk counts it as committed rather than as free money.</p></div></section>
    <section class="grid metrics">
      <article class="metric-card"><span class="metric-icon">∞</span><span class="metric-label">Per month</span><div class="metric-value">${money(summary.monthly_total_cents || 0, { compact: true })}</div><span class="metric-note">${summary.count || 0} commitment(s)</span></article>
      <article class="metric-card"><span class="metric-icon">▤</span><span class="metric-label">Subscriptions</span><div class="metric-value">${money(summary.subscription_monthly_cents || 0, { compact: true })}</div><span class="metric-note">${summary.subscription_count || 0} fixed-price renewals</span></article>
      <article class="metric-card ${summary.unbudgeted_count ? "warning" : "good"}"><span class="metric-icon">!</span><span class="metric-label">Not budgeted</span><div class="metric-value">${money(summary.unbudgeted_monthly_cents || 0, { compact: true })}</div><span class="metric-note">${summary.unbudgeted_count || 0} without a budget this month</span></article>
      <article class="metric-card ${summary.overdue_count ? "warning" : ""}"><span class="metric-icon">⏱</span><span class="metric-label">Overdue</span><div class="metric-value">${summary.overdue_count || 0}</div><span class="metric-note">Expected but not yet arrived</span></article>
    </section>
    ${(summary.price_changes || []).length ? `<div class="alert-banner"><span>↗</span><div><strong>Subscription prices changed</strong><p>${escapeHtml(summary.price_changes.map((item) => `${item.label} ${item.percent > 0 ? "+" : ""}${Math.round(item.percent * 100)}%`).join(" · "))}</p></div></div>` : ""}
    <article class="panel">
      <div class="toolbar"><div class="filter-tabs">${filters.map((filter) => `<button class="${filter === state.recurringFilter ? "active" : ""}" data-action="recurring-filter" data-filter="${filter}">${titleCase(filter)}</button>`).join("")}</div><span class="spacer"></span><span class="muted" style="font-size:11px">${filtered.length} shown</span></div>
      <div class="status-list">${filtered.length ? filtered.map(recurringRow).join("") : emptyState("↻", "Nothing matches", "Clerk needs three charges from the same merchant at a regular interval before it calls something recurring.")}</div>
    </article>`;
}

async function renderActivity() {
  [state.jobs, state.decisions] = await Promise.all([
    api("/api/jobs?limit=100"),
    api("/api/decisions?limit=150"),
  ]);
  const filters = ["all", "sync", "categorize", "health", "digest"];
  const jobs = state.jobs.filter((job) => state.jobFilter === "all" || job.kind === state.jobFilter);
  content.innerHTML = `
    <section class="page-intro"><div><h2>Activity</h2><p>Every run Clerk has made and every filing decision it recorded, including the ones it withheld.</p></div><div class="actions"><button class="button ghost" data-action="run-health">Check connections</button><button class="button primary" data-action="sync-now">Sync now</button></div></section>
    <article class="panel">
      <div class="toolbar"><div class="filter-tabs">${filters.map((filter) => `<button class="${filter === state.jobFilter ? "active" : ""}" data-action="job-filter" data-filter="${filter}">${titleCase(filter)}</button>`).join("")}</div></div>
      <div class="status-list">${jobs.length ? jobs.map((job) => jobRow(job)).join("") : emptyState("↻", "No runs yet", "Clerk records every scheduled and manual run here.")}</div>
    </article>
    <article class="panel" style="margin-top:18px">
      <div class="toolbar"><span class="muted" style="font-size:11px">${state.decisions.length} filing decision(s)</span><span class="spacer"></span><input class="search-input" id="decision-search" type="search" placeholder="Filter by merchant or category" /></div>
      <div class="status-list" id="decision-list">${state.decisions.length ? state.decisions.map(decisionRow).join("") : emptyState("✎", "No decisions yet", "Clerk records what it filed, what it withheld, and why, after the first categorization run.")}</div>
    </article>`;
}

// ------------------------------------------------------------------ drawers

function openDrawer(html) {
  drawerContent.innerHTML = html;
  drawer.classList.add("open"); scrim.classList.add("visible"); drawer.setAttribute("aria-hidden", "false");
}
function closeDrawer() { drawer.classList.remove("open"); scrim.classList.remove("visible"); drawer.setAttribute("aria-hidden", "true"); }
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
        <div class="detail-stat"><span>Needed</span><strong>${percent(rationale.threshold || 0)}</strong></div>
      </div></section>
      <section class="detail-section"><h3>Category</h3><div class="change-list">
        <div class="change"><i>${item.category_id ? "✓" : "?"}</i><div><strong>${escapeHtml(item.category_name || item.proposed_category || "No category proposed")}</strong><small>${escapeHtml(rationale.reason || (memory ? "Chosen from this budget's own filing history." : "No explanation was recorded."))}</small></div></div>
        ${(item.tags || []).map((tag) => `<div class="change"><i>#</i><div><strong>#${escapeHtml(tag)}</strong><small>Written into the transaction notes in Actual.</small></div></div>`).join("")}
      </div></section>
      ${memory ? `<section class="detail-section"><h3>Memory evidence</h3><div class="change-list"><div class="change"><i>↺</i><div><strong>${memory.observations} prior sighting(s), ${percent(memory.share)} agreement</strong><small>Matched on “${escapeHtml(memory.matched_key)}”${memory.exact ? "" : " by merchant prefix"}. Confidence ${percent(memory.confidence)}.</small></div></div>${evidence.map((entry) => `<div class="change"><i>·</i><div><strong>${escapeHtml(entry.category_name || entry.category_id)}</strong><small>${percent(entry.share)} of this merchant's history · ${entry.sightings} sighting(s)</small></div></div>`).join("")}</div></section>` : ""}
      ${(rationale.examples || []).length ? `<section class="detail-section"><h3>Examples given to the model</h3><p class="muted" style="font-size:10px">${escapeHtml(rationale.examples.filter(Boolean).join(", "))}</p></section>` : ""}
      <section class="detail-section"><h3>Raw record</h3><pre class="code-block">${escapeHtml(JSON.stringify({ merchant_key: item.merchant_key, source: item.source, status: item.status, rationale }, null, 2))}</pre></section>
      <div class="resolution-actions">
        ${actualUrl ? `<a class="button ghost" href="${escapeHtml(actualUrl)}" target="_blank" rel="noreferrer">Open Actual ↗</a>` : ""}
        ${item.merchant_key ? `<button class="button ghost" data-action="forget-merchant" data-key="${escapeHtml(item.merchant_key)}">Forget this merchant</button>` : ""}
        ${item.status === "needs_review" ? `<button class="button primary" data-action="review-accept" data-id="${escapeHtml(item.id)}">Apply proposal</button>` : ""}
      </div>
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
      <div class="detail-stat"><span>Bank balance</span><strong class="money">${item.remote_balance_cents === null || item.remote_balance_cents === undefined ? "—" : money(item.remote_balance_cents)}</strong></div>
      <div class="detail-stat"><span>Cleared vs bank</span><strong class="money ${item.drift_cents ? "negative" : ""}">${item.drift_cents === null || item.drift_cents === undefined ? "—" : money(item.drift_cents, { sign: true })}</strong></div>
      <div class="detail-stat"><span>Bank data age</span><strong>${item.balance_age_hours === null || item.balance_age_hours === undefined ? "—" : `${Math.round(item.balance_age_hours)}h`}</strong></div>
      <div class="detail-stat"><span>Last transaction</span><strong>${escapeHtml(item.last_transaction_date || "—")}</strong></div>
      <div class="detail-stat"><span>Actual last sync</span><strong>${item.last_sync ? relativeTime(item.last_sync) : "—"}</strong></div>
    </div></section>
    <section class="detail-section"><h3>What Clerk sees</h3><ul class="note-list">${(item.signals || []).length ? item.signals.map((signal) => `<li>${escapeHtml(signal)}</li>`).join("") : `<li>Balances agree and data is current.</li>`}</ul></section>
    <section class="detail-section"><h3>Link</h3><div class="change-list">
      <div class="change"><i>⇄</i><div><strong>${escapeHtml(item.sync_source || "Not linked")}</strong><small>${escapeHtml(item.external_id ? `External account ${item.external_id}` : "Clerk cannot verify a manual account against a bank.")}</small></div></div>
      <div class="change"><i>${item.monitored === false ? "✗" : "✓"}</i><div><strong>${item.monitored === false ? "Monitoring is off" : "Monitoring is on"}</strong><small>${item.monitored === false ? `Clerk still reads this account but will not alert on it${item.underlying_status ? `. Unmonitored, it would currently read as ${escapeHtml(titleCase(item.underlying_status))}.` : "."}` : "Clerk scores this connection and alerts when its status changes."}</small></div></div>
      <div class="change"><i>◷</i><div><strong>Status since ${escapeHtml(fullTime(item.since))}</strong><small>Last checked ${relativeTime(item.checked_at)}.</small></div></div>
    </div></section>
    <div class="resolution-actions"><button class="button primary" data-action="check-connections">Check again</button></div>
  </div>`);
}

async function showJob(id) {
  openDrawer(`${drawerHeader("Run detail", "Loading…")}<div class="drawer-body"><div class="skeleton"></div></div>`);
  try {
    const job = await api(`/api/jobs/${id}`);
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
      <div class="resolution-actions">${["failed", "cancelled"].includes(job.status) ? `<button class="button primary" data-action="retry-job" data-id="${escapeHtml(job.id)}">Retry run</button>` : ""}</div>
    </div>`);
  } catch (error) { toast("Could not load the run", error.message, "error"); closeDrawer(); }
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

function committedGroupPicker(settings) {
  const groups = overview().groups || [];
  const chosen = new Set(settings.committed_groups || []);
  if (!groups.length) {
    return `<div class="field full"><label>Committed category groups</label><small>Clerk lists your Actual category groups here after its first sync.</small></div>`;
  }
  return `<div class="field full"><label>Committed category groups</label>
    <div class="group-picker">${groups.map((group) => `<label><input type="checkbox" name="committed_groups" value="${escapeHtml(group)}" ${chosen.has(group) ? "checked" : ""} /> ${escapeHtml(group)}</label>`).join("")}</div>
    <small>Groups holding your recurring bills. Leave every box unchecked and Clerk treats any category you budgeted this month as committed, which is what the budget setup guide sets up.</small></div>`;
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
        <a href="#settings-budget">Budget report</a>
        <a href="#settings-schedule">Sync &amp; digest</a>
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
          ${settingInput("model_context_tokens", "Context limit", s.model_context_tokens, { type: "number", min: 2048 })}
          ${settingInput("model_max_output_tokens", "Maximum output tokens", s.model_max_output_tokens, { type: "number", min: 256 })}
        </div></div></section>

        <section class="panel settings-section" id="settings-filing"><header class="panel-head"><div><h3>Filing</h3><p class="section-description">How Clerk decides, and how much it does without asking.</p></div></header><div class="panel-body">
          ${settingToggle("categorization_enabled", "File uncategorized transactions", "Runs after every sync. Transactions you have already categorized are never touched.", s.categorization_enabled)}
          ${settingToggle("ai_enabled", "Ask the local model about new merchants", "With this off, Clerk still files merchants it recognizes and queues the rest for review.", s.ai_enabled)}
          <div class="form-grid">
            ${settingInput("apply_mode", "When Clerk is confident", s.apply_mode, { type: "select", full: true, choices: [["automatic", "Apply the category in Actual"], ["review", "Propose it for review instead"]] })}
            ${settingInput("memory_min_confidence", "Confidence needed from memory", s.memory_min_confidence, { type: "number", min: 0, max: 1, step: 0.01, note: "Evidence from your own filing history." })}
            ${settingInput("memory_min_observations", "Sightings needed from memory", s.memory_min_observations, { type: "number", min: 1, max: 50 })}
            ${settingInput("ai_min_confidence", "Confidence needed from the model", s.ai_min_confidence, { type: "number", min: 0, max: 1, step: 0.01 })}
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
            ${settingCheck("tag_cadence", "Tag subscriptions and recurring bills", "#subscription for fixed renewals, #recurring for variable ones, #annual for yearly charges.", s.tag_cadence)}
            ${settingCheck("tag_anomalies", "Tag refunds and outsized charges", "#refund for money coming back, #unusual for a charge far above a merchant's normal size.", s.tag_anomalies)}
          </div>
          <div class="form-grid">${settingInput("clerk_tag", "Clerk tag", s.clerk_tag, { full: true, note: "Written without the leading #." })}</div>
        </div></section>

        <section class="panel settings-section" id="settings-budget"><header class="panel-head"><div><h3>Budget report</h3><p class="section-description">Free money is expected income minus what you have already committed.</p></div></header><div class="panel-body"><div class="form-grid">
          ${settingInput("monthly_income_override", "Monthly income", s.monthly_income_override, { type: "number", min: 0, step: 0.01, note: "Leave at 0 and Clerk uses the income you budgeted in Actual, then income actually received, then a trailing average. Only set this if you budget no income in Actual." })}
          ${settingInput("income_lookback_months", "Months in the income average", s.income_lookback_months, { type: "number", min: 1, max: 12 })}
          ${settingInput("budget_currency", "Currency", s.budget_currency, { note: "Three-letter code, used for display only." })}
          ${settingInput("transaction_stale_days", "Warn when an account has no transaction for (days)", s.transaction_stale_days, { type: "number", min: 1, max: 90 })}
          ${committedGroupPicker(s)}
        </div></div></section>

        <section class="panel settings-section" id="settings-schedule"><header class="panel-head"><div><h3>Sync &amp; digest</h3><p class="section-description">How often Clerk pulls from Actual and when it sends the morning report.</p></div></header><div class="panel-body">
          ${settingToggle("sync_enabled", "Sync on a schedule", "Pulls the budget, asks Actual to run bank sync, then files what arrived.", s.sync_enabled)}
          ${settingToggle("bank_sync_enabled", "Run Actual's bank sync", "Turn this off if something else already triggers bank sync on a schedule.", s.bank_sync_enabled)}
          ${settingToggle("digest_enabled", "Send a morning digest", "One notification a day with your budget report and anything that needs attention.", s.digest_enabled)}
          <div class="form-grid">
            ${settingInput("sync_interval_minutes", "Sync every (minutes)", s.sync_interval_minutes, { type: "number", min: 5, max: 1440 })}
            ${settingInput("health_interval_minutes", "Check connections every (minutes)", s.health_interval_minutes, { type: "number", min: 5, max: 1440 })}
            ${settingInput("digest_time", "Digest time", s.digest_time, { note: "24-hour local time, for example 07:30." })}
            ${settingInput("timezone", "Time zone", s.timezone, { note: "An IANA name such as America/New_York." })}
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
          ${settingInput("appearance_theme", "Color theme", s.appearance_theme, { type: "select", choices: [["system", "Follow system"], ["light", "Light"], ["dark", "Dark"]] })}
          ${settingInput("appearance_density", "Interface density", s.appearance_density, { type: "select", choices: [["comfortable", "Comfortable"], ["compact", "Compact"]] })}
          ${settingInput("appearance_motion", "Animation and motion", s.appearance_motion, { type: "select", full: true, choices: [["system", "Follow system"], ["full", "Full motion"], ["reduced", "Reduced motion"]] })}
        </div></div></section>

        <section class="panel settings-section" id="settings-advanced"><header class="panel-head"><div><h3>Limits &amp; reliability</h3><p class="section-description">Bounds on how much history Clerk reads and how hard it retries.</p></div></header><div class="panel-body"><div class="form-grid">
          ${settingInput("history_lookback_days", "History read from Actual (days)", s.history_lookback_days, { type: "number", min: 30, max: 3650, note: "Feeds the memory, the recurring detection, and the income average." })}
          ${settingInput("balance_stale_hours", "Call a bank balance stale after (hours)", s.balance_stale_hours, { type: "number", min: 2, max: 720 })}
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

document.addEventListener("click", async (event) => {
  // A control that owns its own event lives inside a clickable row.
  if (event.target.closest("[data-stop]")) { event.stopPropagation(); return; }
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
  if (action === "sync-now") enqueue("sync", "Sync");
  if (action === "run-categorize") enqueue("categorize", "Filing");
  if (action === "catch-up") {
    if (!window.confirm("Go back over your whole retained history and categorize everything Clerk has not filed yet?\n\nThis asks the local model about each unfamiliar merchant once, so it can take a while on the first run.")) return;
    enqueue("categorize", "History catch-up", { full: true });
  }
  if (action === "check-connections") enqueue("health", "Connection check");
  if (action === "run-health") enqueue("health", "Connection check");
  if (action === "job-detail") showJob(target.dataset.id);
  if (action === "review-detail") showDecision(target.dataset.id);
  if (action === "account-detail") showAccount(target.dataset.id);
  if (action === "recurring-filter") { state.recurringFilter = target.dataset.filter; renderRecurring(); }
  if (action === "job-filter") { state.jobFilter = target.dataset.filter; renderActivity(); }

  if (action === "review-accept") {
    event.stopPropagation();
    const select = document.querySelector(`[data-role="review-category"][data-id="${CSS.escape(target.dataset.id)}"]`);
    const chosen = select?.value || "";
    await resolveReview(target.dataset.id, chosen ? "recategorize" : "accept", chosen || undefined);
  }
  if (action === "review-dismiss") { event.stopPropagation(); await resolveReview(target.dataset.id, "dismiss"); }

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
    try { await api(`/api/jobs/${target.dataset.id}/retry`, { method: "POST" }); toast("Run queued"); closeDrawer(); await renderRoute({ quiet: true }); }
    catch (error) { toast("Retry failed", error.message, "error"); }
  }
  if (action === "cancel-job") {
    event.stopPropagation();
    try { await api(`/api/jobs/${target.dataset.id}/cancel`, { method: "POST" }); toast("Run cancelled"); await renderRoute({ quiet: true }); }
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
document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeDrawer(); });
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
  const checks = ["actual_verify_ssl", "categorization_enabled", "ai_enabled", "rule_promotion_enabled", "tagging_enabled", "tag_provenance", "tag_cadence", "tag_anomalies", "allow_new_categories", "sync_enabled", "bank_sync_enabled", "digest_enabled", "notifications_enabled", "health_alerts_enabled"];

  for (const [key, value] of data.entries()) {
    if (key.startsWith("clear_") || key === "committed_groups") continue;
    values[key] = integers.has(key) ? Number.parseInt(value, 10) : decimals.has(key) ? Number.parseFloat(value) : value;
  }
  values.committed_groups = data.getAll("committed_groups");
  for (const key of checks) if (!locked.has(key)) values[key] = data.get(key) === "on";
  for (const key of ["actual_password", "actual_encryption_password", "simplefin_access_url", "openai_api_key", "ntfy_token"]) {
    if (data.get(`clear_${key}`) === "on") values[key] = "";
    else if (!values[key]) delete values[key];
  }

  const button = form.querySelector('[type="submit"]');
  button.disabled = true;
  try {
    const result = await api("/api/settings", { method: "PATCH", body: JSON.stringify({ values }) });
    toast("Settings saved", result.restart_required.length ? `Restart required for: ${result.restart_required.join(", ")}` : "New runs will use these values.");
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
    const query = event.target.value.toLowerCase();
    document.querySelectorAll("#decision-list .decision-row").forEach((row) => { row.hidden = !row.textContent.toLowerCase().includes(query); });
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

async function initialize() {
  try { state.settings = await api("/api/settings"); applyAppearance(state.settings); }
  catch { /* cached or system appearance remains active */ }
  try {
    state.health = await api("/api/health");
    document.querySelector("#app-version").textContent = `Actual Clerk ${state.health.version}`;
  } catch { /* the main route reports the error */ }
  await navigateFromHash();
  state.poll = setInterval(async () => {
    if (document.hidden || state.route === "settings" || drawer.classList.contains("open")) return;
    try { await renderRoute({ quiet: true }); } catch { /* keep the last good screen */ }
  }, 8000);
}

initialize();
