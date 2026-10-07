import { Profiler, StrictMode, useCallback, useEffect, useRef, useState } from "react";
import { SessionsSidebar } from "../../src/components/SessionsSidebar";
import { ChatView } from "../../src/components/ChatView";
import { useMobileViewport } from "../../src/use-mobile-viewport";
import { SidebarToggle } from "../../src/components/SidebarToggle";
import { EngineSelector } from "../../src/components/EngineSelector";
import { ImageLightbox } from "../../src/components/ImageLightbox";
import { PasteCards } from "../../src/components/PasteCards";
import { makeComposerPaste } from "../../src/composer-pastes";
import { SidebarShell, SidebarState } from "../../src/components/SidebarLayout";
import { useSidebarController } from "../../src/sidebar-state";
import { STREAMING_REMARK_PLUGINS } from "../../src/markdown-math";
import type { SessionInfo } from "../../src/protocol";
import type { Turn } from "../../src/reducer";

const noop = () => {};
const stress = new URLSearchParams(location.search).has("sidebar-load");
const delayedCommit = new URLSearchParams(location.search).has("sidebar-delayed-commit");
const showCode = new URLSearchParams(location.search).has("sidebar-code");
const showOverlays = new URLSearchParams(location.search).has("sidebar-overlays");
const markdown = Array.from({ length: 12 }, (_, index) =>
  `### 检查项 ${index + 1}\n\n正文 **强调** 和 [链接](https://example.com) 保持正常。\n\n`
  + "| 模块 | 状态 |\n| --- | --- |\n| `wrapper` | 正常 |\n| `relay` | 正常 |\n\n"
  + "- 保留历史记录\n- 持续输出内容\n- 手势跟随手指\n"
  + "\n[查看文件](/workspace/sidebar-test.ts:12)\n",
).join("\n");

// Diagnostic only: compare rendering work for the same history and delta load.
const renders: number[] = [];
const parses = { history: 0, live: 0 };
const sceneRenders = { count: 0 };
if (stress) STREAMING_REMARK_PLUGINS.push(() => (_tree: unknown, file: { value: unknown }) => {
  const source = String(file.value);
  if (source.includes("### 检查项")) parses[source.includes("当前输出") ? "live" : "history"]++;
});
Object.assign(window, { sidebarRenderDurations: renders, sidebarMarkdownParses: parses,
  sidebarSceneRenders: sceneRenders });
const sessions: SessionInfo[] = Array.from({ length: 28 }, (_, index) => ({
  session_id: `sidebar-${index}`, summary: ["cc-remote", "手机侧栏动画", "模型与会话管理"][index % 3],
  cwd: `/workspace/project-${Math.floor(index / 4)}`, state: "idle", engine: "codex", space: "code",
}));

function SidebarScene() {
  sceneRenders.count++;
  const sidebar = useSidebarController();
  const setOpen = sidebar.setOpen;
  const [revision, setRevision] = useState(0);
  const [selected, setSelected] = useState("sidebar-0");
  const [preview, setPreview] = useState(false);
  const [pastes, setPastes] = useState(() => [makeComposerPaste("待编辑的粘贴内容", "sidebar-paste")]);
  const pendingOpen = useRef(false);
  const changeOpen = useCallback((next: boolean) => {
    pendingOpen.current = next;
    if (!delayedCommit) setOpen(next);
  }, [setOpen]);
  useMobileViewport();
  useEffect(() => {
    document.documentElement.dataset.theme = "dark";
    document.documentElement.dataset.engine = "codex";
    const update = () => setRevision(value => value + 1);
    const commit = () => setOpen(pendingOpen.current);
    window.addEventListener("sidebar-fixture-stream", update);
    window.addEventListener("sidebar-fixture-commit", commit);
    return () => {
      window.removeEventListener("sidebar-fixture-stream", update);
      window.removeEventListener("sidebar-fixture-commit", commit);
    };
  }, [setOpen]);
  const turns: Turn[] = [{ id: "sidebar-chat", prompt: "侧边栏应该是什么效果？", done: true,
    blocks: [{ kind: "text", message_id: "sidebar-answer", done: true, channel: "final",
      text: "聊天页面跟随手指整体平移，侧栏从左侧露出来。\n\n松开后自然停靠，也可以向左滑动收回。" }],
  }, ...Array.from({ length: 12 }, (_, index): Turn => ({
    id: `history-${index}`, prompt: `历史消息 ${index + 1}`, done: true,
    blocks: [{ kind: "text", message_id: `answer-${index}`, done: true, channel: "final",
      text: stress ? markdown : "保留聊天内容的原始宽度，标题、消息和输入框一起移动。上下滚动仍然浏览历史。" }],
  })), ...(stress ? [{ id: "live", prompt: "持续输出测试", done: false,
    blocks: [{ kind: "text" as const, message_id: "live-answer", done: false,
      text: `${markdown}\n\n当前输出 ${revision}` }],
  }] : [])];
  return <SidebarShell controller={sidebar} className={revision % 2 ? "fixture-streaming" : ""}
    data-revision={revision} data-selected={selected}>
    <SidebarState controller={sidebar}>{open => <SessionsSidebar open={open} onOpenChange={changeOpen} onClose={() => setOpen(false)}
      engine="codex" space="code" profileScopeKey="sidebar-fixture" sessions={sessions}
      activeSessionId={selected} onSelect={id => { setSelected(id); setOpen(false); }}
      onNew={noop} onNewInDir={noop} onSpaceChange={noop} onRename={noop} onArchive={noop}
      onPin={noop} onDelete={noop} onForkWorktree={noop} onMigrate={noop} />}</SidebarState>
    <section className="pane code-pane">
      <header className="c-head">
        <SidebarState controller={sidebar}>{open => <SidebarToggle testId="sidebar-toggle" open={open} onOpenChange={setOpen}>☰</SidebarToggle>}</SidebarState>
        <span className="ttl">Code</span><span className="chip">Codex</span>
        {showOverlays && <EngineSelector engine="codex" onChange={noop} />}
      </header>
      <Profiler id="chat" onRender={(_id, _phase, duration) => { renders.push(duration); }}>
        <ChatView sid="sidebar-fixture" engine="codex" turns={turns} loading={false}
          hasMore={false} onLoadMore={noop} onEdit={noop} onGetDiff={noop}
          onOpenFile={() => { document.body.dataset.fileRevision = String(revision); }}
          activeTurnId={stress ? "live" : null} />
      </Profiler>
      {showCode && <div className="prose" data-testid="sidebar-code" style={{ flexShrink: 0 }}>
        <pre><code>const answer = 42;</code></pre>
      </div>}
      <div data-testid="sidebar-locked" data-lock-horizontal-swipe>面板手势保留</div>
      <div data-testid="sidebar-horizontal" style={{ overflowX: "auto", flexShrink: 0 }}>
        <div style={{ width: 1500 }}>宽表格／账号列表：保留横向滚动</div>
      </div>
      <div className="composer">
        {showOverlays && <>
          <button onClick={() => setPreview(true)}>图片预览</button>
          <PasteCards pastes={pastes} onChange={setPastes} />
          {preview && <ImageLightbox onClose={() => setPreview(false)} alt="预览图片"
            src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='100' height='100'%3E%3Crect width='100' height='100' fill='red'/%3E%3C/svg%3E" />}
        </>}
        <div className="composer-in"><div className="inrow">
          <textarea aria-label="消息" placeholder="输入 / 命令" rows={1} />
        </div><div className="hint">Full Access · GPT-6.1 Sol</div></div>
      </div>
    </section>
  </SidebarShell>;
}

export function MobileSidebarFixture() {
  return <StrictMode><SidebarScene /></StrictMode>;
}
