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
    // The object `init()` returns. Besides the documented methods it carries
    // `send`, which reaches every server handler -- the only route to
    // linking, unlinking, and server secrets. Actual exposes the same bridge
    // as the deprecated `api.internal`, kept here as the fallback.
    this.lib = null;
  }

  async _send(handler, args = {}) {
    const bridge = this.lib?.send ? this.lib : this.api.internal;
    if (typeof bridge?.send !== 'function') {
      throw new Error('The official Actual API did not expose its handler bridge');
    }
    return bridge.send(handler, args);
  }

  async initialize(params) {
    if (this.initialized) throw new Error('The Actual API worker is already initialized');
    this.lib = (await this.api.init({
      serverURL: params.serverUrl,
      password: params.password,
      dataDir: params.dataDir,
      verbose: false,
    })) || null;
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

  // ------------------------------------------------------------ bank links
  //
  // Everything below exists so Clerk can manage bank links itself: list what
  // Actual links today, detach an account, attach a SimpleFIN account back
  // onto an existing one, and import transactions from a provider Actual does
  // not know (Plaid). The internal handlers are reached through `_send`; the
  // package version is pinned exactly, and the worker contract tests pin the
  // handler names and argument shapes.

  async listAccountsDetailed() {
    // The AQL view of `accounts` hides the link columns (bank, balances), so
    // this reads Actual's own account handler, which joins the bank row.
    await this.api.sync();
    const rows = await this._send('accounts-get');
    return (rows || []).filter(row => !row.tombstone).map(row => ({
      id: cleanString(row.id),
      name: cleanString(row.name),
      off_budget: Boolean(row.offbudget),
      closed: Boolean(row.closed),
      sync_source: cleanString(row.account_sync_source),
      external_id: cleanString(row.account_id),
      official_name: cleanString(row.official_name),
      mask: cleanString(row.mask),
      bank_id: cleanString(row.bank),
      bank_name: cleanString(row.bankName),
      last_sync: cleanString(row.last_sync) || null,
      bank_sync_status: cleanString(row.bank_sync_status),
      balance_current: row.balance_current == null ? null : cleanInteger(row.balance_current),
      balance_available: row.balance_available == null ? null : cleanInteger(row.balance_available),
      balance_limit: row.balance_limit == null ? null : cleanInteger(row.balance_limit),
    }));
  }

  async _accountRow(accountId) {
    const id = cleanString(accountId);
    if (!id) throw new Error('An Actual account id is required');
    const rows = await this._query(this.api.q('accounts')
      .filter({ id })
      .select(['id', 'name', 'offbudget', 'closed', 'account_id', 'account_sync_source']));
    if (!rows.length) throw new Error(`Actual account ${id} was not found`);
    return rows[0];
  }

  async accountTransactions(params) {
    const accountId = cleanString(params.accountId);
    if (!accountId) throw new Error('An Actual account id is required');
    const filter = { account: accountId };
    const start = isoDate(params.start);
    const end = isoDate(params.end);
    if (start || end) {
      filter.date = {};
      if (start) filter.date.$gte = start;
      if (end) filter.date.$lte = end;
    }
    const rows = await this._query(this.api.q('transactions')
      .filter(filter)
      .select([
        'id',
        'date',
        { amount_cents: 'amount' },
        { payee_name: 'payee.name' },
        { imported_description: 'imported_payee' },
        'imported_id',
        'notes',
        { category_id: 'category' },
        'cleared',
        'reconciled',
        'transfer_id',
        'is_parent',
        'is_child',
        'starting_balance_flag',
      ]));
    return rows.map(row => ({
      id: cleanString(row.id),
      date: isoDate(row.date),
      amount_cents: cleanInteger(row.amount_cents),
      payee_name: cleanString(row.payee_name),
      imported_description: cleanString(row.imported_description),
      imported_id: cleanString(row.imported_id),
      notes: cleanString(row.notes),
      category_id: cleanString(row.category_id) || null,
      cleared: Boolean(row.cleared),
      reconciled: Boolean(row.reconciled),
      is_transfer: Boolean(row.transfer_id),
      is_parent: Boolean(row.is_parent),
      is_child: Boolean(row.is_child),
      is_starting_balance: Boolean(row.starting_balance_flag),
    }));
  }

  async unlinkAccount(params) {
    // Actual's own unlink: clears the link columns and leaves the account and
    // every transaction in place. Nothing is sent to SimpleFIN.
    const before = await this._accountRow(params.accountId);
    await this._send('account-unlink', { id: cleanString(before.id) });
    await this.api.sync();
    return {
      id: cleanString(before.id),
      name: cleanString(before.name),
      previous_sync_source: cleanString(before.account_sync_source),
      previous_external_id: cleanString(before.account_id),
    };
  }

  async simpleFinServerStatus() {
    const response = await this._send('simplefin-status');
    const data = response?.data ?? response ?? {};
    return {
      configured: Boolean(data.configured),
      error: cleanString(response?.error || data.error_code || ''),
    };
  }

  async simpleFinServerAccounts() {
    // The Actual server answers with SimpleFIN's own account shape. Reduce it
    // to what a link needs, and keep the raw org identity for `findOrCreateBank`.
    const response = await this._send('simplefin-accounts');
    const data = response?.data ?? response ?? {};
    if (data.error_code || data.error_type || response?.error) {
      return {
        accounts: [],
        error: cleanString(data.error_code || data.error_type || response.error),
        reason: cleanString(data.reason || ''),
      };
    }
    const accounts = (data.accounts || []).map(account => {
      const org = account.org || {};
      return {
        account_id: cleanString(account.id),
        name: cleanString(account.name),
        institution: cleanString(org.name),
        org_domain: cleanString(org.domain),
        org_id: cleanString(org.id || org['sfin-url']),
        balance: cleanString(account.balance),
        currency: cleanString(account.currency),
        balance_date: account['balance-date'] ?? null,
      };
    });
    return { accounts, error: '', reason: '' };
  }

  async linkSimpleFinAccount(params) {
    // Attach a SimpleFIN account to an existing Actual account (or create a
    // new one when no `accountId` is given). Actual immediately runs a first
    // sync from `startingDate`; an account that already holds transactions
    // gets no synthetic starting balance.
    const external = params.externalAccount || {};
    const externalAccount = {
      account_id: cleanString(external.account_id || external.id),
      name: cleanString(external.name),
      institution: cleanString(external.institution) || null,
      orgDomain: cleanString(external.org_domain || external.orgDomain) || null,
      orgId: cleanString(external.org_id || external.orgId) || null,
    };
    if (!externalAccount.account_id) throw new Error('A SimpleFIN account id is required to link');
    const upgradingId = cleanString(params.accountId) || undefined;
    if (upgradingId) await this._accountRow(upgradingId);
    const request = {
      externalAccount,
      upgradingId,
      offBudget: Boolean(params.offBudget),
    };
    const startingDate = isoDate(params.startingDate);
    if (startingDate) request.startingDate = startingDate;
    if (params.startingBalance != null) request.startingBalance = cleanInteger(params.startingBalance);
    await this._send('simplefin-accounts-link', request);
    await this.api.sync();
    const accounts = await this.listAccountsDetailed();
    const linked = accounts.find(account => upgradingId
      ? account.id === upgradingId
      : account.external_id === externalAccount.account_id && account.sync_source === 'simpleFin');
    return linked || { id: upgradingId || '', external_id: externalAccount.account_id, sync_source: 'simpleFin' };
  }

  async setServerSecret(params) {
    // Only the SimpleFIN token is ever managed from here. A null value
    // deletes the secret on the Actual server.
    const name = cleanString(params.name);
    if (name !== 'simplefin_token') throw new Error(`Refusing to manage server secret ${name || '(blank)'}`);
    const value = params.value == null ? null : cleanString(params.value);
    const response = await this._send('secret-set', { name, value });
    if (response?.error) {
      throw new Error(`Actual refused the ${name} secret: ${cleanString(response.reason || response.error)}`);
    }
    return { name, cleared: value === null };
  }

  async createAccount(params) {
    const name = cleanString(params.name).trim();
    if (!name) throw new Error('An account name is required');
    const initialBalance = params.initialBalance == null ? undefined : cleanInteger(params.initialBalance);
    const id = await this.api.createAccount({ name, offbudget: Boolean(params.offBudget) }, initialBalance);
    await this.api.sync();
    return { id: cleanString(id), name, off_budget: Boolean(params.offBudget) };
  }

  async importTransactions(params) {
    // Actual's own reconciliation: match by imported_id, then fuzzy, then run
    // rules. The caller decides what belongs in the batch; nothing here filters.
    const accountId = cleanString(params.accountId);
    if (!accountId) throw new Error('An Actual account id is required');
    const transactions = (params.transactions || []).map(item => {
      const amount = Number(item.amount_cents ?? item.amount);
      if (!Number.isInteger(amount)) throw new Error(`Import amount must be integer cents, got ${item.amount_cents ?? item.amount}`);
      const date = isoDate(item.date);
      if (!date) throw new Error('Import date is required');
      const transaction = { date, amount };
      if (item.payee_name != null) transaction.payee_name = cleanString(item.payee_name);
      if (item.imported_payee != null) transaction.imported_payee = cleanString(item.imported_payee);
      if (item.imported_id != null) transaction.imported_id = cleanString(item.imported_id);
      if (item.notes != null) transaction.notes = cleanString(item.notes);
      if (item.cleared != null) transaction.cleared = Boolean(item.cleared);
      if (item.category_id) transaction.category = cleanString(item.category_id);
      return transaction;
    });
    if (!transactions.length) return { added: [], updated: [], errors: [], preview: [], dry_run: Boolean(params.dryRun) };
    const dryRun = Boolean(params.dryRun);
    const result = await this.api.importTransactions(accountId, transactions, { defaultCleared: true, dryRun });
    if (!dryRun) await this.api.sync();
    return {
      added: (result?.added || []).map(cleanString),
      updated: (result?.updated || []).map(cleanString),
      errors: (result?.errors || []).map(error => cleanString(error?.message || error)),
      preview: result?.updatedPreview || [],
      dry_run: dryRun,
    };
  }

  async deleteTransactions(params) {
    const ids = [...new Set((params.transactionIds || []).map(cleanString).filter(Boolean))];
    if (!ids.length) return { deleted: [], missing: [] };
    const rows = await this._query(this.api.q('transactions').filter({ id: { $oneof: ids } }).select(['id']));
    const present = new Set(rows.map(row => cleanString(row.id)));
    const deleted = ids.filter(id => present.has(id));
    if (deleted.length) {
      await this.api.batchBudgetUpdates(async () => {
        for (const id of deleted) await this.api.deleteTransaction(id);
      });
      await this.api.sync();
    }
    return { deleted, missing: ids.filter(id => !present.has(id)) };
  }

  async adoptImportedIds(params) {
    // Give an existing row a new provider id so later updates and removals
    // find it. Optionally settles the cleared flag and date at the same time,
    // which is what a pending-to-posted swap needs.
    const updates = params.updates || [];
    const ids = [...new Set(updates.map(update => cleanString(update.transaction_id)).filter(Boolean))];
    if (!ids.length) return { applied: [], skipped: [] };
    const rows = await this._query(this.api.q('transactions')
      .filter({ id: { $oneof: ids } })
      .select(['id', 'imported_id', 'cleared', 'date']));
    const existing = new Map(rows.map(row => [cleanString(row.id), row]));
    const planned = [];
    const skipped = [];
    for (const update of updates) {
      const id = cleanString(update.transaction_id);
      const row = existing.get(id);
      if (!row) {
        skipped.push({ id, reason: 'deleted' });
        continue;
      }
      const fields = {};
      const importedId = cleanString(update.imported_id);
      if (importedId && importedId !== cleanString(row.imported_id)) fields.imported_id = importedId;
      if (update.cleared != null && Boolean(update.cleared) !== Boolean(row.cleared)) fields.cleared = Boolean(update.cleared);
      const date = isoDate(update.date);
      if (date && date !== isoDate(row.date)) fields.date = date;
      if (!Object.keys(fields).length) {
        skipped.push({ id, reason: 'no_change' });
        continue;
      }
      planned.push({ id, fields, previous_imported_id: cleanString(row.imported_id) });
    }
    if (planned.length) {
      await this.api.batchBudgetUpdates(async () => {
        for (const update of planned) await this.api.updateTransaction(update.id, update.fields);
      });
      await this.api.sync();
    }
    return {
      applied: planned.map(update => ({ id: update.id, previous_imported_id: update.previous_imported_id, fields: update.fields })),
      skipped,
    };
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
      listAccountsDetailed: () => this.listAccountsDetailed(),
      accountTransactions: () => this.accountTransactions(params),
      unlinkAccount: () => this.unlinkAccount(params),
      simpleFinServerStatus: () => this.simpleFinServerStatus(),
      simpleFinServerAccounts: () => this.simpleFinServerAccounts(),
      linkSimpleFinAccount: () => this.linkSimpleFinAccount(params),
      setServerSecret: () => this.setServerSecret(params),
      createAccount: () => this.createAccount(params),
      importTransactions: () => this.importTransactions(params),
      deleteTransactions: () => this.deleteTransactions(params),
      adoptImportedIds: () => this.adoptImportedIds(params),
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
