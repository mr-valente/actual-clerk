import { pathToFileURL } from 'node:url';
import readline from 'node:readline';

export const RPC_PREFIX = '@@actual-clerk-rpc@@';
export const ACTUAL_API_VERSION = '26.9.0';

function isoDate(value) {
  if (!value) return '';
  return String(value).slice(0, 10);
}

function addDays(value, days) {
  const date = new Date(`${isoDate(value)}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

function monthOf(value) {
  return isoDate(value).slice(0, 7);
}

function queryRows(result) {
  return Array.isArray(result?.data) ? result.data : [];
}

function cleanString(value) {
  return value == null ? '' : String(value);
}

function cleanInteger(value) {
  const number = Number(value || 0);
  return Number.isFinite(number) ? Math.trunc(number) : 0;
}

function errorCode(error) {
  return cleanString(error?.code || error?.cause?.code || error?.name || 'actual-api-error');
}

export function applyTags(notes, tags) {
  const base = cleanString(notes);
  const existing = new Set();
  for (const match of base.matchAll(/#([^\s#]+)/g)) {
    const tag = match[1].replace(/[.,;:!?]+$/, '');
    if (tag) existing.add(tag.toLocaleLowerCase());
  }

  const additions = [];
  for (const value of tags || []) {
    const tag = cleanString(value).trim().replace(/^#+/, '');
    if (!tag || /[\s#]/.test(tag) || existing.has(tag.toLocaleLowerCase())) continue;
    existing.add(tag.toLocaleLowerCase());
    additions.push(`#${tag}`);
  }
  if (!additions.length) return base;
  return `${base.trimEnd()} ${additions.join(' ')}`.trim();
}

export function findUnconfirmedTransfers(rows) {
  const byId = new Map(rows.map(row => [cleanString(row.id), row]));
  const result = {};
  for (const row of rows) {
    if (!row.cleared || cleanString(row.imported_id) || !cleanString(row.transfer_id)) continue;
    const counterpart = byId.get(cleanString(row.transfer_id));
    if (!counterpart || !cleanString(counterpart.imported_id)) continue;
    const accountId = cleanString(row.account_id);
    if (!accountId || !row.date) continue;
    (result[accountId] ||= []).push({
      id: cleanString(row.id),
      date: isoDate(row.date),
      amount_cents: cleanInteger(row.amount_cents),
    });
  }
  for (const candidates of Object.values(result)) {
    candidates.sort((left, right) => right.date.localeCompare(left.date) || left.id.localeCompare(right.id));
  }
  return result;
}

export function transactionDiff(beforeRows, afterRows) {
  const fingerprint = row => JSON.stringify([
    row.date,
    row.amount,
    row.payee,
    row.category,
    row.notes,
    row.imported_id,
    row.transfer_id,
    row.cleared,
  ]);
  const before = new Map(beforeRows.map(row => [cleanString(row.id), fingerprint(row)]));
  const added = [];
  const updated = [];
  for (const row of afterRows) {
    const id = cleanString(row.id);
    if (!before.has(id)) added.push(id);
    else if (before.get(id) !== fingerprint(row)) updated.push(id);
  }
  return { added, updated };
}

function flattenBudgetMonth(month) {
  const values = {};
  for (const group of month?.categoryGroups || []) {
    for (const category of group.categories || []) {
      const amount = cleanInteger(category.budgeted);
      if (amount !== 0) values[cleanString(category.id)] = amount;
    }
  }
  return values;
}

export function assembleSnapshot({
  today,
  historyStart,
  accounts,
  accountMetadata,
  groups,
  categories,
  transactionRows,
  reviewTransactionRows = [],
  balanceRows,
  clearedBalanceRows,
  transferRows,
  budgetMonths,
  tags,
  collectedAt,
}) {
  const groupById = new Map(groups.map(group => [cleanString(group.id), group]));
  const accountById = new Map(accounts.map(account => [cleanString(account.id), account]));
  const metadataById = new Map(accountMetadata.map(row => [cleanString(row.id), row]));
  const totals = new Map(balanceRows.map(row => [cleanString(row.account_id), cleanInteger(row.balance_cents)]));
  const cleared = new Map(clearedBalanceRows.map(row => [cleanString(row.account_id), cleanInteger(row.balance_cents)]));
  const unconfirmed = findUnconfirmedTransfers(transferRows);

  const categoryList = categories.map(category => {
    const groupId = cleanString(category.group_id ?? category.group);
    const group = groupById.get(groupId) || {};
    return {
      id: cleanString(category.id),
      name: cleanString(category.name),
      group_id: groupId,
      group_name: cleanString(group.name),
      is_income: Boolean(category.is_income || group.is_income),
      hidden: Boolean(category.hidden),
    };
  });

  const mapTransaction = row => {
    const accountId = cleanString(row.account_id);
    const account = accountById.get(accountId) || {};
    return {
      id: cleanString(row.id),
      date: isoDate(row.date),
      amount_cents: cleanInteger(row.amount_cents),
      category_id: cleanString(row.category_id) || null,
      category_name: cleanString(row.category_name),
      payee_name: cleanString(row.payee_name),
      imported_description: cleanString(row.imported_description),
      notes: cleanString(row.notes),
      account_id: accountId,
      account_name: cleanString(account.name),
      off_budget: Boolean(account.offbudget),
      closed_account: Boolean(account.closed),
      is_transfer: Boolean(row.transfer_id),
      is_child: Boolean(row.is_child),
      is_starting_balance: Boolean(row.starting_balance_flag),
      cleared: Boolean(row.cleared),
      pending: false,
      imported_id: cleanString(row.imported_id),
      schedule_id: cleanString(row.schedule_id) || null,
    };
  };
  const transactions = transactionRows.map(mapTransaction);
  const reviewTransactions = reviewTransactionRows.map(mapTransaction);

  const lastTransaction = new Map();
  for (const transaction of transactions) {
    const prior = lastTransaction.get(transaction.account_id);
    if (!prior || transaction.date > prior) lastTransaction.set(transaction.account_id, transaction.date);
  }

  const accountList = accounts.map(account => {
    const id = cleanString(account.id);
    const meta = metadataById.get(id) || {};
    const total = totals.get(id) ?? cleanInteger(account.balance_current);
    const clearedTotal = cleared.get(id) ?? 0;
    return {
      id,
      name: cleanString(account.name),
      sync_source: cleanString(meta.account_sync_source),
      external_id: cleanString(meta.account_id),
      bank_name: cleanString(meta.official_name),
      balance_cents: total,
      cleared_balance_cents: clearedTotal,
      uncleared_balance_cents: total - clearedTotal,
      unconfirmed_transfers: unconfirmed[id] || [],
      last_sync: cleanString(meta.last_sync) || null,
      off_budget: Boolean(account.offbudget),
      closed: Boolean(account.closed),
      type: '',
      last_transaction_date: lastTransaction.get(id) || null,
    };
  });

  const budgetedHistory = {};
  for (const [month, data] of Object.entries(budgetMonths)) {
    budgetedHistory[month] = flattenBudgetMonth(data);
  }

  const incomeIds = new Set(categoryList.filter(category => category.is_income).map(category => category.id));
  const income = new Map();
  for (const transaction of transactions) {
    if (!incomeIds.has(transaction.category_id) || transaction.off_budget || transaction.is_transfer) continue;
    const month = monthOf(transaction.date);
    income.set(month, (income.get(month) || 0) + Math.max(0, transaction.amount_cents));
  }

  return {
    accounts: accountList,
    categories: categoryList,
    groups: groups.map(group => ({
      id: cleanString(group.id),
      name: cleanString(group.name),
      is_income: Boolean(group.is_income),
      hidden: Boolean(group.hidden),
    })),
    budgeted: budgetedHistory[monthOf(today)] || {},
    budgeted_history: budgetedHistory,
    transactions,
    review_transactions: reviewTransactions,
    income_history: [...income.entries()].sort().map(([month, cents]) => [`${month}-01`, cents]),
    tags: tags.map(tag => ({
      tag: cleanString(tag.tag),
      color: cleanString(tag.color),
      description: cleanString(tag.description),
    })),
    collected_at: collectedAt,
    history_start: historyStart,
  };
}

export class ActualService {
  constructor(api) {
    this.api = api;
    this.initialized = false;
    this.loadedBudget = null;
    this.serverVersion = '';
  }

  async initialize(params) {
    if (this.initialized) throw new Error('The Actual API worker is already initialized');
    await this.api.init({
      serverURL: params.serverUrl,
      password: params.password,
      dataDir: params.dataDir,
      verbose: false,
    });
    this.initialized = true;

    let budgets = await this.api.getBudgets();
    let local = budgets.find(file => file?.id && (file.groupId === params.syncId || file.cloudFileId === params.syncId));
    if (local) {
      try {
        await this.api.loadBudget(local.id);
      } catch {
        local = null;
      }
    }
    if (!local) {
      await this.api.downloadBudget(params.syncId, { password: params.encryptionPassword || undefined });
      budgets = await this.api.getBudgets();
      local = budgets.find(file => file?.id && (file.groupId === params.syncId || file.cloudFileId === params.syncId));
    }
    if (!local?.id) throw new Error(`Actual downloaded sync ID ${params.syncId} but did not expose a local budget`);

    this.loadedBudget = local;
    await this.api.sync();
    try {
      const version = await this.api.getServerVersion();
      this.serverVersion = cleanString(version?.version || version);
    } catch {
      this.serverVersion = '';
    }
    return {
      budgetId: local.id,
      budgetName: cleanString(local.name),
      serverVersion: this.serverVersion,
      apiVersion: ACTUAL_API_VERSION,
    };
  }

  async shutdown() {
    if (this.initialized) await this.api.shutdown();
    this.initialized = false;
    return { shutdown: true };
  }

  async _query(query) {
    return queryRows(await this.api.aqlQuery(query));
  }

  async _accountMetadata() {
    return this._query(this.api.q('accounts').select([
      'id',
      'account_id',
      'official_name',
      'account_sync_source',
      'last_sync',
      'bank_sync_status',
    ]));
  }

  async _balanceRows(clearedOnly = false) {
    let query = this.api.q('transactions');
    if (clearedOnly) query = query.filter({ cleared: true });
    const rows = await this._query(query
      .groupBy('account')
      .select(['account', { balance_cents: { $sum: '$amount' } }]));
    return rows.map(row => ({ ...row, account_id: row.account }));
  }

  async _transferRows() {
    return this._query(this.api.q('transactions')
      .filter({ transfer_id: { $ne: null } })
      .select([
        'id',
        { account_id: 'account' },
        'date',
        { amount_cents: 'amount' },
        'cleared',
        'imported_id',
        'transfer_id',
      ]));
  }

  async _transactionRows(historyStart, today) {
    return this._query(this.api.q('transactions')
      .filter({ date: { $gte: historyStart, $lte: today } })
      .select([
        'id',
        'date',
        { amount_cents: 'amount' },
        { category_id: 'category' },
        { category_name: 'category.name' },
        { payee_name: 'payee.name' },
        { imported_description: 'imported_payee' },
        'notes',
        { account_id: 'account' },
        'transfer_id',
        'is_child',
        'starting_balance_flag',
        'cleared',
        'imported_id',
        { schedule_id: 'schedule' },
      ]));
  }

  async _reviewTransactionRows(transactionIds) {
    const ids = [...new Set((transactionIds || []).map(cleanString).filter(Boolean))];
    if (!ids.length) return [];
    return this._query(this.api.q('transactions')
      .filter({ id: { $oneof: ids } })
      .select([
        'id',
        'date',
        { amount_cents: 'amount' },
        { category_id: 'category' },
        { category_name: 'category.name' },
        { payee_name: 'payee.name' },
        { imported_description: 'imported_payee' },
        'notes',
        { account_id: 'account' },
        'transfer_id',
        'is_child',
        'starting_balance_flag',
        'cleared',
        'imported_id',
        { schedule_id: 'schedule' },
      ]));
  }

  async _budgetMonths(historyStart, today) {
    const available = await this.api.getBudgetMonths();
    const first = monthOf(historyStart);
    const last = monthOf(today);
    const wanted = available.filter(month => month >= first && month <= last);
    if (!wanted.includes(last)) wanted.push(last);
    const result = {};
    for (const month of [...new Set(wanted)].sort()) {
      result[month] = await this.api.getBudgetMonth(month);
    }
    return result;
  }

  async snapshot(params) {
    await this.api.sync();
    const today = isoDate(params.today);
    const historyStart = addDays(today, -cleanInteger(params.historyLookbackDays));
    const accounts = await this.api.getAccounts();
    const groups = await this.api.getCategoryGroups();
    const categories = await this.api.getCategories();
    const transactionRows = await this._transactionRows(historyStart, today);
    const reviewTransactionRows = await this._reviewTransactionRows(params.transactionIds);
    const accountMetadata = await this._accountMetadata();
    const balanceRows = await this._balanceRows(false);
    const clearedBalanceRows = await this._balanceRows(true);
    const transferRows = await this._transferRows();
    const budgetMonths = await this._budgetMonths(historyStart, today);
    const tags = await this.api.getTags();
    return assembleSnapshot({
      today,
      historyStart,
      accounts,
      accountMetadata,
      groups,
      categories,
      transactionRows,
      reviewTransactionRows,
      balanceRows,
      clearedBalanceRows,
      transferRows,
      budgetMonths,
      tags,
      collectedAt: new Date().toISOString(),
    });
  }

  async _transactionLedger() {
    return this._query(this.api.q('transactions').select([
      'id',
      'date',
      'account',
      'amount',
      'payee',
      'category',
      'notes',
      'imported_id',
      'transfer_id',
      'cleared',
    ]));
  }

  async bankSync() {
    await this.api.sync();
    const accounts = await this.api.getAccounts();
    const before = await this._transactionLedger();
    await this.api.runBankSync();
    await this.api.sync();
    const after = await this._transactionLedger();
    const diff = transactionDiff(before, after);
    const afterById = new Map(after.map(row => [cleanString(row.id), row]));
    const accountById = new Map(accounts.map(account => [cleanString(account.id), cleanString(account.name)]));
    const touchedAccounts = new Set();
    for (const id of [...diff.added, ...diff.updated]) {
      const accountId = cleanString(afterById.get(id)?.account);
      if (accountById.has(accountId)) touchedAccounts.add(accountById.get(accountId));
    }
    return {
      imported: diff.added.length + diff.updated.length,
      new: diff.added.length,
      updated: diff.updated.length,
      accounts: [...touchedAccounts].sort(),
    };
  }

  async pull() {
    await this.api.sync();
    return { changes: 0 };
  }

  async testConnection() {
    await this.api.sync();
    const accounts = await this.api.getAccounts();
    return {
      ok: true,
      message: `${accounts.length} account(s) in ${cleanString(this.loadedBudget?.name) || 'the budget'}`,
      accounts: accounts.length,
      budget_name: cleanString(this.loadedBudget?.name),
      server_version: this.serverVersion,
      api_version: ACTUAL_API_VERSION,
    };
  }

  async applyUpdates(params) {
    const updates = params.updates || [];
    if (!updates.length) return { applied: [], skipped: [] };
    const categories = await this.api.getCategories();
    const liveCategories = new Set(categories.map(category => cleanString(category.id)));
    const ids = [...new Set(updates.map(update => cleanString(update.transaction_id)).filter(Boolean))];
    const rows = await this._query(this.api.q('transactions')
      .filter({ id: { $oneof: ids } })
      .select(['id', 'category', 'notes']));
    const transactions = new Map(rows.map(row => [cleanString(row.id), row]));
    const planned = [];
    const skipped = [];

    for (const update of updates) {
      const id = cleanString(update.transaction_id);
      const transaction = transactions.get(id);
      if (!transaction) {
        skipped.push({ id, reason: 'deleted' });
        continue;
      }
      const categoryId = cleanString(update.category_id);
      if (categoryId && !liveCategories.has(categoryId)) {
        skipped.push({ id, reason: 'unknown_category' });
        continue;
      }
      if (categoryId && transaction.category && transaction.category !== categoryId && !params.overwrite) {
        skipped.push({ id, reason: 'already_categorized' });
        continue;
      }
      const fields = {};
      if (categoryId && transaction.category !== categoryId) fields.category = categoryId;
      const notes = applyTags(transaction.notes, update.add_tags || []);
      if (notes !== cleanString(transaction.notes)) fields.notes = notes;
      if (!Object.keys(fields).length) {
        skipped.push({ id, reason: 'no_change' });
        continue;
      }
      planned.push({ id, fields });
    }

    if (planned.length) {
      await this.api.batchBudgetUpdates(async () => {
        for (const update of planned) await this.api.updateTransaction(update.id, update.fields);
      });
      await this.api.sync();
    }
    return { applied: planned.map(update => update.id), skipped };
  }

  async ensureTags(params) {
    const existing = new Set((await this.api.getTags()).map(tag => cleanString(tag.tag).toLocaleLowerCase()));
    const create = [];
    for (const entry of params.catalog || []) {
      const name = cleanString(entry.tag).trim();
      if (!name || existing.has(name.toLocaleLowerCase())) continue;
      existing.add(name.toLocaleLowerCase());
      create.push({
        tag: name,
        color: cleanString(entry.color) || '#690cb0',
        description: cleanString(entry.description),
      });
    }
    if (create.length) {
      await this.api.batchBudgetUpdates(async () => {
        for (const tag of create) await this.api.createTag(tag);
      });
      await this.api.sync();
    }
    return create.map(tag => tag.tag);
  }

  async createCategory(params) {
    const requestedGroup = cleanString(params.groupName).trim();
    let group = (await this.api.getCategoryGroups()).find(
      item => cleanString(item.name).toLocaleLowerCase() === requestedGroup.toLocaleLowerCase(),
    );
    if (!group) {
      const id = await this.api.createCategoryGroup({ name: requestedGroup });
      group = { id, name: requestedGroup };
    }
    const id = await this.api.createCategory({ name: cleanString(params.name).trim(), group_id: group.id });
    await this.api.sync();
    return { id, name: cleanString(params.name).trim(), group_name: cleanString(group.name) };
  }

  async createCategoryRule(params) {
    if (params.runImmediately) throw new Error('The official Actual API cannot run a newly created rule retroactively');
    const category = (await this.api.getCategories()).find(item => item.id === params.categoryId);
    if (!category) throw new Error(`Category ${params.categoryId} no longer exists`);
    const id = await this.api.createRule({
      stage: 'default',
      conditionsOp: 'and',
      conditions: [{ field: 'payee', op: 'contains', value: params.matchValue }],
      actions: [{ op: 'set', field: 'category', value: params.categoryId }],
    });
    await this.api.sync();
    return { id, match_value: params.matchValue, category_id: params.categoryId };
  }

  async findAccount(params) {
    const requested = cleanString(params.name).trim().toLocaleLowerCase();
    const account = (await this.api.getAccounts()).find(
      item => cleanString(item.name).trim().toLocaleLowerCase() === requested,
    );
    return account ? { id: account.id, name: account.name } : null;
  }

  async diagnostics() {
    const preferenceRows = await this._query(this.api.q('preferences').filter({ id: 'budgetType' }).select(['id', 'value']));
    const budgetType = preferenceRows[0]?.value ?? null;
    const isTracking = budgetType === 'report' || budgetType === 'tracking';
    const tables = {};
    for (const table of ['zero_budgets', 'reflect_budgets']) {
      const rows = await this._query(this.api.q(table).select(['amount']));
      tables[table] = {
        rows: rows.length,
        non_zero: rows.filter(row => cleanInteger(row.amount) !== 0).length,
        total_cents: rows.reduce((sum, row) => sum + cleanInteger(row.amount), 0),
      };
    }
    return {
      budget_rows: null,
      redirects: {},
      redirect_map: {},
      deleted_categories: {},
      budget_type_preference: budgetType,
      reads_table: isTracking ? 'reflect_budgets' : 'zero_budgets',
      is_tracking: isTracking,
      tables,
      budget_name: cleanString(this.loadedBudget?.name),
      budget_id: cleanString(this.loadedBudget?.groupId || this.loadedBudget?.cloudFileId),
      server_version: this.serverVersion,
      api_version: ACTUAL_API_VERSION,
    };
  }

  async dispatch(method, params = {}) {
    const methods = {
      initialize: () => this.initialize(params),
      shutdown: () => this.shutdown(),
      snapshot: () => this.snapshot(params),
      bankSync: () => this.bankSync(),
      pull: () => this.pull(),
      testConnection: () => this.testConnection(),
      applyUpdates: () => this.applyUpdates(params),
      ensureTags: () => this.ensureTags(params),
      createCategory: () => this.createCategory(params),
      createCategoryRule: () => this.createCategoryRule(params),
      findAccount: () => this.findAccount(params),
      diagnostics: () => this.diagnostics(),
    };
    if (!methods[method]) throw new Error(`Unknown Actual worker method: ${method}`);
    if (method !== 'initialize' && !this.initialized) throw new Error('The Actual API worker is not initialized');
    return methods[method]();
  }
}

export async function runRpcLoop(service, input = process.stdin, output = process.stdout) {
  const lines = readline.createInterface({ input, crlfDelay: Infinity });
  for await (const line of lines) {
    let request;
    try {
      request = JSON.parse(line);
      const result = await service.dispatch(request.method, request.params || {});
      output.write(`${RPC_PREFIX}${JSON.stringify({ id: request.id, ok: true, result })}\n`);
      if (request.method === 'shutdown') break;
    } catch (error) {
      output.write(`${RPC_PREFIX}${JSON.stringify({
        id: request?.id ?? null,
        ok: false,
        error: {
          name: cleanString(error?.name || 'Error'),
          message: cleanString(error?.message || error),
          code: errorCode(error),
        },
      })}\n`);
    }
  }
}

async function main() {
  // Actual's internals occasionally use console output. Keep stdout reserved
  // for the framed RPC protocol so one log line can never corrupt a response.
  console.log = (...values) => process.stderr.write(`${values.map(String).join(' ')}\n`);
  console.info = console.log;
  console.warn = (...values) => process.stderr.write(`${values.map(String).join(' ')}\n`);
  const api = await import('@actual-app/api');
  await runRpcLoop(new ActualService(api));
}

const entrypoint = process.argv[1] ? pathToFileURL(process.argv[1]).href : '';
if (import.meta.url === entrypoint) {
  main().catch(error => {
    process.stderr.write(`Actual API worker failed: ${error?.stack || error}\n`);
    process.exitCode = 1;
  });
}
