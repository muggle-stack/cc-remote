import { useEffect, useReducer, useState } from "react";
import { ChatView } from "../../src/components/ChatView";
import { useChatReadingMemory } from "../../src/chat-reading-memory";
import { createRuntime, initialState, reduce, type SessionRuntime, type Turn } from "../../src/reducer";
import { displayHistoryProjection } from "../../src/history-recovery";
import { activeTurnCandidateIds } from "../../src/process-blocks";
import { PROTOCOL_VERSION, type Engine } from "../../src/protocol";

const ENGINES = ["claude", "codex", "dsh"] as const;

function turn(id: string, running = false): Turn {
  const blocks: Turn["blocks"] = [{ kind: "text", done: true, message_id: `${id}-answer`,
    channel: running ? "commentary" : "final",
    text: Array.from({ length: 20 }, (_, i) => `${id} paragraph ${i}. Reading a long answer while another session continues working.`).join("\n\n"),
  }];
  if (running) blocks.push({ kind: "tool", tool_use_id: `${id}-tool`,
    message_id: `${id}-tool-message`, tool: "commandExecution", category: "command",
    input: { command: "check progress" }, title: "检查进度", done: true,
    result: { content: "Background check started", is_error: false, status: "succeeded" },
  });
  return { id, done: !running, prompt: id, blocks };
}

export function ReadingNavigationFixture() {
  const params = new URLSearchParams(window.location.search);
  const startingEngine = ENGINES.find(candidate => candidate === params.get("engine")) ?? "codex";
  const live = params.has("live-reading");
  const [engine, setEngine] = useState<Engine>(startingEngine);
  const [machine, setMachine] = useState("device-a");
  const [state, dispatch] = useReducer(reduce, null, () => ({ ...initialState,
    focusedSid: `${startingEngine}-session`,
    runtimes: Object.fromEntries(ENGINES.map(e => [`${e}-session`, {
      ...createRuntime(), historyRevision: "revision-1", historyGeneration: "generation-1",
      state: live ? "running" : "idle",
      liveOwner: live ? { turnId: `${e}-3`, seq: 1 } : null,
      historyHeadKnown: true, hasMore: true, oldestId: `${e}-1`,
      turns: [turn(`${e}-1`), turn(`${e}-2`), turn(`${e}-3`, live)],
    } satisfies SessionRuntime])),
  }));
  const sid = state.focusedSid!;
  const runtime = state.runtimes[sid];
  const scope = `${machine}:code:${engine}`;
  const memory = useChatReadingMemory(scope, sid, runtime, state.historyBrowse, dispatch);
  const view = displayHistoryProjection(state.historyRecovery, sid, runtime, state.historyBrowse);
  const activeTurns = activeTurnCandidateIds(view.turns, runtime.liveOwner?.turnId,
    !view.browsing && runtime.state !== "idle");
  const navigate = (e: Engine) => {
    dispatch({ type: "restore_session_list", sessions: [] });
    dispatch({ type: "focus_session", sid: `${e}-session` });
    setEngine(e);
  };
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if (!(event.metaKey || event.ctrlKey)) return;
      if (["1", "2", "3"].includes(event.key)) {
        event.preventDefault(); navigate(ENGINES[Number(event.key) - 1]);
      }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, []);
  return <main style={{ height: "100dvh", display: "flex", flexDirection: "column" }}>
    <div>
      <button onClick={() => navigate("claude")}>Claude</button>
      <button onClick={() => navigate("codex")}>Codex</button>
      <button onClick={() => navigate("dsh")}>DSH</button>
      <button onClick={() => setMachine(m => m === "device-a" ? "device-b" : "device-a")}>Switch device</button>
      <button onClick={() => {
        const targetSid = `${startingEngine}-session`;
        const turns = state.runtimes[targetSid].turns;
        dispatch({ type: "set_turns", sid: targetSid,
          turns: live ? turns.map(row => row.id === `${startingEngine}-3` ? {
            ...row, blocks: [...row.blocks, { kind: "tool", tool_use_id: `${row.id}-next-tool`,
              message_id: `${row.id}-next-message`, tool: "commandExecution", category: "command",
              input: { command: "continue checking" }, title: "继续检查", done: false,
            }],
          } : row) : [...turns, turn("background-output")],
        });
      }}>Background output</button>
      {live && <button onClick={() => {
        const targetSid = `${startingEngine}-session`;
        dispatch({ type: "set_turns", sid: targetSid,
          turns: state.runtimes[targetSid].turns.map(row => ({
            ...row, done: true, blocks: row.blocks.map(block => ({ ...block, done: true })),
          })),
        });
        dispatch({ type: "event", event: { v: PROTOCOL_VERSION, ts: Date.now() / 1000,
          type: "state", sid: targetSid, state: "idle" } });
      }}>Complete task</button>}
      {params.has("rebase-reading") && <button onClick={() => dispatch({
        type: "set_turns", sid: `${startingEngine}-session`,
        turns: [turn("new-head-1"), turn("new-head-2"), turn("new-head-3")],
      })}>Replace latest window</button>}
      <button onClick={() => dispatch({ type: "begin_history_browse", sid,
        revision: runtime.historyRevision!, generation: runtime.historyGeneration,
        scopeKey: scope, viewId: "old-browse", basePageKey: "latest" })}>Browse</button>
      <button onClick={() => {
        const b = state.historyBrowse!;
        dispatch({ type: "install_history_browse_page", sid, scopeKey: b.scopeKey,
          revision: b.revision, generation: b.generation, viewId: b.viewId,
          windowEpoch: b.windowEpoch, before: b.olderCursor!, page: {
            pageKey: "older", turns: Array.from({ length: 12 }, (_, i) => turn(`older-${i}`)),
            hasOlder: false, olderCursor: null, newerPageKey: "latest",
          } });
      }}>Older page</button>
      <output data-testid="surface">{scope}</output>
      <output data-testid="browse-id">{state.historyBrowse?.viewId ?? "latest"}</output>
    </div>
    <ChatView key={`${scope}:${sid}`} {...memory} sid={sid} engine={engine}
      turns={view.turns} hasMore={view.hasMore} historyRevision={runtime.historyRevision}
      historyGeneration={runtime.historyGeneration} historyViewRevision={view.viewRevision}
      historyScopeKey={view.scopeKey ?? scope} historyViewId={view.viewId}
      historyWindowEpoch={view.windowEpoch} historyCursor={view.oldestId}
      browseMode={view.browsing} hasNewer={view.hasNewer}
      activeTurnId={activeTurns.length === 1 ? activeTurns[0] : null}
      onReturnLatest={() => dispatch({ type: "return_to_latest", sid })}
      onEdit={() => {}} onGetDiff={() => {}} />
  </main>;
}
