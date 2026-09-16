import { useReducer } from "react";
import { ChatView } from "../../src/components/ChatView";
import { createRuntime, initialState, reduce } from "../../src/reducer";
import { PROTOCOL_VERSION, type ServerEvent } from "../../src/protocol";

export function TurnUsageFixture() {
  const params = new URLSearchParams(location.search);
  const engine = params.get("engine") === "claude" ? "claude" : params.get("engine") === "dsh" ? "dsh" : "codex";
  document.documentElement.dataset.engine = engine;
  document.documentElement.dataset.theme = params.get("theme") === "dark" ? "dark" : "light";
  const [state, dispatch] = useReducer(reduce, { ...initialState, focusedSid: "session", runtimes: {
    session: { ...createRuntime(), state: "running" as const, turns: [{
      id: "user", forkPointId: "native", prompt: "检查任务进度", done: false,
      blocks: [{ kind: "text" as const, message_id: "reply", text: "正在检查代码和任务状态。", done: false, channel: "commentary" as const }],
    }], turnUsage: { native: { v: PROTOCOL_VERSION, type: "turn_usage" as const, ts: 1,
      turn_id: "native", sid: "session", seq: 1,
      usage: { input_tokens: 184_001, output_tokens: 2400, cache_read_tokens: 180_000, cache_write_tokens: 500 } } } },
  } });
  const runtime = state.runtimes.session;
  const send = (event: Partial<ServerEvent>) => dispatch({ type: "event", event: {
    v: PROTOCOL_VERSION, sid: "session", ts: 2, ...event,
  } as ServerEvent });
  return <main className="pane" style={{ height: "100dvh", display: "flex", flexDirection: "column" }}>
    <div style={{ padding: 12, display: "flex", gap: 12 }}>
      <button onClick={() => send({ type: "turn_usage", turn_id: "native", seq: 2,
        usage: { input_tokens: 186_800, output_tokens: 5820, cache_read_tokens: 182_000, cache_write_tokens: 500 } })}>更新用量</button>
      <button onClick={() => send({ type: "turn_end", turn_id: "native", seq: 3,
        result: { subtype: "success", duration_ms: 1000, is_error: false } })}>结束任务</button>
    </div>
    <ChatView sid="session" engine={engine} turns={runtime.turns} turnUsage={runtime.turnUsage}
      activeTurnId={runtime.turns[0].done ? null : "user"} />
    <footer className="composer" style={{ height: 130, flex: "none" }}>输入消息</footer>
  </main>;
}
