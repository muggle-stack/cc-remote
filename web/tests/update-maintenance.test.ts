import assert from "node:assert/strict";
import { RelayWs } from "../src/ws.ts";
import type { AppState } from "../src/reducer.ts";
import { createServer } from "vite";
import { PROTOCOL_VERSION } from "../src/protocol.ts";

class Socket {
  static readonly OPEN = 1;
  static instances: Socket[] = [];
  readyState = 1;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor() { Socket.instances.push(this); }
  send(raw: string) { this.sent.push(raw); }
  close() { this.readyState = 3; }
  receive(frame: Record<string, unknown>) {
    this.onmessage?.({ data: JSON.stringify({ v: PROTOCOL_VERSION, ts: 1, ...frame }) });
  }
}
const harness = await createServer({ root: process.cwd(), appType: "custom", logLevel: "silent",
  server: { middlewareMode: true, watch: null } });
try {
const { initialState, reduce } = await harness.ssrLoadModule("/src/reducer.ts") as typeof import("../src/reducer.ts");
Object.assign(globalThis, {
  window: { location: { protocol: "http:", host: "relay.test" } }, WebSocket: Socket,
  sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
});
for (const code of ["update_in_progress", "busy"]) {
  let state: AppState = { ...initialState, focusedSid: "update-a" };
  let notices = 0;
  const guarded = new RelayWs({
    onEvent: event => { state = reduce(state, { type: "event", event }); },
    onConnState: () => {}, onMaintenance: () => { notices++; },
  });
  guarded.setFocusedSid("update-a", "codex"); guarded.start();
  const socket = Socket.instances.at(-1)!; socket.onopen?.();
  socket.receive({ type: "error", code, sid: "update-a", message: "设备正在安装更新，请在重新连接后继续。" });
  assert.equal(notices, 1);
  assert.equal(state.banner, initialState.banner);
  const before = socket.sent.length;
  assert.equal(guarded.sendQueryTo("update-a", "preserved draft", "update-query"), false);
  guarded.setFocusedSid("update-b", "codex");
  guarded.sendSwitchSession("update-b", "codex");
  assert.equal(socket.sent.length, before);
  assert.equal(guarded.sendGetHistory("update-b"), true);
  assert.equal(JSON.parse(socket.sent.at(-1)!).type, "get_history");
  guarded.setMaintenance(false); guarded.setMaintenance(false);
  const resumed = socket.sent.slice(before).map(raw => JSON.parse(raw));
  assert.equal(resumed.filter(frame => frame.type === "switch_session").length, 1);
  assert.equal(resumed.find(frame => frame.type === "switch_session").session_id, "update-b");
  assert.equal(resumed.some(frame => frame.type === "query"), false);
  socket.receive({ type: "error", code: "busy", message: "ordinary busy" });
  assert.equal(state.banner, "当前操作暂时无法执行，请稍后重试。");
  assert.equal(notices, 1);
  guarded.stop();
}
} finally { await harness.close(); }
console.log("maintenance: reconnect, history reads, current-session recovery and ordinary errors passed");
