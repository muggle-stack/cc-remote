import { useEffect, useRef, useState } from "react";
import { ChatView } from "./ChatView";
import { Icon } from "../icons";
import { PanelTabs } from "./PanelTabs";
import { NoticeStack } from "./NoticeStack";
import type { Artifact, SessionRuntime } from "../reducer";
import { ImeSubmitGuard } from "../ime-submit";

/** /btw side panel: a mini chat over an ephemeral fork of the current session.
 * Reuses ChatView for the transcript; a minimal textarea for input. Closing
 * discards the fork (the main thread never sees any of this). */
export function BtwPanel({ sid, rt, engine, opening, active, hasArtifact,
  artifactKind, onTab, onSend, onOpenFile, onClose, onDismissNotice }: {
  sid?: string;
  rt: SessionRuntime | undefined;
  engine?: string;
  opening?: boolean;   // fork still spawning (no sid yet) — show a spinner
  active: "diff" | "btw";
  hasArtifact: boolean;
  artifactKind?: Artifact["kind"];
  onTab: (v: "diff" | "btw") => void;
  onSend: (prompt: string) => void;
  onOpenFile?: (path: string, line?: number) => void;
  onClose: () => void;
  onDismissNotice: (noticeId: string) => void;
}) {
  const [text, setText] = useState("");
  const taRef = useRef<HTMLTextAreaElement>(null);
  const imeSubmitRef = useRef(new ImeSubmitGuard());
  const buttonSendTimerRef = useRef<number | null>(null);
  const turns = rt?.turns ?? [];
  const busy = !!opening || rt?.state === "running";

  useEffect(() => () => {
    if (buttonSendTimerRef.current !== null) {
      window.clearTimeout(buttonSendTimerRef.current);
    }
  }, []);

  const send = (value = taRef.current?.value ?? text) => {
    const t = value.trim();
    if (!t || busy) return;
    onSend(t);
    setText("");
    if (taRef.current) taRef.current.style.height = "auto";
  };
  const requestButtonSend = () => {
    if (buttonSendTimerRef.current !== null) return;
    buttonSendTimerRef.current = window.setTimeout(() => {
      buttonSendTimerRef.current = null;
      send();
    }, 0);
  };
  const grow = (el: HTMLTextAreaElement) => { el.style.height = "auto"; el.style.height = Math.min(el.scrollHeight, 120) + "px"; };

  return (
    <div className="btw-panel">
      <div className="btw-head">
        {hasArtifact
          ? <PanelTabs active={active} artifactKind={artifactKind} onTab={onTab} />
          : <div className="btw-titles">
              <span className="btw-title">btw · 侧边对话{engine === "codex" ? " · Codex" : ""}</span>
              <span className="btw-sub">基于当前会话上下文,不写回主线</span>
            </div>}
        <button className="iconbtn" onClick={onClose} aria-label="关闭 btw" title="关闭并丢弃这个侧边对话">
          <Icon name="chevrons-right" />
        </button>
      </div>
      <NoticeStack notices={rt?.notices ?? []} onDismiss={onDismissNotice} />
      <div className="btw-body">
        {opening
          ? <div className="btw-empty"><span className="thinking"><span/><span/><span/></span> 正在打开侧边对话…</div>
          : turns.length === 0
            ? <div className="btw-empty">问一个基于当前会话的侧边问题 —— 回答不会写进主线,关闭即丢弃。</div>
            : <ChatView sid={sid ?? null} turns={turns} onEdit={() => {}}
                onGetDiff={() => {}} onOpenFile={onOpenFile} />}
      </div>
      <div className="btw-input">
        <textarea
          ref={taRef}
          value={text}
          placeholder={opening ? "正在打开…" : rt?.state === "running" ? "回答中…" : "问点什么(Enter 发送 · Shift+Enter 换行)"}
          rows={1}
          onChange={(e) => { setText(e.target.value); grow(e.target); }}
          onCompositionStart={() => imeSubmitRef.current.startComposition()}
          onCompositionEnd={(e) => {
            imeSubmitRef.current.endComposition();
            setText(e.currentTarget.value);
          }}
          onKeyDown={(e) => {
            if (!imeSubmitRef.current.shouldSubmitKey({
              key: e.key, shiftKey: e.shiftKey,
              isComposing: e.nativeEvent.isComposing, keyCode: e.nativeEvent.keyCode,
            })) return;
            e.preventDefault();
            send(e.currentTarget.value);
          }}
        />
        <button className="btw-send"
          onPointerDown={() => {
            if (imeSubmitRef.current.shouldCommitBeforeButtonSubmit()) taRef.current?.blur();
          }}
          onClick={requestButtonSend}
          disabled={busy || !text.trim()} aria-label="发送">
          <Icon name="send" size={18} />
        </button>
      </div>
    </div>
  );
}
