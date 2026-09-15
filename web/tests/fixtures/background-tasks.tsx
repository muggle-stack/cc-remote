import { useEffect, useReducer, useRef, useState } from "react";
import { Composer } from "../../src/components/Composer";
import { ChatView } from "../../src/components/ChatView";
import BackgroundTaskControl from "../../src/components/BackgroundTaskControl";
import { ComposerDraftStore } from "../../src/composer-drafts";
import { createRuntime, initialState, reduce, type AppState } from "../../src/reducer";
import { PROTOCOL_VERSION, type ServerEvent } from "../../src/protocol";
import { useMobileViewport } from "../../src/use-mobile-viewport";

function event(sid: string, payload: Record<string, unknown>): ServerEvent {
  return { v: PROTOCOL_VERSION, ts: Date.now() / 1000, sid, ...payload } as ServerEvent;
}

function initial(): AppState {
  let state: AppState = { ...initialState, runtimes: {} };
  for (const sid of ["background-a", "background-b"]) {
    state.runtimes[sid] = createRuntime();
    for (const payload of [
      { type: "user_msg", msg_id: "parent", prompt: "检查构建进度和板载温度" },
      { type: "state", state: "running" },
      { type: "process", item_id: "build", kind: "task", phase: "start", status: "running",
        turn_id: "parent", title: "检查构建进度和板载温度", command: "make verify",
        background: true, ts: Date.now() / 1000 - 154 },
      { type: "process", item_id: "agent", kind: "agent", phase: "start", status: "running",
        turn_id: "parent", title: "核对构建产物", summary: "后台检查仍在继续", background: true },
      { type: "assistant_msg_start", message_id: "answer", turn_id: "parent", channel: "final" },
      { type: "delta", message_id: "answer", turn_id: "parent", channel: "final",
        text: "构建还在后台运行，我会在完成后继续核对结果。" },
      { type: "assistant_msg_end", message_id: "answer", turn_id: "parent", channel: "final" },
    ]) state = reduce(state, { type: "event", event: event(sid, payload) });
  }
  return state;
}

export function BackgroundTasksFixture() {
  useMobileViewport();
  const [state, dispatch] = useReducer(reduce, undefined, initial);
  const [sid, setSid] = useState("background-a");
  const [opened, setOpened] = useState("");
  const drafts = useRef(new ComposerDraftStore());
  const runtime = state.runtimes[sid];
  useEffect(() => {
    document.documentElement.dataset.engine = "claude";
    document.documentElement.dataset.theme = new URLSearchParams(location.search).get("theme") ?? "dark";
  }, []);
  const emit = (payload: Record<string, unknown>) => dispatch({ type: "event", event: event(sid, payload) });
  const complete = (itemId: string, kind: "task" | "agent") => emit({
    type: "process", item_id: itemId, kind, phase: "end", status: "succeeded",
    turn_id: "parent", title: itemId === "build" ? "检查构建进度和板载温度" : "核对构建产物",
    summary: "任务已完成", background: true,
  });
  return <main className="pane" style={{ height: "100dvh", display: "flex", flexDirection: "column" }}>
    <div data-testid="background-test-actions" style={{ flex: "none", display: "flex", flexWrap: "wrap", gap: 12 }}>
      <button onClick={() => {
        emit({ type: "turn_end", turn_id: "parent",
          result: { subtype: "success", duration_ms: 12000, is_error: false } });
        emit({ type: "state", state: "idle" });
      }}>回复结束</button>
      <button onClick={() => complete("build", "task")}>构建完成</button>
      <button onClick={() => complete("agent", "agent")}>代理完成</button>
      <button onClick={() => emit({ type: "background_process_sync", items: [] })}>空快照</button>
      <button onClick={() => setSid(value => value === "background-a" ? "background-b" : "background-a")}>切换会话</button>
      <button onClick={() => emit({ type: "background_process_sync", items: Array.from({ length: 12 }, (_, i) => ({
        item_id: `task-${i}`, kind: "task", status: "running", title: `后台任务 ${i + 1}：${"正在检查构建输出与温度。".repeat(4)}`,
        command: "make verify", started_at: Date.now() / 1000 - 60,
      })) })}>多个任务</button>
      <output data-testid="background-opened">{opened}</output>
    </div>
    <header className="c-head" style={{ flex: "none" }}>Code</header>
    <ChatView sid={sid} turns={runtime.turns} engine="claude"
      historyScopeKey={sid} onOpenAgent={id => setOpened(id)} />
    <Composer draftKey={sid} draftStore={drafts.current} state={runtime.state}
      backgroundTasks={runtime.backgroundProcesses.length > 0
        ? <BackgroundTaskControl key={sid} processes={runtime.backgroundProcesses}
            onOpenAgent={id => setOpened(id)} /> : null}
      connState="connected" wrapperOnline sendMode={runtime.sendMode}
      setSendMode={mode => dispatch({ type: "set_send_mode", sid, mode })}
      queue={[]} pendingSend={null} failedDeferred={[]}
      unconfirmedQueued={[]} unconfirmedReplaceable={[]}
      queueCapacity={{}} replaceQueueCapacity={{}}
      model="claude-fable-5-1" effort="max" perm="bypassPermissions"
      permissionProfile={null} permissionProfiles={null} webSearch={null}
      collaborationMode="default" engine="claude" editPrompt={null}
      onEditConsumed={() => {}} onSendQuery={() => false} onSteerQuery={() => false}
      onInterrupt={() => {}} onEnqueue={() => false} onSetPending={() => false}
      onRemoveQueued={() => {}} onInspectQueued={() => {}} onSetModel={() => {}}
      onSetEffort={() => {}} onSetPerm={() => {}} onSetPermissionProfile={() => {}}
      onGetPermissionProfiles={() => {}} onSetWebSearch={() => {}}
      onSetCollaborationMode={() => {}} onClear={() => {}} onContext={() => {}}
      contextReport={null} />
  </main>;
}
