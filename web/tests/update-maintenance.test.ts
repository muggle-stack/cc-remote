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
  guarded.setFocusedSid("update-a", "codex", "code"); guarded.start();
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

for (const trigger of ["maintenance", "reconnect"]) {
  for (const engine of ["claude", "codex", "dsh"] as const) {
    for (const known of [false, true]) {
      const relay = new RelayWs({ onEvent: () => {}, onConnState: () => {} });
      relay.setFocusedSid("visible-code", "claude", "code");
      if (known) relay.setSessionEngines([{ session_id: "background-work", engine, space: "work" }]);
      relay.start();
      const socket = Socket.instances.at(-1)!; socket.onopen?.();
      try {
        if (trigger === "maintenance") relay.setMaintenance(true);
        const requestId = relay.sendGetContextTo("background-work");
        const original = socket.sent.map(raw => JSON.parse(raw)).find(frame => frame.cmd_id === requestId);
        const before = socket.sent.length;
        if (trigger === "maintenance") relay.setMaintenance(false);
        else socket.receive({ type: "wrapper_reconnected", state: "idle", generation: "replacement" });
        const replay = socket.sent.slice(before).map(raw => JSON.parse(raw));
        assert.deepEqual(replay.find(frame => frame.cmd_id === requestId), original,
          "background reads retain their exact command identity");
        assert.equal(replay.some(frame => frame.type === "switch_session" && frame.session_id === "background-work"), false,
          `${trigger}/${engine}/${known}: background reads must never switch or resume a session`);
        assert.equal(relay.pendingSessionIds().includes("background-work"), true);
      } finally { relay.stop(); }
    }
  }
}

for (const engine of ["claude", "codex"] as const) {
  const relay = new RelayWs({ onEvent: () => {}, onConnState: () => {} });
  relay.setFocusedSid("visible-code", "claude", "code");
  relay.setSessionEngines([{ session_id: "work-target", engine, space: "work" }]);
  relay.sendSetModelTo("work-target", "selected-model");
  relay.sendSetModelTo("unknown-target", "selected-model");
  relay.start();
  const socket = Socket.instances.at(-1)!; socket.onopen?.();
  try {
    const frames = socket.sent.map(raw => JSON.parse(raw));
    const preparation = frames.find(frame => frame.type === "switch_session" && frame.session_id === "work-target");
    assert.equal(preparation.engine, engine); assert.equal(preparation.space, "work");
    assert.equal(frames.some(frame => frame.type === "switch_session" && frame.session_id === "unknown-target"), false);
    assert.equal(frames.filter(frame => frame.type === "set_model").length, 2,
      "unknown routing must not silently discard queued commands");
    assert.equal(frames.at(-1).session_id, "visible-code", "restore the user's visible focus last");
  } finally { relay.stop(); }
}
} finally { await harness.close(); }
console.log("maintenance: reconnect, history reads, current-session recovery and ordinary errors passed");
