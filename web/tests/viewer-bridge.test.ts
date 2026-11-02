import assert from "node:assert/strict";
import { test, type TestContext } from "node:test";
import { PROTOCOL_VERSION } from "../src/protocol.ts";
import { ViewerBridge } from "../src/viewer-bridge.ts";

async function fixture(t: TestContext) {
  const sockets: Socket[] = [];
  class Socket {
    static OPEN = 1;
    readyState = 1;
    bufferedAmount = 0;
    binaryType = "";
    onopen: (() => void) | null = null;
    onmessage: ((event: { data: string }) => void) | null = null;
    onerror: (() => void) | null = null;
    onclose: (() => void) | null = null;
    sent: object[] = [];
    closed = false;
    constructor() { sockets.push(this); }
    send(data: string) { this.sent.push(JSON.parse(data)); }
    close() { this.closed = true; this.onclose?.(); }
  }
  class Port {
    onmessage: ((event: { data: object }) => void) | null = null;
    closed = false;
    postMessage() {}
    close() { this.closed = true; }
  }
  const channels: Channel[] = [];
  class Channel {
    port1 = new Port();
    port2 = new Port();
    constructor() { channels.push(this); }
  }
  const intervals = new Map<number, () => void>();
  const win = Object.assign(new EventTarget(), {
    setInterval: (callback: () => void) => { intervals.set(1, callback); return 1; },
    clearInterval: (id: number) => { intervals.delete(id); },
  });
  const globals = { window: win, WebSocket: Socket, MessageChannel: Channel,
    location: new URL("https://remote.example/") };
  const restoreGlobals: (() => void)[] = [];
  for (const [name, value] of Object.entries(globals)) {
    const saved = Object.getOwnPropertyDescriptor(globalThis, name);
    Object.defineProperty(globalThis, name, { configurable: true, value });
    restoreGlobals.push(() => {
      if (saved) Object.defineProperty(globalThis, name, saved);
      else Reflect.deleteProperty(globalThis, name);
    });
  }
  const inits: { type: string; v: number; id: string; nonce: string }[] = [];
  const frame = Object.assign(new EventTarget(), { src: "", contentWindow: {
    postMessage: (message: typeof inits[number]) => inits.push(message),
  } });
  const grant = { id: "test-grant", runner: "/__cc_viewer/bridge/test-grant",
    entry: "/index.html", script_origins: [] };
  const errors: { message: string; clear: boolean }[] = [];
  let ready = 0;
  const bridge = new ViewerBridge(frame as unknown as HTMLIFrameElement, grant,
    () => { ready++; }, (message, clear) => errors.push({ message, clear }));
  t.after(() => {
    bridge.dispose();
    restoreGlobals.forEach(restore => restore());
  });
  await Promise.resolve(); // Start the bridge's deferred socket setup.
  const socket = sockets[0];
  socket.onopen?.();
  const emit = (data = {}, origin = "null", source: object = frame.contentWindow) => {
    win.dispatchEvent(Object.assign(new Event("message"), { origin, source,
      data: { type: "cc-viewer-bridge-ready", id: grant.id, v: PROTOCOL_VERSION, ...data } }));
  };
  const bound = () => socket.onmessage?.({ data: JSON.stringify({ type: "bound", v: PROTOCOL_VERSION, id: grant.id }) });
  return { bridge, socket, frame, grant, errors, inits, channels, intervals, emit, bound,
    ready: () => ready };
}

test("a stale runner reports an update immediately and closes the handshake", async t => {
  const f = await fixture(t);
  f.emit({ v: PROTOCOL_VERSION - 1 });
  assert.equal(f.errors.length, 1);
  assert.match(f.errors[0].message, /版本不一致.*刷新 cc-remote/);
  assert.equal(f.socket.closed, true);
  assert.equal(f.intervals.size, 0);
  f.bound();
  f.emit();
  assert.equal(f.inits.length, 0);
  assert.equal(f.errors.length, 1, "late socket events cannot replace the useful error");
});

test("other windows, origins, grants and message types cannot trigger version errors", async t => {
  const f = await fixture(t);
  const stale = { v: PROTOCOL_VERSION - 1 };
  f.emit(stale, "null", {});
  f.emit(stale, "https://remote.example");
  f.emit({ ...stale, id: "other-grant" });
  f.emit({ ...stale, type: "other-message" });
  assert.deepEqual(f.errors, []);
  assert.equal(f.socket.closed, false);
  f.emit(stale);
  assert.equal(f.errors.length, 1, "the authentic stale runner is still detected");
});

for (const runnerFirst of [true, false]) {
  test(`matching runner connects and loads (runner first: ${runnerFirst})`, async t => {
    const f = await fixture(t);
    assert.equal(f.frame.src, f.grant.runner);
    assert.deepEqual(f.socket.sent, [{ type: "bind", v: PROTOCOL_VERSION, id: f.grant.id }]);
    if (runnerFirst) f.emit(); else f.bound();
    assert.equal(f.inits.length, 0, "both parties must be ready before transferring the port");
    if (runnerFirst) f.bound(); else f.emit();
    assert.equal(f.inits.length, 1);
    assert.equal(f.inits[0].v, PROTOCOL_VERSION);
    assert.equal(f.inits[0].id, f.grant.id);
    f.channels[0].port1.onmessage?.({ data: { type: "loaded", nonce: f.inits[0].nonce } });
    assert.equal(f.ready(), 1);
    f.emit({ v: PROTOCOL_VERSION - 1 });
    assert.deepEqual(f.errors, [], "ready messages stop being accepted after connecting");
    f.bridge.dispose();
    assert.equal(f.channels[0].port1.closed, true);
  });
}

test("a media codec error leaves the page and resource channel usable", async t => {
  const f = await fixture(t);
  f.emit(); f.bound();
  const nonce = f.inits[0].nonce;
  f.channels[0].port1.onmessage?.({ data: { type: "media-error", nonce, message: "unsupported codec" } });
  assert.deepEqual(f.errors, [{ message: "unsupported codec", clear: false }]);
  assert.equal(f.socket.closed, false);
  f.channels[0].port1.onmessage?.({ data: { type: "loaded", nonce } });
  assert.equal(f.ready(), 1);
});
