/** Opt-in stdio MCP transport adapter. Tool implementations stay in the App. */
import { spawn } from 'node:child_process';
import { stat } from 'node:fs/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';

const MAX_BYTES = 16 * 1024 * 1024;
const MAX_PENDING = 128;
const OFFLINE = 'Codex App is not connected to this shared host. Open it using the shared-daemon launcher; normal CLI and cc-remote tools remain available.';
const LOST = 'Codex App connection changed. This operation was not replayed; its outcome may be unknown. Check the App before retrying a write.';

function stopChild(child, grace = 1000) {
  if (child.exitCode !== null || child.signalCode !== null) return;
  child.kill('SIGTERM');
  const timer = setTimeout(() => {
    if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
  }, grace);
  timer.unref();
  child.once('exit', () => clearTimeout(timer));
}

// The App authenticates both the MCP peer and its direct parent. A long-lived
// adapter can still be mapped to the old Node image after Sparkle replaces the
// App bundle. Keep the peer AND its signed parent owned by one connection
// generation. This worker forwards bytes only; all MCP policy stays official.
export function runOfficialWorker(script, { runtime = process, spawnProcess = spawn } = {}) {
  const child = spawnProcess(runtime.execPath, [script], {
    stdio: ['pipe', 'pipe', 'ignore'], env: runtime.env,
  });
  let stopping = false;
  const stop = () => {
    if (stopping) return;
    stopping = true;
    runtime.stdin.unpipe(child.stdin);
    child.stdin.destroy();
    stopChild(child);
  };
  const finish = (code) => {
    runtime.exitCode = code ?? 1;
    runtime.stdin.removeListener('end', stop);
    runtime.stdin.removeListener('close', stop);
    runtime.stdout.removeListener('error', stop);
    runtime.removeListener('SIGINT', stop);
    runtime.removeListener('SIGTERM', stop);
    runtime.stdin.unpipe(child.stdin);
    runtime.stdin.destroy();
  };
  runtime.stdin.once('end', stop);
  runtime.stdin.once('close', stop);
  runtime.stdout.once('error', stop);
  runtime.once('SIGINT', stop);
  runtime.once('SIGTERM', stop);
  child.stdin.once('error', stop);
  child.stdout.once('error', stop);
  child.stdout.once('end', stop);
  child.once('error', () => finish(1));
  child.once('close', finish);
  runtime.stdin.pipe(child.stdin);
  child.stdout.pipe(runtime.stdout, { end: false });
  return child;
}

function rpcError(message, code = -32603) {
  return Object.assign(new Error(message), { code });
}

export function readLines(stream, receive, fail) {
  let buffered = Buffer.alloc(0);
  stream.on('data', (chunk) => {
    buffered = Buffer.concat([buffered, chunk]);
    let end;
    while ((end = buffered.indexOf(10)) !== -1) {
      if (end > MAX_BYTES) { fail(rpcError('MCP frame too large')); return; }
      const line = buffered.subarray(0, end);
      buffered = buffered.subarray(end + 1);
      if (!line.length) continue;
      try { receive(JSON.parse(line.toString('utf8'))); }
      catch { fail(rpcError('Invalid MCP JSON')); return; }
    }
    if (buffered.length > MAX_BYTES) fail(rpcError('MCP frame too large'));
  });
}

export class OfficialServer {
  constructor(target, { notify, spawnProcess = spawn } = {}) {
    this.next = 0;
    this.pending = new Map();
    this.closed = false;
    this.notify = notify ?? (() => {});
    this.child = spawnProcess(target.node, [fileURLToPath(import.meta.url), '--official-worker', target.script], {
      cwd: target.cwd, stdio: ['pipe', 'pipe', 'ignore'],
      env: { ...process.env, CODEX_APP_TOOLS_PIPE_PATH: target.pipe, CODEX_MCP_NODE_PATH: target.node },
    });
    readLines(this.child.stdout, (message) => this.receive(message), () => this.close());
    this.child.stdout.once('end', () => this.close());
    this.child.stdout.once('error', () => this.close());
    this.child.once('error', () => this.close());
    this.child.once('exit', () => this.close());
    this.child.stdin.on('error', () => this.close());
  }

  receive(message) {
    if (message.method) {
      if (Object.hasOwn(message, 'id')) {
        // The installed official App MCP doesn't issue client requests. Never
        // silently grant an unexpected approval if a future version does.
        this.write({ jsonrpc: '2.0', id: message.id, error: { code: -32601, message: 'Unsupported MCP client request' } });
      } else {
        this.notify(message);
      }
      return;
    }
    const pending = this.pending.get(message.id);
    if (!pending) return;
    this.pending.delete(message.id);
    pending.cleanup();
    if (message.error) pending.reject(Object.assign(new Error(message.error.message), message.error));
    else pending.resolve(message.result);
  }

  write(message) {
    if (this.closed) throw rpcError(LOST);
    const line = JSON.stringify(message) + '\n';
    if (Buffer.byteLength(line) > MAX_BYTES || this.child.stdin.writableLength > MAX_BYTES) {
      throw rpcError('MCP input limit exceeded');
    }
    this.child.stdin.write(line);
  }

  request(method, params, { signal, timeout = 10_000 } = {}) {
    if (this.closed || signal?.aborted) return Promise.reject(rpcError(LOST));
    if (this.pending.size >= MAX_PENDING) return Promise.reject(rpcError('Too many pending App requests'));
    const id = ++this.next;
    return new Promise((resolve, reject) => {
      const cancel = () => {
        const entry = this.pending.get(id);
        if (!entry) return;
        this.pending.delete(id);
        entry.cleanup();
        try { this.write({ jsonrpc: '2.0', method: 'notifications/cancelled', params: { requestId: id } }); } catch { /* Already disconnected. */ }
        reject(rpcError('App request cancelled or timed out; not replayed'));
      };
      const timer = setTimeout(cancel, timeout);
      signal?.addEventListener('abort', cancel, { once: true });
      this.pending.set(id, {
        resolve, reject,
        cleanup: () => { clearTimeout(timer); signal?.removeEventListener('abort', cancel); },
      });
      try { this.write({ jsonrpc: '2.0', id, method, params }); }
      catch (error) { this.pending.delete(id); clearTimeout(timer); signal?.removeEventListener('abort', cancel); reject(error); }
    });
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    for (const entry of this.pending.values()) { entry.cleanup(); entry.reject(rpcError(LOST)); }
    this.pending.clear();
    this.child.stdin.destroy();
    // The worker gets time to reap its own official MCP child first.
    stopChild(this.child, 2000);
  }
}

export class AppToolsBridge {
  constructor({ discover, notify = () => {}, makeServer = (target, options) => new OfficialServer(target, options) }) {
    this.discover = discover;
    this.notify = notify;
    this.makeServer = makeServer;
    this.server = null;
    this.generation = null;
    this.reason = null;
    this.initialized = false;
    this.closed = false;
    this.refreshing = null;
    this.lastCheck = 0;
  }

  async refresh(force = false) {
    if (this.closed) return;
    if (this.refreshing) return this.refreshing;
    if (!force && Date.now() - this.lastCheck < (this.server && !this.server.closed ? 60_000 : 2000)) return;
    this.lastCheck = Date.now();
    this.refreshing = (async () => {
      const target = await this.discover().catch(() => ({ state: 'unavailable' }));
      if (this.closed) return;
      const next = target.state === 'ready' ? target.generation : null;
      this.reason = target.reason ?? null;
      if (next === this.generation && (!next || (this.server && !this.server.closed))) return;
      this.server?.close();
      this.server = null;
      this.generation = null;
      if (next) {
        const server = this.makeServer(target, { notify: (message) => {
          if (this.initialized && this.server === server) this.notify(message);
        } });
        try {
          const result = await server.request('initialize', {
            protocolVersion: '2024-11-05', capabilities: {},
            clientInfo: { name: 'cc-remote-desktop-tools', version: '0.1.0' },
          });
          if (!result?.capabilities?.tools) throw rpcError('Official App MCP lacks tools');
          server.write({ jsonrpc: '2.0', method: 'notifications/initialized' });
          // initialize is local to the MCP. Listing the catalog is a read-only
          // native-pipe handshake, including the App's peer authorization.
          const catalog = await server.request('tools/list', {});
          if (!Array.isArray(catalog?.tools)) throw rpcError('Official App catalog unavailable');
          if (this.closed) { server.close(); return; }
          this.server = server;
          this.generation = next;
        } catch { server.close(); this.reason = 'app_tools_unavailable'; }
      }
      if (this.initialized) this.notify({ jsonrpc: '2.0', method: 'notifications/tools/list_changed' });
    })().finally(() => { this.refreshing = null; });
    return this.refreshing;
  }

  async request(method, params, signal) {
    if (method === 'initialize') {
      await this.refresh(true);
      this.initialized = true;
      return {
        protocolVersion: '2024-11-05',
        capabilities: { tools: { listChanged: true } },
        serverInfo: { name: 'codex-app-tools', title: 'Codex App Tools (shared host)', version: '0.1.0' },
        instructions: 'Tools provided by the running Codex App on this shared host. App-specific tools can be unavailable when the App is closed; other tools are unaffected.',
      };
    }
    if (!this.initialized) throw rpcError('Initialize MCP first', -32002);
    if (method === 'ping') return {};
    if (method !== 'tools/list' && method !== 'tools/call') throw rpcError('Method not supported', -32601);
    await this.refresh();
    const server = this.server;
    if (!server) {
      if (method === 'tools/list') return { tools: [] };
      return { isError: true, content: [{ type: 'text', text: this.reason === 'official_policy_changed'
        ? 'The official Codex App MCP configuration changed. Review its tool approval policy and regenerate this adapter configuration before enabling App tools again.'
        : this.reason === 'app_tools_unavailable'
          ? 'Codex App is connected, but its native tools handshake failed. Check the App tools connection; no operation was replayed.' : OFFLINE }] };
    }
    try {
      const result = await server.request(method, params, {
        signal, timeout: method === 'tools/call' ? 3_600_000 : 10_000,
      });
      if (server !== this.server) throw rpcError(LOST);
      return result;
    } catch (error) {
      // Never replay a tool call after a transport error: it may have changed
      // state already. A later explicit request may use the replacement App.
      if (method === 'tools/call') return { isError: true, content: [{ type: 'text', text: error.message || LOST }] };
      // Keep the optional MCP alive if the App exits during tools/list.
      if (this.server === server) {
        server.close();
        this.server = null;
        this.generation = null;
      }
      return { tools: [] };
    }
  }

  close() { this.closed = true; this.server?.close(); this.server = null; }
}

export function discoverCommand(python, profile, app, manifest, { spawnProcess = spawn } = {}) {
  return () => new Promise((resolve, reject) => {
    const proc = spawnProcess(python, ['-m', 'cc_remote.codex_app_tools', 'discover', '--profile', profile, '--app', app, '--manifest-sha256', manifest], {
      stdio: ['ignore', 'pipe', 'ignore'],
    });
    let output = '';
    const timer = setTimeout(() => { proc.kill('SIGKILL'); reject(rpcError('App discovery timed out')); }, 8000);
    proc.stdout.on('data', (data) => {
      output += data.toString('utf8');
      if (output.length > 8192) { proc.kill('SIGKILL'); reject(rpcError('Invalid App discovery output')); }
    });
    proc.once('error', (error) => { clearTimeout(timer); reject(error); });
    proc.once('exit', (code) => {
      clearTimeout(timer);
      if (code !== 0) { reject(rpcError('App discovery failed')); return; }
      try { resolve(JSON.parse(output)); } catch { reject(rpcError('Invalid App discovery output')); }
    });
  });
}

async function main() {
  const argument = (name) => {
    const index = process.argv.indexOf(name);
    if (index < 0 || !process.argv[index + 1]) throw new Error(`Missing ${name}`);
    return process.argv[index + 1];
  };
  const send = (message) => {
    const line = JSON.stringify(message) + '\n';
    if (Buffer.byteLength(line) > MAX_BYTES || process.stdout.writableLength > MAX_BYTES) { stop(); return; }
    process.stdout.write(line);
  };
  const bridge = new AppToolsBridge({
    discover: discoverCommand(argument('--python'), argument('--profile'), argument('--app'), argument('--manifest-sha256')),
    notify: send,
  });
  const pending = new Map();
  let closed = false;
  const stop = () => {
    if (closed) return;
    closed = true;
    clearInterval(timer);
    for (const controller of pending.values()) controller.abort();
    bridge.close();
    process.stdin.destroy();
  };
  let fingerprint = '';
  let lastRefresh = 0;
  let polling = false;
  const timer = setInterval(async () => {
    if (closed || !bridge.initialized || polling) return;
    polling = true;
    try {
      const info = await stat('/tmp/codex-browser-use').catch(() => null);
      const next = info ? `${info.ino}:${info.mtimeMs}` : '';
      // Most ticks are one stat, not one process/log scan per loaded thread.
      if (next !== fingerprint || bridge.server?.closed || Date.now() - lastRefresh > (bridge.server ? 60_000 : 10_000)) {
        fingerprint = next;
        lastRefresh = Date.now();
        await bridge.refresh(true);
      }
    } finally { polling = false; }
  }, 2000);
  timer.unref();
  process.stdin.once('end', stop);
  process.stdin.once('close', stop);
  process.stdout.once('error', stop);
  process.once('SIGINT', stop);
  process.once('SIGTERM', stop);
  readLines(process.stdin, (message) => {
    if (!Object.hasOwn(message, 'id')) {
      if (message.method === 'notifications/cancelled') pending.get(message.params?.requestId)?.abort();
      return;
    }
    if (pending.has(message.id) || pending.size >= MAX_PENDING) {
      send({ jsonrpc: '2.0', id: message.id, error: { code: -32600, message: 'Duplicate or excess pending request' } });
      return;
    }
    const controller = new AbortController();
    pending.set(message.id, controller);
    bridge.request(message.method, message.params, controller.signal)
      .then((result) => { if (!closed) send({ jsonrpc: '2.0', id: message.id, result }); })
      .catch((error) => { if (!closed) send({ jsonrpc: '2.0', id: message.id, error: { code: error.code ?? -32603, message: error.message } }); })
      .finally(() => pending.delete(message.id));
  }, stop);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  if (process.argv[2] === '--official-worker' && process.argv.length === 4) {
    runOfficialWorker(process.argv[3]);
  } else {
    main().catch(() => { process.stderr.write('Codex App tools adapter failed to start\n'); process.exitCode = 1; });
  }
}
