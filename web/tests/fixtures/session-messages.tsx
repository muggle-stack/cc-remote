import { StrictMode, useEffect, useRef, useState } from "react";
import { ChatView } from "../../src/components/ChatView";
import { SessionMessagePreview } from "../../src/components/SessionMessagePreview";
import { resolveRelatedSession, relatedSessionTitle } from "../../src/session-messages";
import { PROTOCOL_VERSION, type ServerEvent, type SessionInfo } from "../../src/protocol";
import type { Turn } from "../../src/domain/conversation";

const sessions: SessionInfo[] = [
  { session_id: "nyx@source", engine: "codex", space: "code", summary: "cc-remote 开发" },
  { session_id: "nyx@receiver", engine: "codex", space: "code", summary: "代码审查" },
];
const turns: Turn[] = Array.from({ length: 5 }, (_, index) => ({
  id: `review-${index}`, prompt: "请检查跨会话消息的来源标识，以及历史重载后是否会重复。",
  sourceThreadId: "source", done: true,
  blocks: [{ kind: "text", message_id: `answer-${index}`, done: true, channel: "final",
    text: "收到。我会检查来源识别、历史去重和会话跳转。\n\n" + "保持原生消息身份，刷新后只显示一次。\n\n".repeat(5) }],
}));

export function SessionMessagesFixture() {
  const [preview, setPreview] = useState<SessionInfo | null>(null);
  const receive = useRef<((event: ServerEvent) => boolean) | null>(null);
  const [requests, setRequests] = useState<string[]>([]);
  useEffect(() => {
    document.documentElement.dataset.theme = "dark";
    document.documentElement.dataset.engine = "codex";
  }, []);
  return <main style={{ display: "flex", flexDirection: "column", height: "100dvh", background: "var(--bg)" }}>
    <header style={{ padding: 18 }}>cc·remote　/　代码审查</header>
    <ChatView sid="nyx@receiver" engine="codex" turns={turns}
      sessionLink={(id) => ({ title: relatedSessionTitle(resolveRelatedSession(id, "nyx@receiver", sessions), id),
        available: !!resolveRelatedSession(id, "nyx@receiver", sessions) })}
      onOpenSession={(id) => setPreview(resolveRelatedSession(id, "nyx@receiver", sessions) ?? null)} />
    <output data-testid="session-read-commands" style={{ display: "none" }}>{JSON.stringify(requests)}</output>
    {preview && <StrictMode><SessionMessagePreview session={preview} returnSid="nyx@receiver" returnLabel="代码审查" onClose={() => setPreview(null)}
      api={{ receive, detail: () => false, history: (sid, before) => {
        setRequests((commands) => [...commands, "get_history"]);
        setTimeout(() => receive.current?.({ v: PROTOCOL_VERSION, type: "history", ts: 10, sid,
          session_id: sid, revision: "r", generation: "g", before, detail: "summary", events: [],
          has_more: !before, oldest_id: before ? "old" : "send", turns: [{
            id: before ? "old" : "send", prompt: before ? "更早的开发记录" : "让代码审查会话帮忙检查这次改动。",
            done: true, detailEventCount: 0, detailLoaded: false,
            sessionMessages: before ? [] : [{ itemId: "call", threadId: "receiver", status: "sent" }],
            blocks: [{ kind: "text", message_id: "answer", done: true, channel: "final", text: "已请审查会话核对来源识别、历史去重和跳转。" }],
          }],
        }), 25);
        return true;
      } }} /></StrictMode>}
  </main>;
}
