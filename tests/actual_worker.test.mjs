import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import {
  ACTUAL_API_VERSION,
  ActualService,
  applyTags,
  assembleSnapshot,
  findUnconfirmedTransfers,
  transactionDiff,
} from '../src/actual_clerk/actual_worker.mjs';

class Query {
  filter() { return this; }
  select() { return this; }
  groupBy() { return this; }
}

test('the worker and npm lock stay on the same exact official API release', async () => {
  const manifest = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'));
  const lock = JSON.parse(await readFile(new URL('../package-lock.json', import.meta.url), 'utf8'));
  assert.equal(manifest.dependencies['@actual-app/api'], ACTUAL_API_VERSION);
  assert.equal(lock.packages['node_modules/@actual-app/api'].version, ACTUAL_API_VERSION);
});

test('notes keep their text and receive tags idempotently', () => {
  const once = applyTags('Coffee run #Clerk', ['clerk', 'subscription']);
  assert.equal(once, 'Coffee run #Clerk #subscription');
  assert.equal(applyTags(once, ['subscription']), once);
  assert.equal(applyTags('note', ['two words', '']), 'note');
});

test('only asymmetric imported transfer halves are held out', () => {
  const rows = [
    { id: 'source', account_id: 'wf', transfer_id: 'generated', imported_id: 'SF-WF', cleared: true, date: '2026-08-31', amount_cents: -2500 },
    { id: 'generated', account_id: 'co', transfer_id: 'source', imported_id: '', cleared: true, date: '2026-08-31', amount_cents: 2500 },
    { id: 'manual-a', account_id: 'wf', transfer_id: 'manual-b', imported_id: '', cleared: true, date: '2026-08-30', amount_cents: -1000 },
    { id: 'manual-b', account_id: 'co', transfer_id: 'manual-a', imported_id: '', cleared: true, date: '2026-08-30', amount_cents: 1000 },
  ];
  assert.deepEqual(findUnconfirmedTransfers(rows), {
    co: [{ id: 'generated', date: '2026-08-31', amount_cents: 2500 }],
  });
});

test('the snapshot uses Actual-resolved category and payee mappings', () => {
  const snapshot = assembleSnapshot({
    today: '2026-09-01',
    historyStart: '2026-08-01',
    accounts: [{ id: 'acct', name: 'Checking', balance_current: 6500, offbudget: false, closed: false }],
    accountMetadata: [{ id: 'acct', account_sync_source: 'simpleFin', account_id: 'remote-1', last_sync: '2026-09-01T10:00:00Z' }],
    groups: [{ id: 'group', name: 'Bills', is_income: false, hidden: false }],
    categories: [{ id: 'cat-new', name: 'Rent', group_id: 'group', is_income: false, hidden: false }],
    transactionRows: [{ id: 'txn', date: '2026-08-31', amount_cents: -3500, category_id: 'cat-new', category_name: 'Rent', payee_name: 'Landlord', account_id: 'acct', cleared: false }],
    reviewTransactionRows: [{ id: 'review-transfer', date: '2026-09-01', amount_cents: -50000, payee_name: 'Transfer', account_id: 'acct', transfer_id: 'other-half', cleared: true }],
    balanceRows: [{ account_id: 'acct', balance_cents: 6500 }],
    clearedBalanceRows: [{ account_id: 'acct', balance_cents: 10000 }],
    transferRows: [],
    budgetMonths: { '2026-09': { categoryGroups: [{ categories: [{ id: 'cat-new', budgeted: 120000 }] }] } },
    tags: [],
    collectedAt: '2026-09-01T12:00:00Z',
  });
  assert.equal(snapshot.transactions[0].category_id, 'cat-new');
  assert.equal(snapshot.transactions[0].category_name, 'Rent');
  assert.equal(snapshot.transactions[0].payee_name, 'Landlord');
  assert.equal(snapshot.review_transactions[0].id, 'review-transfer');
  assert.equal(snapshot.review_transactions[0].is_transfer, true);
  assert.equal(snapshot.accounts[0].balance_cents, 6500);
  assert.equal(snapshot.accounts[0].cleared_balance_cents, 10000);
  assert.equal(snapshot.accounts[0].uncleared_balance_cents, -3500);
  assert.deepEqual(snapshot.budgeted, { 'cat-new': 120000 });
});

test('native bank sync is called and reports new and reconciled rows', async () => {
  const before = [{ id: 'old', account: 'acct', date: '2026-08-30', amount: -100, payee: 'p1', category: null, notes: '', imported_id: 'bank-1', transfer_id: null, cleared: true }];
  const after = [
    { ...before[0], payee: 'p2' },
    { id: 'new', account: 'acct', date: '2026-09-01', amount: -200, payee: 'p3', category: null, notes: '', imported_id: 'bank-2', transfer_id: null, cleared: true },
  ];
  let queryNumber = 0;
  let bankSyncs = 0;
  let syncs = 0;
  const api = {
    q: () => new Query(),
    aqlQuery: async () => ({ data: queryNumber++ === 0 ? before : after }),
    getAccounts: async () => [{ id: 'acct', name: 'Checking' }],
    runBankSync: async () => { bankSyncs += 1; },
    sync: async () => { syncs += 1; },
  };
  const service = new ActualService(api);
  service.initialized = true;
  assert.deepEqual(await service.bankSync(), {
    imported: 2,
    new: 1,
    updated: 1,
    accounts: ['Checking'],
  });
  assert.equal(bankSyncs, 1);
  assert.equal(syncs, 2, 'pull before bank sync and push afterward');
});

test('batched writes validate every category and preserve live notes', async () => {
  const written = [];
  let batches = 0;
  let syncs = 0;
  const api = {
    q: () => new Query(),
    getCategories: async () => [{ id: 'cat-live' }],
    aqlQuery: async () => ({ data: [
      { id: 'open', category: null, notes: 'User note' },
      { id: 'occupied', category: 'cat-other', notes: '' },
    ] }),
    batchBudgetUpdates: async callback => { batches += 1; await callback(); },
    updateTransaction: async (id, fields) => { written.push({ id, fields }); },
    sync: async () => { syncs += 1; },
  };
  const service = new ActualService(api);
  service.initialized = true;
  const result = await service.applyUpdates({
    overwrite: false,
    updates: [
      { transaction_id: 'open', category_id: 'cat-live', add_tags: ['clerk'] },
      { transaction_id: 'occupied', category_id: 'cat-live' },
      { transaction_id: 'open', category_id: 'cat-gone' },
      { transaction_id: 'deleted', category_id: 'cat-live' },
    ],
  });
  assert.deepEqual(written, [{ id: 'open', fields: { category: 'cat-live', notes: 'User note #clerk' } }]);
  assert.deepEqual(result, {
    applied: ['open'],
    skipped: [
      { id: 'occupied', reason: 'already_categorized' },
      { id: 'open', reason: 'unknown_category' },
      { id: 'deleted', reason: 'deleted' },
    ],
  });
  assert.equal(batches, 1);
  assert.equal(syncs, 1);
});

test('promoted category rules match the Actual payee', async () => {
  const rules = [];
  let syncs = 0;
  const api = {
    getCategories: async () => [{ id: 'cat-coffee' }],
    createRule: async rule => { rules.push(rule); return 'rule-1'; },
    sync: async () => { syncs += 1; },
  };
  const service = new ActualService(api);
  service.initialized = true;

  assert.deepEqual(await service.createCategoryRule({
    matchValue: 'Blue Bottle',
    categoryId: 'cat-coffee',
    runImmediately: false,
  }), {
    id: 'rule-1',
    match_value: 'Blue Bottle',
    category_id: 'cat-coffee',
  });
  assert.deepEqual(rules, [{
    stage: 'default',
    conditionsOp: 'and',
    conditions: [{ field: 'payee', op: 'contains', value: 'Blue Bottle' }],
    actions: [{ op: 'set', field: 'category', value: 'cat-coffee' }],
  }]);
  assert.equal(syncs, 1);
});

test('rules and payees are read in Clerk\'s shape, and a rule can be retired and restored', async () => {
  const deleted = [];
  const created = [];
  let syncs = 0;
  const api = {
    sync: async () => { syncs += 1; },
    getRules: async () => [
      { id: 'r1', stage: null, conditionsOp: 'and', conditions: [{ op: 'is', field: 'payee', value: 'p1', type: 'id' }], actions: [{ op: 'set', field: 'category', value: 'c1', type: 'id' }] },
      { id: 'r2', stage: 'pre', conditionsOp: 'or', conditions: 'not-a-list', actions: [{ op: 'delete-transaction' }] },
    ],
    getPayees: async () => [{ id: 'p1', name: 'Blue Bottle', transfer_acct: null }, { id: 'p2', name: '', transfer_acct: 'acct-1' }],
    deleteRule: async id => { deleted.push(id); return true; },
    createRule: async rule => { created.push(rule); return { id: 'r-new' }; },
  };
  const service = new ActualService(api);
  service.initialized = true;

  assert.deepEqual(await service.listRules(), [
    { id: 'r1', stage: 'default', conditions_op: 'and', conditions: [{ op: 'is', field: 'payee', value: 'p1', type: 'id' }], actions: [{ op: 'set', field: 'category', value: 'c1', type: 'id' }] },
    { id: 'r2', stage: 'pre', conditions_op: 'or', conditions: [], actions: [{ op: 'delete-transaction' }] },
  ]);
  assert.deepEqual(await service.listPayees(), [
    { id: 'p1', name: 'Blue Bottle', transfer_account_id: '' },
    { id: 'p2', name: '', transfer_account_id: 'acct-1' },
  ]);
  assert.deepEqual(await service.deleteRule({ ruleId: 'r1' }), { id: 'r1', deleted: true });
  assert.deepEqual(deleted, ['r1']);
  await assert.rejects(() => service.deleteRule({}), /rule id is required/);

  const stored = { id: 'r1', stage: 'default', conditions_op: 'and', conditions: [{ op: 'is', field: 'payee', value: 'p1', type: 'id' }], actions: [{ op: 'set', field: 'category', value: 'c1', type: 'id' }] };
  assert.deepEqual(await service.restoreRule({ rule: stored }), { id: 'r-new', previous_id: 'r1' });
  assert.deepEqual(created, [{ stage: 'default', conditionsOp: 'and', conditions: stored.conditions, actions: stored.actions }]);
  await assert.rejects(() => service.restoreRule({ rule: { conditions: [] } }), /conditions and actions/);
  // One sync to read, one after the delete, one after the restore.
  assert.equal(syncs, 3);
});

test('transaction change reporting distinguishes additions from reconciliations', () => {
  const before = [{ id: 'one', amount: 100, date: '2026-09-01' }];
  const after = [{ id: 'one', amount: 200, date: '2026-09-01' }, { id: 'two', amount: 50, date: '2026-09-01' }];
  assert.deepEqual(transactionDiff(before, after), { added: ['two'], updated: ['one'] });
});

// ------------------------------------------------------------ bank links

function linkApi(overrides = {}) {
  const sent = [];
  const api = {
    q: () => new Query(),
    aqlQuery: async () => ({ data: [] }),
    sync: async () => {},
    internal: { send: async (handler, args) => { sent.push({ handler, args }); return overrides.reply?.(handler, args) ?? 'ok'; } },
    ...overrides.api,
  };
  return { api, sent };
}

test('the handler bridge prefers the object init() returned and falls back to api.internal', async () => {
  const calls = [];
  const api = {
    init: async () => ({ send: async (handler, args) => { calls.push(['lib', handler, args]); return 'ok'; } }),
    getBudgets: async () => [{ id: 'local', groupId: 'sync-id', name: 'Budget' }],
    loadBudget: async () => {},
    sync: async () => {},
    getServerVersion: async () => ({ version: '26.9.0' }),
    internal: { send: async (handler, args) => { calls.push(['internal', handler, args]); return 'ok'; } },
  };
  const service = new ActualService(api);
  await service.initialize({ serverUrl: 'http://actual', password: 'pw', syncId: 'sync-id', dataDir: '/tmp' });
  await service._send('simplefin-status');
  assert.deepEqual(calls, [['lib', 'simplefin-status', {}]]);

  const fallback = new ActualService({ internal: api.internal });
  fallback.initialized = true;
  await fallback._send('secret-set', { name: 'simplefin_token', value: 'x' });
  assert.deepEqual(calls[1], ['internal', 'secret-set', { name: 'simplefin_token', value: 'x' }]);

  const bare = new ActualService({});
  bare.initialized = true;
  await assert.rejects(() => bare._send('simplefin-status'), /handler bridge/);
});

test('detailed accounts carry link metadata and the bank behind it', async () => {
  const { api } = linkApi({ reply: () => [
    { id: 'acct', name: 'Checking', offbudget: 0, closed: 0, tombstone: 0, account_id: 'sf-1', official_name: 'CHECKING', account_sync_source: 'simpleFin', bank: 'bank-row', bankName: 'Test Bank', last_sync: '1757600000000', bank_sync_status: 'ok', balance_current: 1234, balance_available: null, balance_limit: null },
    { id: 'gone', name: 'Old', tombstone: 1 },
  ] });
  const service = new ActualService(api);
  service.initialized = true;
  const accounts = await service.listAccountsDetailed();
  assert.equal(accounts.length, 1, 'tombstoned accounts are not listed');
  const [account] = accounts;
  assert.equal(account.sync_source, 'simpleFin');
  assert.equal(account.external_id, 'sf-1');
  assert.equal(account.bank_name, 'Test Bank');
  assert.equal(account.bank_id, 'bank-row');
  assert.equal(account.balance_current, 1234);
  assert.equal(account.balance_available, null);
});

test('unlinking goes through Actual and reports what the account used to be', async () => {
  const { api, sent } = linkApi({ api: {
    aqlQuery: async () => ({ data: [{ id: 'acct', name: 'Checking', account_sync_source: 'simpleFin', account_id: 'sf-1' }] }),
  } });
  const service = new ActualService(api);
  service.initialized = true;
  assert.deepEqual(await service.unlinkAccount({ accountId: 'acct' }), {
    id: 'acct', name: 'Checking', previous_sync_source: 'simpleFin', previous_external_id: 'sf-1',
  });
  assert.deepEqual(sent, [{ handler: 'account-unlink', args: { id: 'acct' } }]);

  const missing = new ActualService(linkApi().api);
  missing.initialized = true;
  await assert.rejects(() => missing.unlinkAccount({ accountId: 'ghost' }), /not found/);
});

test('the server-side SimpleFIN account list is reduced to what a link needs', async () => {
  const { api } = linkApi({ reply: handler => handler === 'simplefin-accounts'
    ? { data: { accounts: [{ id: 'sf-1', name: 'Checking', balance: '12.34', currency: 'USD', 'balance-date': 1757600000, org: { name: 'Test Bank', domain: 'test.example', id: 'org-1' } }] } }
    : { data: { configured: true } } });
  const service = new ActualService(api);
  service.initialized = true;
  assert.deepEqual(await service.simpleFinServerStatus(), { configured: true, error: '' });
  assert.deepEqual(await service.simpleFinServerAccounts(), { accounts: [{
    account_id: 'sf-1', name: 'Checking', institution: 'Test Bank', org_domain: 'test.example', org_id: 'org-1',
    balance: '12.34', currency: 'USD', balance_date: 1757600000,
  }], error: '', reason: '' });

  const broken = new ActualService(linkApi({ reply: () => ({ data: { error_code: 'INVALID_ACCESS_TOKEN', reason: 'revoked' } }) }).api);
  broken.initialized = true;
  assert.deepEqual(await broken.simpleFinServerAccounts(), { accounts: [], error: 'INVALID_ACCESS_TOKEN', reason: 'revoked' });
});

test('relinking SimpleFIN onto an existing account sends the upgrade shape and a starting date', async () => {
  const { api, sent } = linkApi({
    api: { aqlQuery: async () => ({ data: [{ id: 'acct', name: 'Checking', account_sync_source: null, account_id: null }] }) },
    reply: handler => handler === 'accounts-get'
      ? [{ id: 'acct', name: 'Checking', offbudget: 0, closed: 0, account_id: 'sf-1', account_sync_source: 'simpleFin', bank: 'b', bankName: 'Test Bank' }]
      : 'ok',
  });
  const service = new ActualService(api);
  service.initialized = true;
  const linked = await service.linkSimpleFinAccount({
    accountId: 'acct',
    externalAccount: { account_id: 'sf-1', name: 'Checking', institution: 'Test Bank', org_domain: 'test.example' },
    startingDate: '2026-09-01',
  });
  assert.equal(linked.id, 'acct');
  assert.equal(linked.sync_source, 'simpleFin');
  assert.deepEqual(sent.filter(item => item.handler !== 'accounts-get'), [{ handler: 'simplefin-accounts-link', args: {
    externalAccount: { account_id: 'sf-1', name: 'Checking', institution: 'Test Bank', orgDomain: 'test.example', orgId: null },
    upgradingId: 'acct',
    offBudget: false,
    startingDate: '2026-09-01',
  } }]);
  assert.equal(linked.bank_name, 'Test Bank');
});

test('only the SimpleFIN token can be managed as a server secret', async () => {
  const { api, sent } = linkApi();
  const service = new ActualService(api);
  service.initialized = true;
  assert.deepEqual(await service.setServerSecret({ name: 'simplefin_token', value: null }), { name: 'simplefin_token', cleared: true });
  assert.deepEqual(sent, [{ handler: 'secret-set', args: { name: 'simplefin_token', value: null } }]);
  await assert.rejects(() => service.setServerSecret({ name: 'gocardless_secretId', value: 'x' }), /Refusing/);

  const refused = new ActualService(linkApi({ reply: () => ({ error: 'unauthorized' }) }).api);
  refused.initialized = true;
  await assert.rejects(() => refused.setServerSecret({ name: 'simplefin_token', value: 'tok' }), /refused/);
});

test('imports are validated, handed to Actual, and synced unless it is a dry run', async () => {
  const imported = [];
  let syncs = 0;
  const api = {
    importTransactions: async (accountId, transactions, opts) => { imported.push({ accountId, transactions, opts }); return { added: ['n1'], updated: [], errors: [], updatedPreview: [] }; },
    sync: async () => { syncs += 1; },
  };
  const service = new ActualService(api);
  service.initialized = true;
  const result = await service.importTransactions({ accountId: 'acct', transactions: [
    { date: '2026-09-10', amount_cents: -1250, payee_name: 'Blue Bottle', imported_payee: 'SQ *BLUE BOTTLE', imported_id: 'plaid-1', cleared: false },
  ] });
  assert.deepEqual(result, { added: ['n1'], updated: [], errors: [], preview: [], dry_run: false });
  assert.deepEqual(imported[0].transactions, [{ date: '2026-09-10', amount: -1250, payee_name: 'Blue Bottle', imported_payee: 'SQ *BLUE BOTTLE', imported_id: 'plaid-1', cleared: false }]);
  assert.deepEqual(imported[0].opts, { defaultCleared: true, dryRun: false });
  assert.equal(syncs, 1);

  await service.importTransactions({ accountId: 'acct', dryRun: true, transactions: [{ date: '2026-09-10', amount_cents: 5 }] });
  assert.equal(syncs, 1, 'a dry run writes nothing, so nothing is synced');
  await assert.rejects(() => service.importTransactions({ accountId: 'acct', transactions: [{ date: '2026-09-10', amount_cents: 12.5 }] }), /integer cents/);
  assert.deepEqual(await service.importTransactions({ accountId: 'acct', transactions: [] }), { added: [], updated: [], errors: [], preview: [], dry_run: false });
});

test('adopting a new imported id only touches rows that exist and change', async () => {
  const written = [];
  let syncs = 0;
  const api = {
    q: () => new Query(),
    aqlQuery: async () => ({ data: [
      { id: 'pending', imported_id: 'plaid-pending', cleared: 0, date: '2026-09-08', amount: -450 },
      { id: 'same', imported_id: 'plaid-same', cleared: 1, date: '2026-09-08', amount: -100 },
    ] }),
    batchBudgetUpdates: async callback => { await callback(); },
    updateTransaction: async (id, fields) => { written.push({ id, fields }); },
    sync: async () => { syncs += 1; },
  };
  const service = new ActualService(api);
  service.initialized = true;
  const result = await service.adoptImportedIds({ updates: [
    { transaction_id: 'pending', imported_id: 'plaid-posted', cleared: true, date: '2026-09-10', amount_cents: -475 },
    { transaction_id: 'same', imported_id: 'plaid-same', cleared: true, amount_cents: -100 },
    { transaction_id: 'gone', imported_id: 'plaid-x' },
  ] });
  assert.deepEqual(written, [{ id: 'pending', fields: { imported_id: 'plaid-posted', cleared: true, amount: -475, date: '2026-09-10' } }]);
  assert.deepEqual(result.applied, [{ id: 'pending', previous_imported_id: 'plaid-pending', fields: { imported_id: 'plaid-posted', cleared: true, amount: -475, date: '2026-09-10' } }]);
  assert.deepEqual(result.skipped, [{ id: 'same', reason: 'no_change' }, { id: 'gone', reason: 'deleted' }]);
  assert.equal(syncs, 1);
});

test('deleting transactions reports which ids were already gone', async () => {
  const deleted = [];
  const api = {
    q: () => new Query(),
    aqlQuery: async () => ({ data: [{ id: 'present' }] }),
    batchBudgetUpdates: async callback => { await callback(); },
    deleteTransaction: async id => { deleted.push(id); },
    sync: async () => {},
  };
  const service = new ActualService(api);
  service.initialized = true;
  assert.deepEqual(await service.deleteTransactions({ transactionIds: ['present', 'gone', 'present'] }), { deleted: ['present'], missing: ['gone'] });
  assert.deepEqual(deleted, ['present']);
});

test('account transactions are read in Clerk\'s shape', async () => {
  const api = {
    q: () => new Query(),
    aqlQuery: async () => ({ data: [{ id: 't', date: '2026-09-10', amount_cents: -100, payee_name: 'Shop', imported_description: 'SHOP 1', imported_id: 'sf-9', notes: '', category_id: null, cleared: 1, reconciled: 0, transfer_id: null, is_parent: 0, is_child: 0, starting_balance_flag: 0 }] }),
  };
  const service = new ActualService(api);
  service.initialized = true;
  assert.deepEqual(await service.accountTransactions({ accountId: 'acct', start: '2026-09-01' }), [{
    id: 't', date: '2026-09-10', amount_cents: -100, payee_name: 'Shop', imported_description: 'SHOP 1', imported_id: 'sf-9', notes: '',
    category_id: null, cleared: true, reconciled: false, is_transfer: false, is_parent: false, is_child: false, is_starting_balance: false,
  }]);
  await assert.rejects(() => service.accountTransactions({}), /account id/);
});

test('every bank-link method is reachable through dispatch', async () => {
  const service = new ActualService({});
  service.initialized = true;
  for (const method of ['listAccountsDetailed', 'accountTransactions', 'unlinkAccount', 'simpleFinServerStatus', 'simpleFinServerAccounts', 'linkSimpleFinAccount', 'setServerSecret', 'createAccount', 'importTransactions', 'listRules', 'listPayees', 'deleteRule', 'restoreRule']) {
    // Each rejects on its own validation rather than on "Unknown Actual worker method".
    await assert.rejects(() => service.dispatch(method, {}), error => !/Unknown Actual worker method/.test(error.message));
  }
  // Empty batches are a no-op rather than an error.
  assert.deepEqual(await service.dispatch('deleteTransactions', {}), { deleted: [], missing: [] });
  assert.deepEqual(await service.dispatch('adoptImportedIds', {}), { applied: [], skipped: [] });
});
