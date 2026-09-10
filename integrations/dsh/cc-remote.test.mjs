import assert from 'node:assert/strict';
import test from 'node:test';
import { apply, snapshot, SNAPSHOT_PATH } from './cc-remote.mjs';

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

for (const suffix of ['&unknown=yes', '&sessionId=other', '&maxMessages=101', '&beforeSeq=-1', '&throughSeq=1.5', '&maxMessages=00']) {
  test(`invalid request is rejected before observing history: ${suffix}`, async () => {
    const { ctx, calls } = fixture();
    const response = await snapshot(ctx, new Request('http://127.0.0.1/api/cc-remote.snapshot?sessionId=test-session' + suffix));
    assert.equal(response.status, 400);
    assert.equal(calls.length, 0);
  });
}

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
