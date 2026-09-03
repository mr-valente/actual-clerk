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

test('transaction change reporting distinguishes additions from reconciliations', () => {
  const before = [{ id: 'one', amount: 100, date: '2026-09-01' }];
  const after = [{ id: 'one', amount: 200, date: '2026-09-01' }, { id: 'two', amount: 50, date: '2026-09-01' }];
  assert.deepEqual(transactionDiff(before, after), { added: ['two'], updated: ['one'] });
});
