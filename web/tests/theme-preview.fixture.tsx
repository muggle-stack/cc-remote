/** Isolated, zero-network preview using the production chat and controls. */
import { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "../src/index.css";
import "../src/App.css";
import { SessionsSidebar } from "../src/components/SessionsSidebar";
import { ChatView } from "../src/components/ChatView";
import { Composer } from "../src/components/Composer";
import { HeaderMenu } from "../src/components/HeaderMenu";
import { EngineSelector } from "../src/components/EngineSelector";
import BackgroundTaskControl from "../src/components/BackgroundTaskControl";
import { ComposerDraftStore } from "../src/composer-drafts";
import { Icon } from "../src/icons";
import { createRuntime, initialState, reduce, type AppState } from "../src/reducer";
import { normalizeEngine, PROTOCOL_VERSION, type Engine, type ServerEvent, type SessionInfo } from "../src/protocol";
import { isThemeChoice } from "../src/themes";
import { useTheme } from "../src/use-theme";
import { useBoldText } from "../src/use-bold-text";
import { useMobileViewport } from "../src/use-mobile-viewport";

const params = new URLSearchParams(location.search);
const noop = () => {};
const profiles = [{ id: "default", label: "Primary", available: true }, { id: "nyx", label: "Stack", available: true }];
function demoState(engine: Engine) {
  let state: AppState = { ...initialState, runtimes: { preview: createRuntime() } };
  const events: Record<string, unknown>[] = [
    { type: "user_msg", msg_id: "theme-preview", prompt: "试试这套低饱和主题，看看聊天、代码和后台任务的效果。" },
    { type: "state", state: "running" },
    { type: "process", item_id: "thinking", kind: "reasoning", phase: "end", status: "succeeded",
      turn_id: "theme-preview", title: "检查配色与文字对比度", text: "让主题色留在背景和控件中，保持正文清晰。" },
    { type: "process", item_id: "read", kind: "tool", phase: "end", status: "succeeded",
      turn_id: "theme-preview", title: "读取主题配置", tool_name: "Read", command: "web/src/themes.ts" },
    { type: "assistant_msg_start", message_id: "answer", turn_id: "theme-preview", channel: "final" },
    { type: "delta", message_id: "answer", turn_id: "theme-preview", channel: "final", text:
      "六款主题已经准备好了，页面保留了原来的布局。\n\n### 更柔和，也更清楚\n\n背景、消息气泡、边框和按钮使用同一套配色，正文保留清晰的对比。\n\n- `frontend.cpp:68` 使用 `std::max_element`，需要补上 `<algorithm>`。\n- `package.xml`、`test.yaml` 和 `workdir: sdk` 符合当前规范。\n\n完整记录：[查看审查报告](https://github.com/muggle-stack/cc-remote)，项目说明：[`README.md`](https://github.com/muggle-stack/cc-remote/blob/master/README.md)。\n\n```ts\nconst appearance = {\n  palette: 'moss',\n  rememberPerEngine: true,\n};\n```\n\n从右上角 **更多设置 → 主题** 打开配色卡，点击就能看到整页效果。每个引擎的选择会在当前浏览器单独保存。" },
    { type: "assistant_msg_end", message_id: "answer", turn_id: "theme-preview", channel: "final" },
    { type: "turn_end", turn_id: "theme-preview", result: { subtype: "success", duration_ms: 74000, is_error: false } },
    { type: "state", state: "idle" },
    { type: "background_process_sync", items: [{ item_id: "build", kind: "task", status: "running",
      title: "检查主题预览", command: "npm run build", started_at: Date.now() / 1000 - 90 }] },
  ];
  for (const payload of events) state = reduce(state, { type: "event", event: {
    v: PROTOCOL_VERSION, ts: Date.now() / 1000, sid: "preview", engine, ...payload,
  } as ServerEvent });
  return state.runtimes.preview;
}

export function ThemePreview() {
  useMobileViewport();
  const [engine, setEngine] = useState<Engine>(() => normalizeEngine(params.get("engine") ?? "codex"));
  const { mode, choice, selectTheme } = useTheme(engine);
  const { boldText, setBoldText } = useBoldText();
  const [sidebarOpen, setSidebarOpen] = useState(() => window.innerWidth >= 980);
  const [active, setActive] = useState("preview");
  const [space, setSpace] = useState<"code" | "work">("code");
  const [sendMode, setSendMode] = useState<"steer" | "queue">("steer");
  const drafts = useRef(new ComposerDraftStore());
  const runtime = useMemo(() => demoState(engine), [engine]);
  useEffect(() => {
    const palette = params.get("palette");
    if (isThemeChoice(palette)) {
      selectTheme(palette);
      const url = new URL(location.href);
      url.searchParams.delete("palette");
      history.replaceState(null, "", url);
    }
  // Preview URL initializes a palette once; UI selections remain live afterward.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const sessions: SessionInfo[] = [
    { session_id: "preview", summary: "cc-remote", cwd: "/workspace/cc-remote", pinned: true, state: "idle" },
    { session_id: "auriga", summary: "Auriga", cwd: "/workspace/auriga", pinned: true, state: "idle" },
    { session_id: "speech", summary: "机器人语音", cwd: "/workspace/speech", pinned: true, state: "idle" },
    { session_id: "review", summary: "代码审查", cwd: "/workspace", state: "idle" },
    { session_id: "deploy", summary: "部署检查", cwd: "/workspace", state: "idle", timed_tasks: [{
      task_id: "preview-timer", title: "检查部署状态", next_message_at: Date.now() / 1000 + 780,
      interval_seconds: 900, sent_count: 1, total_count: 3, valid_until: Date.now() / 1000 + 3600,
    }] },
  ].map((session, index) => ({ ...session, engine, space,
    codex_profile_id: index === 1 ? "nyx" : "default", claude_profile_id: index === 1 ? "nyx" : "default" })) as SessionInfo[];
  return <div className={`shell${sidebarOpen ? " sidebar-open" : ""}`}>
    <SessionsSidebar open={sidebarOpen} engine={engine} space={space} profileScopeKey={`preview:${engine}`}
      machineId="theme-preview" codexProfiles={profiles} claudeProfiles={profiles}
      defaultCodexProfileId="default" defaultClaudeProfileId="default"
      sessions={sessions} activeSessionId={active} onSpaceChange={setSpace} onSelect={setActive}
      onClose={() => setSidebarOpen(false)} onNew={() => setActive("new")}
      onNewInDir={noop} onRename={noop} onArchive={noop} onPin={noop} onDelete={noop}
      onForkWorktree={noop} onMigrate={noop} />
    <main className="pane">
      <header className="c-head">
        <button className="iconbtn" onClick={() => setSidebarOpen(value => !value)} aria-label="会话列表"><Icon name="code" /></button>
        <div className="titlewrap"><div className="ttl">Code</div><div className="sub">主题预览 · 示例会话</div></div>
        <span className="hstat idle"><span className="sd" />idle</span>
        <EngineSelector engine={engine} onChange={setEngine} />
        <HeaderMenu engine={engine} theme={mode} themeChoice={choice} onSelectTheme={selectTheme}
          boldText={boldText} onBoldText={setBoldText}
          notificationMode="off" notificationBinding="off" notificationAvailable={false}
          onNotificationMode={async () => false} onOpenUsageActivity={noop} onLogout={noop} />
      </header>
      <ChatView sid="preview" turns={runtime.turns} engine={engine} historyScopeKey={engine} />
      <Composer draftKey={engine} draftStore={drafts.current} state="idle"
        backgroundTasks={<BackgroundTaskControl processes={runtime.backgroundProcesses} />}
        connState="connected" wrapperOnline sendMode={sendMode} setSendMode={setSendMode}
        queue={[]} pendingSend={null} failedDeferred={[]} unconfirmedQueued={[]} unconfirmedReplaceable={[]}
        queueCapacity={{}} replaceQueueCapacity={{}}
        model={engine === "codex" ? "gpt-6-astra" : String(engine) === "dsh" ? "DeepSeek-V41-Flash" : "claude-fable-5-1"} effort="max"
        perm={engine === "codex" ? "danger-full-access" : "bypassPermissions"}
        permissionProfile={null} permissionProfiles={null} webSearch={null} collaborationMode="default"
        engine={engine} editPrompt={null} onEditConsumed={noop} onSendQuery={() => false}
        onSteerQuery={() => false} onInterrupt={noop} onEnqueue={() => false} onSetPending={() => false}
        onRemoveQueued={noop} onInspectQueued={noop} onSetModel={noop} onSetEffort={noop} onSetPerm={noop}
        onSetPermissionProfile={noop} onGetPermissionProfiles={noop} onSetWebSearch={noop}
        onSetCollaborationMode={noop} onClear={noop} onContext={noop} contextReport={null} />
    </main>
  </div>;
}

createRoot(document.getElementById("root")!).render(<ThemePreview />);
