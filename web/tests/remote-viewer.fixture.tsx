import { useCallback, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "../src/index.css";
import "../src/App.css";
import { RetainedViewerPanels } from "../src/components/RetainedViewerPanels";
import { readViewerSelections, viewerScopeKey, writeViewerSelections } from "../src/remote-viewer";
import { useMobileViewport } from "../src/use-mobile-viewport";
import { ViewerPagesProvider } from "../src/components/ViewerPagesProvider";
import type { ViewerPage } from "../src/remote-viewer";
import { PagePreviewLinks } from "../src/components/PagePreviewLinks";
import { MessageBlock } from "../src/components/MessageBlock";
import type { Turn } from "../src/domain/conversation";
import { useViewerPages } from "../src/viewer-pages-context";
import { ChatView } from "../src/components/ChatView";
import { RightPanelPresentation } from "../src/right-panel-presentation";

function RefreshPages() {
  const pages = useViewerPages();
  return <button disabled={!pages} onClick={() => { void pages?.refresh().catch(() => {}); }}>刷新页面关联</button>;
}

export default function Fixture() {
  useMobileViewport();
  const [sid, setSid] = useState(() => new URLSearchParams(location.search).get("sid") ?? "session-a");
  const [engine, setEngine] = useState("codex");
  const [authenticated, setAuthenticated] = useState(true);
  const [showArtifact, setShowArtifact] = useState(false);
  const [homeArtifact, setHomeArtifact] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [messages, setMessages] = useState(0);
  const [expanded, setExpanded] = useState(false);
  const chatLayout = new URLSearchParams(location.search).has("chat-layout");
  const [values, setValues] = useState(() => readViewerSelections(sessionStorage));
  const scope = { machineId: "render-device", sid, space: "code", engine };
  const key = viewerScopeKey(scope);
  const open = useCallback((page: ViewerPage) => setValues((current) => ({ ...current, [key]: {
    machine_id: page.machine_id, site_id: page.site_id, entry: page.entry,
  } })), [key]);
  const turn: Turn = { id: "page-turn", done: !streaming, ts: 1000, doneTs: 2000, prompt: "做一个页面", blocks: [{
    kind: "text", message_id: "page-answer", done: !streaming,
    channel: "final",
    text: homeArtifact
      ? "页面已做好：[本地页面](http://127.0.0.1:4179/viewer/index.html)。[GitHub](https://github.com/demo/index.html) 保持普通外链。"
      : "页面已做好：[本地页面](viewer/index.html)。[GitHub](https://github.com/demo/index.html) 保持普通外链。",
  }] };
  useEffect(() => {
    document.documentElement.dataset.theme = "light";
    document.documentElement.dataset.engine = "codex";
  }, []);
  useEffect(() => writeViewerSelections(sessionStorage, values), [values]);
  return <ViewerPagesProvider scope={Object.hasOwn(values, key) || showArtifact ? scope : null} onOpen={open}>
    <div className={`shell${Object.hasOwn(values, key) ? " panel-open" : ""}`}>
    <main className="pane"><header className="c-head"><b>Code</b></header>
      {/* Fixture-only controls must leave a usable chat viewport on phones. */}
      <div style={chatLayout
        ? { padding: "12px", display: "flex", flexWrap: "wrap", gap: "8px", maxHeight: "40vh", overflow: "auto", flexShrink: 0 }
        : { padding: "40px", display: "grid", gap: "20px" }}>
        <p>结构已经生成，可以打开右侧预览查看。</p>
        <button onClick={() => setValues((current) => ({ ...current, [key]: null }))}>打开远程预览</button>
        <button onClick={() => setShowArtifact(true)}>显示页面产物</button>
        <button onClick={() => { setStreaming(true); setShowArtifact(true); }}>开始流式页面产物</button>
        {streaming && <button onClick={() => setStreaming(false)}>完成回复</button>}
        <button onClick={() => { setHomeArtifact(true); setShowArtifact(true); }}>显示未登记页面</button>
        {showArtifact && !chatLayout && <><MessageBlock text={turn.blocks[0].kind === "text" ? turn.blocks[0].text : ""} done={!streaming} />
          <PagePreviewLinks sid={sid} turn={turn} /><RefreshPages /></>}
        <button onClick={() => setSid((value) => value === "session-a" ? "session-b" : "session-a")}>切换会话</button>
        <button onClick={() => setEngine(value => value === "codex" ? "claude" : "codex")}>切换引擎</button>
        <button onClick={() => setSid(value => value + "-next")}>下个会话</button>
        <button onClick={() => setAuthenticated(false)}>退出测试</button>
        <button id="append-message" onClick={() => setMessages((value) => value + 1)}>更新对话 {messages}</button>
        <p data-testid="current-sid">{sid}</p>
      </div>
      {chatLayout && showArtifact && <ChatView sid={sid} turns={[turn]} engine="codex" />}
      </main>
    <RightPanelPresentation.Provider value={{ expanded, onExpand: () => setExpanded(true),
      onDock: () => setExpanded(false), onDismiss: () => setExpanded(false) }}>
    {authenticated && <RetainedViewerPanels machineId={scope.machineId} activeScope={Object.hasOwn(values, key) ? scope : null} selections={values}
      devices={[{ machine_id: "render-device", label: "开发设备" }]}
      onSelect={(owner, selection) => setValues((current) => ({ ...current, [owner]: selection }))}
      onClose={owner => setValues((current) => { const next = { ...current }; delete next[owner]; return next; })} />}
    </RightPanelPresentation.Provider>
  </div></ViewerPagesProvider>;
}
createRoot(document.getElementById("root")!).render(<Fixture />);
