import { useReducer, useState } from "react";
import { createRoot } from "react-dom/client";
import "../src/index.css";
import "../src/App.css";
import { HeaderFeedback } from "../src/components/HeaderFeedback";
import { EngineSelector } from "../src/components/EngineSelector";
import { TerminalControl } from "../src/components/TerminalControl";
import { SessionStatus } from "../src/components/SessionStatus";
import { NoticeStack } from "../src/components/NoticeStack";
import { ReconnectBanner } from "../src/components/ReconnectBanner";
import { SessionsSidebar } from "../src/components/SessionsSidebar";
import { Icon } from "../src/icons";
import { reduce, initialState, createRuntime } from "../src/reducer";
import { headerBannerKind, headerConnectionPhase, isHeaderNotice } from "../src/header-feedback";
import { PROTOCOL_VERSION, type Notice, type Engine } from "../src/protocol";

const noop = () => {};
document.documentElement.dataset.engine = "claude";
document.documentElement.dataset.theme = "dark";

export function HeaderFeedbackFixture() {
  const [state, dispatch] = useReducer(reduce, { ...initialState,
    connState: "connected", wrapperOnline: true, focusedSid: "a",
    runtimes: { a: createRuntime(), b: createRuntime() },
  });
  const [sid, setSid] = useState("a");
  const [engine, setEngine] = useState<Engine>("claude");
  const [space, setSpace] = useState<"code" | "work">("code");
  const [syncing, setSyncing] = useState(false);
  const [device, setDevice] = useState("test-device");
  const [running, setRunning] = useState(false);
  const [versionBlocked, setVersionBlocked] = useState(false);
  const [engineError, setEngineError] = useState<string | null>(null);
  const [reloadCount, setReloadCount] = useState(0);
  const runtime = state.runtimes[sid];
  const notice = (id: string, warning = false, detail?: string) => dispatch({ type: "event", event: {
    v: PROTOCOL_VERSION, type: "notice", ts: 1, sid, notice_id: id,
    severity: warning ? "warning" : "info", category: "runtime",
    title: warning ? "回滚部分完成" : "回滚完成", message: "对话：已恢复 · 代码：已恢复", detail,
  } as Notice });
  const checkpoint = (id: string) => dispatch({ type: "event", event: {
    v: PROTOCOL_VERSION, type: "notice", ts: 1, sid, notice_id: id,
    severity: "warning", category: "runtime", title: "本轮代码回滚不可用",
    message: "本轮修改了 Git 暂存区或包含当前无法安全记录的文件；对话仍可正常回滚。",
  } as Notice });
  const controls = <div style={{ padding: 24, display: "flex", flexWrap: "wrap", gap: 12 }}>
    <button onClick={() => dispatch({ type: "event", event: { v: PROTOCOL_VERSION,
      type: "wrapper_disconnected", ts: 1 } })}>offline</button>
    <button onClick={() => dispatch({ type: "conn", connState: "reconnecting" })}>reconnecting</button>
    <button onClick={() => { dispatch({ type: "conn", connState: "connected" }); dispatch({ type: "event",
      event: { v: PROTOCOL_VERSION, type: "wrapper_reconnected", ts: 2 } }); }}>sync</button>
    <button onClick={() => { setSyncing(false); dispatch({ type: "event", event: {
      v: PROTOCOL_VERSION, type: "replay_end", ts: 3, sid,
    } }); }}>ready</button>
    <button onClick={() => setSyncing(value => !value)}>history sync</button>
    <button onClick={() => notice("rollback-1")}>undo</button>
    <button onClick={() => notice("rollback-1")}>replay same undo</button>
    <button onClick={() => notice("rollback-2")}>new undo</button>
    <button onClick={() => dispatch({ type: "command_error", detail: "操作未完成，请稍后重试。" })}>fail</button>
    <button onClick={() => notice("rollback-warning", true, "文件未恢复：" + "/example/very-long-path".repeat(120))}>long failure</button>
    <button onClick={() => checkpoint("checkpoint-1")}>checkpoint</button>
    <button onClick={() => checkpoint("checkpoint-1")}>replay same checkpoint</button>
    <button onClick={() => checkpoint("checkpoint-2")}>new checkpoint</button>
    <button onClick={() => dispatch({ type: "event", event: {
      v: PROTOCOL_VERSION, type: "notice", ts: 1, sid, notice_id: "compact-exception",
      severity: "warning", category: "runtime", title: "上下文压缩失败", message: "请稍后重试。",
    } as Notice })}>other exception</button>
    <button onClick={() => dispatch({ type: "command_error", detail: "当前操作不适用于这个会话。" })}>session failure</button>
    <button onClick={() => dispatch({ type: "command_error", detail: "通知权限未开启，设置没有更改。" })}>permission failure</button>
    <button onClick={() => dispatch({ type: "command_error", detail: "页面版本已更新，请刷新后重试。" })}>page mismatch</button>
    <button onClick={() => setVersionBlocked(value => !value)}>protocol mismatch</button>
    <button onClick={() => setEngineError(value => value ? null : "本机连接断开")}>engine connection</button>
    <span data-testid="reload-count">{reloadCount}</span>
    <button onClick={() => setSid(value => value === "a" ? "b" : "a")}>session</button>
    <button onClick={() => setDevice(value => value === "test-device" ? "other-device" : "test-device")}>device</button>
    <button onClick={() => setSpace(value => value === "code" ? "work" : "code")}>space</button>
    <button onClick={() => setRunning(value => !value)}>running</button>
  </div>;
  return <div className="app">
    <SessionsSidebar open={false} engine="claude" space={space} cwd="/workspace"
      machineId="test-device" sessions={[{ session_id: "a", summary: "cc-remote", cwd: "/workspace/cc-remote",
        engine: "claude", state: "running", space: "code", tag: "pinned", updated_at: 1 }]}
      activeSessionId={sid} onSpaceChange={setSpace} onSelect={setSid} onClose={noop}
      onNew={noop} onNewInDir={noop} onRename={noop} onArchive={noop} onPin={noop} onDelete={noop}
      onForkWorktree={noop} onMigrate={noop} />
    <section className={`pane ${space}-pane`}>
      <header className={`c-head ${space}-head`}>
        <div className="titlewrap"><div className="ttl"><button className="surface-head-title">
          <span className="surface-head-mark"><Icon name={space === "work" ? "work" : "code"} size={18} /></span>
          <span>{space === "work" ? "Work" : "Code"}</span>
        </button></div><div className="sub">session test-session</div></div>
        <HeaderFeedback deviceScope={device} sessionScope={sid}
          versionNotice={versionBlocked ? "版本不兼容，请更新设备。" : null} versionBlocked={versionBlocked}
          engineConnectionError={engineError}
          pageReload={{ onReload: () => setReloadCount(count => count + 1), updating: false, message: null }}
          phase={headerConnectionPhase(state.connState, state.wrapperOnline, syncing, state.banner, state.wrapperConnection)}
          banner={state.banner} bannerRevision={state.bannerRevision} notices={runtime.notices.filter(isHeaderNotice)}
          onDismissBanner={(banner, revision) => dispatch({ type: "dismiss_banner", banner, revision })}
          onDismissNotice={noticeId => dispatch({ type: "dismiss_notice", sid, noticeId })} />
        <SessionStatus state={running ? "running" : "idle"} />
        {space === "code" && <TerminalControl engine="claude" availability="online" />}
        <button className="device-trigger online" aria-label="设备中心"><Icon name="devices" size={18} /><span>Mac</span><i /></button>
        <EngineSelector engine={engine} onChange={setEngine} />
        <button className="iconbtn" aria-label="菜单"><Icon name="dots" /></button>
      </header>
      <ReconnectBanner banner={headerBannerKind(state.banner) ? undefined : state.banner}
        replaying={false} truncated={false} busy={!state.wrapperOnline}
        onDismiss={banner => dispatch({ type: "dismiss_banner", banner })} />
      <NoticeStack notices={runtime.notices.filter(n => !isHeaderNotice(n))}
        onDismiss={noticeId => dispatch({ type: "dismiss_notice", sid, noticeId })} />
      <div style={{ padding: "48px 24px", flex: 1 }}><p>会话内容保留在这里。</p>{controls}</div>
      <textarea aria-label="输入" placeholder="输入 / 命令" style={{ margin: 20, padding: 20, borderRadius: 24 }} />
    </section>
  </div>;
}
createRoot(document.getElementById("root")!).render(<HeaderFeedbackFixture />);
