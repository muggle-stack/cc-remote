import { StrictMode, useEffect, useState } from "react";
import { SessionsSidebar } from "../../src/components/SessionsSidebar";
import { ChatView } from "../../src/components/ChatView";
import { useMobileViewport } from "../../src/use-mobile-viewport";
import type { SessionInfo } from "../../src/protocol";
import type { Turn } from "../../src/reducer";

const noop = () => {};
const sessions: SessionInfo[] = Array.from({ length: 28 }, (_, index) => ({
  session_id: `sidebar-${index}`, summary: ["cc-remote", "手机侧栏动画", "模型与会话管理"][index % 3],
  cwd: `/workspace/project-${Math.floor(index / 4)}`, state: "idle", engine: "codex", space: "code",
}));

function SidebarScene() {
  const [open, setOpen] = useState(false);
  const [revision, setRevision] = useState(0);
  const [selected, setSelected] = useState("sidebar-0");
  useMobileViewport();
  useEffect(() => {
    document.documentElement.dataset.theme = "dark";
    document.documentElement.dataset.engine = "codex";
    const update = () => setRevision(value => value + 1);
    window.addEventListener("sidebar-fixture-stream", update);
    return () => window.removeEventListener("sidebar-fixture-stream", update);
  }, []);
  const turns: Turn[] = [{ id: "sidebar-chat", prompt: "侧边栏应该是什么效果？", done: true,
    blocks: [{ kind: "text", message_id: "sidebar-answer", done: true, channel: "final",
      text: "聊天页面跟随手指整体平移，侧栏从左侧露出来。\n\n松开后自然停靠，也可以向左滑动收回。" }],
  }, ...Array.from({ length: 12 }, (_, index): Turn => ({
    id: `history-${index}`, prompt: `历史消息 ${index + 1}`, done: true,
    blocks: [{ kind: "text", message_id: `answer-${index}`, done: true, channel: "final",
      text: "保留聊天内容的原始宽度，标题、消息和输入框一起移动。上下滚动仍然浏览历史。" }],
  }))];
  return <div className={`shell${open ? " sidebar-open" : ""}${revision % 2 ? " fixture-streaming" : ""}`}
    data-revision={revision} data-selected={selected}>
    <SessionsSidebar open={open} onOpenChange={setOpen} onClose={() => setOpen(false)}
      engine="codex" space="code" profileScopeKey="sidebar-fixture" sessions={sessions}
      activeSessionId={selected} onSelect={id => { setSelected(id); setOpen(false); }}
      onNew={noop} onNewInDir={noop} onSpaceChange={noop} onRename={noop} onArchive={noop}
      onPin={noop} onDelete={noop} onForkWorktree={noop} onMigrate={noop} />
    <section className="pane code-pane">
      <header className="c-head">
        <button type="button" data-testid="sidebar-toggle" onClick={() => setOpen(value => !value)}>☰</button>
        <span className="ttl">Code</span><span className="chip">Codex</span>
      </header>
      <ChatView sid="sidebar-fixture" engine="codex" turns={turns} loading={false}
        hasMore={false} onLoadMore={noop} onEdit={noop} onGetDiff={noop} activeTurnId={null} />
      <div data-testid="sidebar-locked" data-lock-horizontal-swipe>面板手势保留</div>
      <div data-testid="sidebar-horizontal" style={{ overflowX: "auto", flexShrink: 0 }}>
        <div style={{ width: 1500 }}>宽表格／账号列表：保留横向滚动</div>
      </div>
      <div className="composer">
        <div className="composer-in"><div className="inrow">
          <textarea aria-label="消息" placeholder="输入 / 命令" rows={1} />
        </div><div className="hint">Full Access · GPT-6.1 Sol</div></div>
      </div>
    </section>
  </div>;
}

export function MobileSidebarFixture() {
  return <StrictMode><SidebarScene /></StrictMode>;
}
