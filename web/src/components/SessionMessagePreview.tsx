import { useCallback, useEffect, useReducer, useRef } from "react";
import type { ServerEvent, SessionInfo } from "../protocol";
import { SessionMessageReader } from "../session-message-reader";
import { relatedSessionTitle } from "../session-messages";
import { ChatView } from "./ChatView";
import { Icon } from "../icons";
import "./session-messages.css";

export interface SessionMessagePreviewApi {
  history: (sid: string, before: string | null, cwd?: string | null) => boolean;
  detail: (sid: string, turnId: string, revision: string, before: string | null) => boolean;
  receive: { current: ((event: ServerEvent) => boolean) | null };
}

export function SessionMessagePreview({ session, returnSid, returnLabel, api, onClose }: {
  session: SessionInfo;
  returnLabel: string;
  returnSid: string;
  api: SessionMessagePreviewApi;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const readerRef = useRef<SessionMessageReader | null>(null);
  if (!readerRef.current) readerRef.current = new SessionMessageReader(session.session_id);
  const reader = readerRef.current;
  const [, repaint] = useReducer((n: number) => n + 1, 0);
  const timers = useRef(new Set<ReturnType<typeof setTimeout>>());
  const apiRef = useRef(api);
  apiRef.current = api;

  const armTimeout = useCallback(() => {
    const timer = setTimeout(() => {
      timers.current.delete(timer);
      reader.fail("会话读取超时，请重试。");
      repaint();
    }, 15_000);
    timers.current.add(timer);
  }, [reader]);
  const clearTimers = useCallback(() => {
    for (const timer of timers.current) clearTimeout(timer);
    timers.current.clear();
  }, []);
  const load = useCallback((page: number) => {
    const request = reader.requestPage(page);
    if (!request) return;
    clearTimers();
    if (!apiRef.current.history(reader.sid, request.before, session.cwd)) reader.fail();
    else armTimeout();
    repaint();
  }, [reader, session.cwd, clearTimers, armTimeout]);
  useEffect(() => {
    const node = dialog.current;
    const previousFocus = document.activeElement instanceof HTMLElement
      ? document.activeElement : null;
    node?.showModal();
    const receive = (event: ServerEvent) => {
      if (!reader.accept(event)) return false;
      clearTimers();
      repaint();
      return event.type !== "history_invalidated";
    };
    const receiveRef = apiRef.current.receive;
    receiveRef.current = receive;
    load(0);
    return () => {
      clearTimers();
      // Also cancel the private pending state: StrictMode may immediately
      // set this effect up again with the same reader instance.
      reader.fail();
      if (receiveRef.current === receive) receiveRef.current = null;
      node?.close();
      previousFocus?.focus({ preventScroll: true });
    };
  }, [reader, load, clearTimers]);

  return <dialog ref={dialog} className="session-message-preview"
    aria-label="关联会话历史" onKeyDown={(event) => event.stopPropagation()} onCancel={(event) => { event.preventDefault(); event.stopPropagation(); onClose(); }}>
    <header className="session-message-preview-header">
      <button type="button" onClick={onClose} className="session-message-back">
        <Icon name="back" size={16} /><span>返回{returnLabel}</span>
      </button>
      <strong>{relatedSessionTitle(session, session.session_id)}</strong>
    </header>
    {reader.error && <div role="alert" className="session-message-error">
      <span>{reader.error}</span><button type="button" onClick={() => load(0)}>重新读取</button>
    </div>}
    <ChatView key={`${reader.page}:${reader.revision}`} sid={reader.sid}
      engine="codex" turns={reader.turns} loading={reader.loading}
      sessionLink={(id) => id === returnSid.slice(returnSid.indexOf("@") + 1)
        ? { title: returnLabel, available: true }
        : { title: id.slice(0, 8), available: false }}
      onOpenSession={onClose}
      historyRevision={reader.revision} historyGeneration={reader.generation}
      onLoadDetail={(turnId, before) => {
        if (!reader.requestDetail(turnId, before ?? null)) return false;
        if (!apiRef.current.detail(reader.sid, turnId, reader.revision!, before ?? null)) reader.fail();
        else armTimeout();
        repaint();
        return true;
      }} />
    <footer className="session-message-preview-pages">
      <button type="button" disabled={reader.loading || !reader.hasMore || !!reader.error}
        onClick={() => load(reader.page + 1)}>更早记录</button>
      <span>只读查看</span>
      <button type="button" disabled={reader.loading || reader.page === 0 || !!reader.error}
        onClick={() => load(reader.page - 1)}>较新记录</button>
    </footer>
  </dialog>;
}
