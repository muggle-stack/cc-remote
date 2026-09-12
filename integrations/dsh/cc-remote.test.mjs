import assert from 'node:assert/strict';
import test from 'node:test';
import { apply, snapshot, commandCatalog, producedFiles, SNAPSHOT_PATH } from './cc-remote.mjs';

function fixture() {
  const calls = [];
  const source = {
    header: { version: 3, id: 'test-session', cwd: '/project', isSeeded: false },
    cursor: 12, projections: { asOfSeq: 12, values: {} },
    [Symbol.dispose]() { calls.push('dispose'); },
  };
  const ctx = {
    sessionQuery: { async observeSession(id, options) {
      calls.push({ id, options });
      return source;
    } },
    typertGateway: { async invoke(request) {
      calls.push(request);
      return { records: [], hasMore: false };
    } },
  };
  return { ctx, source, calls };
}

test('plugin registers only the authenticated Connection Fetch route', () => {
  const routes = [];
  apply({ connection: { fetch: { register: route => routes.push(route) } } });
  assert.equal(routes.length, 1);
  assert.equal(routes[0].path, SNAPSHOT_PATH);
  assert.deepEqual(routes[0].methods, ['GET']);
});

test('cold cursor uses a read lease and the official page endpoint only', async () => {
  const { ctx, calls } = fixture();
  const response = await snapshot(ctx, new Request('http://127.0.0.1/api/cc-remote.snapshot?sessionId=test-session'));
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.cursor, 12);
  assert.equal(body.contract, 1);
  assert.equal(calls[0].options.projectionMode, 'all');
  assert.equal(calls[1].namespace, 'session');
  assert.equal(calls[1].method, 'page');
  assert.deepEqual(calls[1].args.request, {
    address: { kind: 'session', sessionId: 'test-session' }, throughSeq: 12, maxMessages: 16,
  });
  assert.equal(calls[2], 'dispose');
});

test('pagination keeps the original cut while later events have arrived', async () => {
  const { ctx, calls } = fixture();
  const response = await snapshot(ctx, new Request('http://127.0.0.1/api/cc-remote.snapshot?sessionId=test-session&throughSeq=8&beforeSeq=4&maxMessages=2'));
  assert.equal(response.status, 200);
  assert.equal((await response.json()).cursor, 8);
  assert.equal(calls[1].args.request.throughSeq, 8);
  assert.equal(calls[1].args.request.beforeSeq, 4);
});

for (const mode of ['one-shot', 'continuable']) {
  test(`child history uses its durable parent and ${mode} descriptor without activation`, async () => {
    const { ctx, source, calls } = fixture();
    source.header.origin = 'subagent';
    source.header.parentSession = 'immediate-parent';
    source.projections.values.subagent = { mode, seq: 0 };
    const response = await snapshot(ctx, new Request('http://127.0.0.1/api/cc-remote.snapshot?sessionId=test-session&beforeSeq=9&throughSeq=10'));
    assert.equal(response.status, 200);
    assert.deepEqual(calls[1].args.request, {
      address: { kind: 'subagent', parentSessionId: 'immediate-parent', childSessionId: 'test-session', mode },
      beforeSeq: 9, throughSeq: 10, maxMessages: 16,
    });
    assert.equal(calls[1].method, 'page');
    assert.equal(calls.at(-1), 'dispose');
  });
}

test('ordinary user forks retain the ordinary session address', async () => {
  const { ctx, source, calls } = fixture();
  source.header.parentSession = 'original-session';
  await snapshot(ctx, new Request('http://127.0.0.1/api/cc-remote.snapshot?sessionId=test-session'));
  assert.deepEqual(calls[1].args.request.address, { kind: 'session', sessionId: 'test-session' });
});

for (const [parentSession, descriptor] of [
  [undefined, { mode: 'continuable' }], ['parent', null], ['parent', { mode: 'unknown' }],
]) {
  test(`invalid child identity is not retried as an ordinary session: ${parentSession}/${descriptor?.mode}`, async () => {
    const { ctx, source, calls } = fixture();
    Object.assign(source.header, { origin: 'subagent', parentSession });
    source.projections.values.subagent = descriptor;
    const response = await snapshot(ctx, new Request('http://127.0.0.1/api/cc-remote.snapshot?sessionId=test-session'));
    assert.equal(response.status, 503);
    assert.deepEqual(await response.json(), { error: 'history_unavailable' });
    assert.equal(calls.some(call => call?.method), false);
    assert.equal(calls.at(-1), 'dispose');
  });
}

function turnsFixture(first = 10) {
  const fixtureValue = fixture();
  const { ctx, source } = fixtureValue;
  source.events = [
    { seq: 0, type: 'permission/preset' }, { seq: 1, type: 'model/selection' },
    { seq: 2, type: 'agent/inbox/spliced' }, { seq: 3, type: 'turn/start' },
    { seq: 4, type: 'step/start' }, { seq: 5, type: 'user/message', data: { source: { kind: 'user' } } },
    { seq: 6, type: 'assistant/message' }, { seq: 7, type: 'turn/end' },
    { seq: 8, type: 'turn/start' }, { seq: 9, type: 'user/message', data: { source: { kind: 'user' } } },
    { seq: 10, type: 'assistant/message' }, { seq: 11, type: 'turn/end' },
  ];
  ctx.typertGateway.invoke = async ({ args: { request } }) => ({
    // Match a native message-sized page, which need not start at turn/start.
    records: source.events.filter(event => event.seq >= first
      && event.seq < (request.beforeSeq ?? Infinity)
      && event.seq <= request.throughSeq).map(event => ({ type: 'event', event })),
    hasMore: true,
  });
  return fixtureValue;
}

for (const first of [3, 5, 6]) {
  test(`first interrupted task has no older history, even with native page starting at ${first}`, async () => {
    const { ctx } = turnsFixture(first);
    const response = await snapshot(ctx, new Request('http://127.0.0.1/api/cc-remote.snapshot?sessionId=test-session&beforeSeq=8&throughSeq=7'));
    const body = await response.json();
    assert.equal(response.status, 200);
    assert.deepEqual(body.records.map(record => record.event.seq), [3, 4, 5, 6, 7]);
    assert.equal(body.hasMore, false);
    assert.equal(body.cursor, 7);
  });
}

test('expanding a later turn preserves real older history without pulling future events', async () => {
  const { ctx } = turnsFixture();
  const body = await (await snapshot(ctx, new Request('http://127.0.0.1/api/cc-remote.snapshot?sessionId=test-session&throughSeq=10'))).json();
  assert.deepEqual(body.records.map(record => record.event.seq), [8, 9, 10]);
  assert.equal(body.hasMore, true);
});

for (const suffix of ['&unknown=yes', '&sessionId=other', '&maxMessages=101', '&beforeSeq=-1', '&throughSeq=1.5', '&maxMessages=00', '&commands=true']) {
  test(`invalid request is rejected before observing history: ${suffix}`, async () => {
    const { ctx, calls } = fixture();
    const response = await snapshot(ctx, new Request('http://127.0.0.1/api/cc-remote.snapshot?sessionId=test-session' + suffix));
    assert.equal(response.status, 400);
    assert.equal(calls.length, 0);
  });
}

test('cold commands come from the recorded preset without activating an agent', async () => {
  const { ctx, source, calls } = fixture();
  source.header.agentPreset = 'minimal';
  source.projections.values.agentPreset = 'ptc';
  const scope = { agentPreset: 'ptc' };
  ctx.agents = { get: id => { assert.equal(id, 'test-session'); } };
  ctx.agentPresets = { async standingKeyFor(id) { assert.equal(id, 'ptc'); return scope; } };
  ctx.commands = { list: value => { assert.equal(value, scope); return [{ name: 'goal', description: 'Native goal' }]; } };
  const response = await snapshot(ctx, new Request('http://127.0.0.1/api/cc-remote.snapshot?sessionId=test-session&commands=1'));
  assert.equal(response.status, 200);
  assert.deepEqual((await response.json()).commands, [{ name: 'goal', description: 'Native goal' }]);
  assert.equal(calls.filter(c => c?.method).every(c => c.method === 'page'), true);
  assert.equal(calls.at(-1), 'dispose');
});

test('live command discovery retains the exact agent generation and scoped overrides', async () => {
  const live = { id: 'test-session' };
  const source = { header: { id: live.id }, projections: { values: { agentPreset: 'ptc' } } };
  const ctx = { agents: { get: () => live }, commands: { list: value => { assert.equal(value, live); return []; } },
    agentPresets: { standingKeyFor: () => { throw new Error('must not replace live composition'); } } };
  assert.deepEqual(await commandCatalog(ctx, source), []);
});

test('unsupported format and stale cursor release the lease', async () => {
  const { ctx, source, calls } = fixture();
  source.header.version = 2;
  assert.equal((await snapshot(ctx, new Request('http://127.0.0.1/api/cc-remote.snapshot?sessionId=test-session'))).status, 409);
  assert.equal(calls.at(-1), 'dispose');
  source.header.version = 3;
  assert.equal((await snapshot(ctx, new Request('http://127.0.0.1/api/cc-remote.snapshot?sessionId=test-session&throughSeq=20'))).status, 409);
  assert.equal(calls.at(-1), 'dispose');
});

test('a provider failure is private and always releases the lease', async () => {
  const { ctx, calls } = fixture();
  ctx.typertGateway.invoke = async () => { throw new Error('secret-path-and-token'); };
  const response = await snapshot(ctx, new Request('http://127.0.0.1/api/cc-remote.snapshot?sessionId=test-session'));
  assert.equal(response.status, 503);
  assert.equal((await response.text()).includes('secret'), false);
  assert.equal(calls.at(-1), 'dispose');
});


test('full-text hit selects its native cursor and releases the cold read lease', async () => {
  const { ctx, calls } = fixture();
  ctx.sessionQuery.searchEvents = async request => {
    assert.deepEqual(request, { sessionId: 'test-session', query: 'needle', limit: 20,
      filters: [{ kind: 'type', values: ['user/message', 'assistant/message'] }, { kind: 'surface', values: ['current'] }] });
    return { items: [
      { sessionId: 'test-session', seq: 10, surface: 'retired', type: 'assistant/message' },
      { sessionId: 'other-session', seq: 8, surface: 'current', type: 'assistant/message' },
      { sessionId: 'test-session', seq: 3, surface: 'current', type: 'user/message' },
    ] };
  };
  const response = await snapshot(ctx, new Request('http://127.0.0.1/api/cc-remote.snapshot?sessionId=test-session&query=needle'));
  assert.equal(response.status, 200);
  assert.equal(calls[1].args.request.beforeSeq, 4);
  assert.equal(calls.at(-1), 'dispose');
});

test('an expired search match cannot silently display the latest unrelated page', async () => {
  const { ctx, calls } = fixture();
  ctx.sessionQuery.searchEvents = async () => ({ items: [] });
  const response = await snapshot(ctx, new Request('http://127.0.0.1/api/cc-remote.snapshot?sessionId=test-session&query=needle'));
  assert.equal(response.status, 409);
  assert.equal((await response.json()).error, 'search_match_changed');
  assert.equal(calls.at(-1), 'dispose');
  assert.equal(calls.some(call => call?.method === 'page'), false);
});

test('produced files include successful writes and explicit deliverables, excluding failed reads and future events', () => {
  const call = (seq, name, args) => ({ type: 'tool/call', seq, data: { callId: String(seq), name, arguments: JSON.stringify(args) } });
  const result = (seq, callId, isError = false) => ({ type: 'tool/result', seq, data: { message: { content: [{ type: 'tool-result', toolCallId: String(callId), isError }] } } });
  assert.deepEqual(producedFiles([
    call(1, 'write', { file_path: 'report.md' }), result(2, 1),
    call(3, 'write', { file_path: 'failed.md' }), result(4, 3, true),
    call(5, 'read', { file_path: 'secret.md' }), result(6, 5),
    { type: 'deliverables/presented', seq: 7, data: { files: [{ path: 'book.xlsx', description: 'Workbook' }] } },
    { type: 'deliverables/presented', seq: 8, data: { files: [{ path: 'later.md' }] } },
  ], 7), [{ path: 'book.xlsx', label: 'Workbook' }, { path: 'report.md', label: '创建或修改的文件' }]);
});
