import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { test } from 'node:test';
import { AppToolsBridge, OfficialServer, readLines, runOfficialWorker } from '../cc_remote/codex_app_tools.mjs';

function fixture() {
  let target = { state: 'unavailable' };
  const servers = [], notifications = [];
  const bridge = new AppToolsBridge({
    discover: async () => target,
    notify: (message) => notifications.push(message),
    makeServer: (state) => {
      const server = {
        generation: state.generation, closed: false, calls: [],
        close() { this.closed = true; this.reject?.(new Error('connection lost')); },
        write() {},
        async request(method, params, options) {
          this.calls.push({ method, params, options });
          if (method === 'initialize') return { capabilities: { tools: {} } };
          if (method === 'tools/list') return { tools: [{ name: 'read_thread' }, { name: 'create_thread' }] };
          if (params?.arguments?.wait) return new Promise((resolve, reject) => { this.reject = reject; });
          return { content: [{ type: 'text', text: state.generation }], isError: false };
        },
      };
      servers.push(server);
      return server;
    },
  });
  return { bridge, servers, notifications, target: (value) => { target = value; } };
}

test('App absent is a healthy MCP with no tools and no model startup dependency', async () => {
  const { bridge } = fixture();
  const initialized = await bridge.request('initialize');
  assert.equal(initialized.capabilities.tools.listChanged, true);
  assert.deepEqual(await bridge.request('tools/list'), { tools: [] });
  assert.equal((await bridge.request('tools/call', { name: 'read_thread' })).isError, true);
  assert.deepEqual(await bridge.request('ping'), {});
  bridge.close();
});

test('changed official policy stays unavailable with a useful error', async () => {
  const f = fixture();
  f.target({ state: 'unavailable', reason: 'official_policy_changed' });
  await f.bridge.request('initialize');
  assert.deepEqual(await f.bridge.request('tools/list'), { tools: [] });
  assert.match((await f.bridge.request('tools/call', { name: 'new_write' })).content[0].text, /approval policy/);
  f.bridge.close();
});

test('one generic connection exposes the official catalog and preserves metadata', async () => {
  const f = fixture();
  f.target({ state: 'ready', generation: 'one' });
  await f.bridge.request('initialize');
  assert.equal((await f.bridge.request('tools/list')).tools.length, 2);
  const params = { name: 'read_thread', arguments: { id: 'native-id' }, _meta: { 'x-codex-turn-metadata': '{"thread_id":"correct"}' } };
  await f.bridge.request('tools/call', params);
  assert.deepEqual(f.servers[0].calls.at(-1).params, params);
  await f.bridge.refresh(true);
  assert.equal(f.servers.length, 1);
  f.bridge.close();
});

test('App return changes catalog; a new pipe replaces only our own MCP child', async () => {
  const f = fixture();
  await f.bridge.request('initialize');
  f.target({ state: 'ready', generation: 'one' });
  await f.bridge.refresh(true);
  assert.equal(f.notifications.at(-1).method, 'notifications/tools/list_changed');
  assert.equal((await f.bridge.request('tools/list')).tools.length, 2);
  f.target({ state: 'ready', generation: 'two' });
  await f.bridge.refresh(true);
  assert.equal(f.servers[0].closed, true);
  assert.equal(f.servers.length, 2);
  assert.equal((await f.bridge.request('tools/call', { name: 'read_thread' })).content[0].text, 'two');
  f.target({ state: 'unavailable' });
  await f.bridge.refresh(true);
  assert.deepEqual(await f.bridge.request('tools/list'), { tools: [] });
  f.bridge.close();
});

test('in-flight writes are never replayed on App replacement', async () => {
  const f = fixture();
  f.target({ state: 'ready', generation: 'one' });
  await f.bridge.request('initialize');
  const result = f.bridge.request('tools/call', { name: 'create_thread', arguments: { wait: true } });
  await new Promise((resolve) => setImmediate(resolve));
  f.target({ state: 'ready', generation: 'two' });
  await f.bridge.refresh(true);
  assert.equal((await result).isError, true);
  assert.equal(f.servers[0].calls.filter((x) => x.method === 'tools/call').length, 1);
  assert.equal(f.servers[1].calls.filter((x) => x.method === 'tools/call').length, 0);
  f.bridge.close();
});

test('closed bridge cannot spawn after an outstanding discovery completes', async () => {
  let resolve;
  let spawned = 0;
  const bridge = new AppToolsBridge({
    discover: () => new Promise((done) => { resolve = done; }),
    makeServer: () => { spawned++; throw new Error('should not spawn'); },
  });
  const refresh = bridge.refresh(true);
  bridge.close();
  resolve({ state: 'ready', generation: 'late' });
  await refresh;
  assert.equal(spawned, 0);
});

test('App exits between discovery and tool listing without failing MCP startup', async () => {
  const f = fixture();
  f.target({ state: 'ready', generation: 'one' });
  await f.bridge.request('initialize');
  f.servers[0].request = async () => { throw new Error('pipe closed'); };
  assert.deepEqual(await f.bridge.request('tools/list'), { tools: [] });
  assert.equal(f.servers[0].closed, true);
  await f.bridge.refresh(true);
  assert.equal((await f.bridge.request('tools/list')).tools.length, 2);
  f.bridge.close();
});

test('native handshake failure never advertises a healthy App tools connection', async () => {
  const bridge = new AppToolsBridge({
    discover: async () => ({ state: 'ready', generation: 'updated' }),
    makeServer: () => ({
      closed: false, write() {}, close() { this.closed = true; },
      async request(method) {
        if (method === 'initialize') return { capabilities: { tools: {} } };
        throw new Error('peer authorization failed');
      },
    }),
  });
  await bridge.request('initialize');
  assert.equal(bridge.server, null);
  assert.deepEqual(await bridge.request('tools/list'), { tools: [] });
  assert.match((await bridge.request('tools/call', { name: 'open_in_codex' })).content[0].text, /handshake failed/);
  bridge.close();
});

test('runtime replacement refreshes signed processes even when App and pipe stay unchanged', async () => {
  const f = fixture();
  f.target({ state: 'ready', generation: 'app:pipe:old-runtime' });
  await f.bridge.request('initialize');
  const active = f.bridge.request('tools/call', { name: 'open_in_codex', arguments: { wait: true } });
  await new Promise((resolve) => setImmediate(resolve));
  f.target({ state: 'ready', generation: 'app:pipe:new-runtime' });
  await f.bridge.refresh(true);
  assert.equal((await active).isError, true);
  assert.equal(f.servers[0].closed, true);
  assert.equal(f.servers[1].calls.filter((call) => call.method === 'tools/call').length, 0);
  const params = { name: 'read_thread', _meta: { 'x-codex-turn-metadata': '{"thread_id":"same-thread"}' } };
  await f.bridge.request('tools/call', params);
  assert.deepEqual(f.servers[1].calls.at(-1).params, params);
  f.bridge.close();
});

function childFixture() {
  const child = new EventEmitter();
  child.stdin = new PassThrough();
  child.stdout = new PassThrough();
  child.exitCode = null;
  child.signalCode = null;
  child.kill = () => { child.signalCode = 'SIGTERM'; child.emit('exit', null); };
  const messages = [];
  readLines(child.stdin, (message) => messages.push(message), (error) => { throw error; });
  const server = new OfficialServer({ node: '/signed/node', script: '/official/server.mjs', cwd: '/official', pipe: '/private.sock' }, { spawnProcess: () => child });
  return { server, child, messages };
}

test('stdio response errors and cancellation are preserved, never approved implicitly', async () => {
  const f = childFixture();
  const abort = new AbortController();
  const result = f.server.request('tools/call', { name: 'write' }, { signal: abort.signal });
  abort.abort();
  await assert.rejects(result, /not replayed/);
  assert.equal(f.messages.at(-1).method, 'notifications/cancelled');
  f.child.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: 77, method: 'unexpected/approval' }) + '\n');
  assert.equal(f.messages.at(-1).error.code, -32601);
  const failed = f.server.request('tools/call', { name: 'bad' });
  f.child.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: f.messages.at(-1).id, error: { code: -32602, message: 'bad parameters' } }) + '\n');
  await assert.rejects(failed, { code: -32602 });
  f.server.close();
});

test('child disconnect rejects pending requests and does not replay', async () => {
  const f = childFixture();
  const result = f.server.request('tools/call', { name: 'write' });
  f.child.emit('exit', 1);
  await assert.rejects(result, /not replayed/);
  assert.equal(f.messages.length, 1);
  assert.equal(f.server.closed, true);
});

test('child stdout EOF rejects pending requests even before process exit', async () => {
  const f = childFixture();
  const result = f.server.request('tools/call', { name: 'write' });
  f.child.stdout.emit('end');
  await assert.rejects(result, /not replayed/);
  assert.equal(f.messages.length, 1);
  assert.equal(f.server.closed, true);
});

test('each official peer gets a fresh parent from the validated installed runtime', () => {
  const spawned = [];
  const child = new EventEmitter();
  Object.assign(child, { stdin: new PassThrough(), stdout: new PassThrough(), exitCode: 0, signalCode: null });
  const target = { node: '/updated/signed/node', script: '/updated/official/server.mjs', cwd: '/updated/official', pipe: '/new.sock' };
  const server = new OfficialServer(target, { spawnProcess: (...args) => { spawned.push(args); return child; } });
  assert.equal(spawned[0][0], target.node);
  assert.match(spawned[0][1][0], /codex_app_tools\.mjs$/);
  assert.deepEqual(spawned[0][1].slice(1), ['--official-worker', target.script]);
  assert.equal(spawned[0][2].env.CODEX_APP_TOOLS_PIPE_PATH, target.pipe);
  assert.equal(spawned[0][2].cwd, target.cwd);
  server.close();
});

test('generation worker forwards bytes unchanged and reaps only its own child', async () => {
  const runtime = new EventEmitter();
  Object.assign(runtime, { execPath: '/new/signed/node', env: { CODEX_APP_TOOLS_PIPE_PATH: '/new.sock' }, stdin: new PassThrough(), stdout: new PassThrough() });
  const child = new EventEmitter();
  let stops = 0;
  Object.assign(child, {
    stdin: new PassThrough(), stdout: new PassThrough(), exitCode: null, signalCode: null,
    kill() { stops++; this.signalCode = 'SIGTERM'; this.emit('exit', null); this.emit('close', null); },
  });
  const spawned = [];
  runOfficialWorker('/official/server.mjs', { runtime, spawnProcess: (...args) => { spawned.push(args); return child; } });
  assert.deepEqual(spawned[0].slice(0, 2), ['/new/signed/node', ['/official/server.mjs']]);
  assert.deepEqual(spawned[0][2].env, runtime.env);
  const sent = [], received = [];
  child.stdin.on('data', (chunk) => sent.push(chunk));
  runtime.stdout.on('data', (chunk) => received.push(chunk));
  const request = Buffer.from('{"method":"tools/call","params":{"_meta":{"thread_id":"same"}}}\n');
  runtime.stdin.write(request);
  const reply = Buffer.from('{"result":{"text":"你好"}}\n');
  child.stdout.write(reply);
  assert.deepEqual(Buffer.concat(sent), request);
  assert.deepEqual(Buffer.concat(received), reply);
  runtime.emit('SIGTERM');
  runtime.emit('SIGTERM');
  assert.equal(stops, 1);
  assert.equal(runtime.stdin.destroyed, true);
});

test('stdio framing handles split multibyte UTF-8', () => {
  const stream = new PassThrough();
  const messages = [];
  readLines(stream, (m) => messages.push(m), (e) => { throw e; });
  const bytes = Buffer.from('{"text":"你好"}\n{"id":2}\n');
  for (const byte of bytes) stream.write(Buffer.from([byte]));
  assert.deepEqual(messages, [{ text: '你好' }, { id: 2 }]);
});
