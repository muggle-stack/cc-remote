import { useEffect, useId, useLayoutEffect, useMemo, useReducer, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { Turn } from "../domain/conversation";
import { EngineIcon, Icon } from "../icons";
import { presentAsyncQuestionReplies, supplementalAnswerPrompt } from "../async-question-presentation";
import { AsyncQuestionStore, type AsyncQuestionDraft, type AsyncQuestionEntry } from "../async-question-state";
import { mergeHistoryPageCopies } from "../history-merge";
import { ImeSubmitGuard } from "../ime-submit";
import type { QueryAcceptanceResult } from "../outbox";
import { presentCommandProblem } from "../problem-presentation";
import { QuestionResume } from "./QuestionPresentation";
import QuestionMarkdown from "./QuestionMarkdown";
import "./QuestionSheet.css";

/** Async replies remain ordinary scoped query/steer input. Closing this
 * presentation never answers a native approval or interrupts the running turn. */
function AsyncQuestionDialog({ entry, initialDraft, page, total, onDraftChange,
  onReply, pendingReply, onDismiss, onLater, onNavigate }: {
  entry: AsyncQuestionEntry;
  initialDraft: AsyncQuestionDraft;
  page: number;
  total: number;
  onDraftChange: (draft: AsyncQuestionDraft) => void;
  onReply?: (prompt: string) => Promise<QueryAcceptanceResult> | null;
  pendingReply?: Promise<QueryAcceptanceResult>;
  onDismiss: () => void;
  onLater: () => void;
  onNavigate: (direction: -1 | 1) => void;
}) {
  const id = useId();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const headingRef = useRef<HTMLSpanElement>(null);
  const editorRef = useRef<HTMLTextAreaElement>(null);
  const backdropPressRef = useRef(false);
  const sendingRef = useRef(!!pendingReply);
  const [submission, setSubmission] = useState(pendingReply);
  const imeRef = useRef(new ImeSubmitGuard());
  const [notice, setNotice] = useState("");
  const [draft, setDraft] = useState(initialDraft);
  const draftRef = useRef(draft);
  const initialScroll = useRef(initialDraft.scrollTop);
  const updateDraft = (next: AsyncQuestionDraft) => {
    draftRef.current = next;
    setDraft(next);
    onDraftChange(next);
    setNotice("");
  };
  const saveDraft = () => onDraftChange({ ...draftRef.current,
    text: editorRef.current?.value ?? draftRef.current.text,
    scrollTop: bodyRef.current?.scrollTop ?? draftRef.current.scrollTop });
  const canSubmit = !!(draft.text.trim() || draft.choice);
  const replyHint = notice ? null : submission ? "正在确认回答是否送达。"
    : !onReply ? "当前会话暂不可写，回答草稿会保留。" : null;

  useEffect(() => {
    if (!submission) return;
    let current = true;
    void submission.then(result => {
      if (!current || result.accepted) return;
      sendingRef.current = false;
      setSubmission(undefined);
      setNotice(presentCommandProblem(result.error));
    }, () => {
      if (current) setNotice("暂未确认回答是否送达，请先查看后续消息，避免重复发送。");
    });
    return () => { current = false; };
  }, [submission]);

  useLayoutEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    dialog.showModal();
    headingRef.current?.focus({ preventScroll: true });
    if (bodyRef.current) bodyRef.current.scrollTop = initialScroll.current;
    return () => { dialog.close(); };
  }, []);

  useLayoutEffect(() => {
    const body = bodyRef.current;
    if (!body) return;
    const observer = new ResizeObserver(() => {
      const active = document.activeElement;
      if (!(active instanceof HTMLTextAreaElement) || !body.contains(active)) return;
      const viewport = body.getBoundingClientRect();
      const editor = active.getBoundingClientRect();
      if (editor.height > viewport.height) return;
      const margin = Math.min(4, (viewport.height - editor.height) / 2);
      if (editor.bottom > viewport.bottom - margin) {
        body.scrollTop += Math.ceil(editor.bottom - viewport.bottom + margin);
      } else if (editor.top < viewport.top + margin) {
        body.scrollTop -= Math.ceil(viewport.top - editor.top + margin);
      }
    });
    observer.observe(body);
    return () => observer.disconnect();
  }, []);

  return createPortal(<dialog className="question-dialog question-batch async-question-dialog"
    ref={dialogRef} aria-modal="true" data-lock-horizontal-swipe
    aria-labelledby={`${id}-heading`} aria-describedby={replyHint ? `${id}-hint` : undefined}
    onCancel={event => { event.preventDefault(); saveDraft(); onLater(); }}
    onKeyDown={event => {
      event.stopPropagation();
      if (event.key === "Tab" && !event.altKey && !event.ctrlKey && !event.metaKey) {
        const focusable = [...event.currentTarget.querySelectorAll<HTMLElement>(
          'button:not(:disabled), textarea:not(:disabled), input:not(:disabled), summary, a[href]',
        )].filter(node => node.getClientRects().length > 0
          && (!(node instanceof HTMLInputElement) || node.type !== "radio" || node.checked));
        if (focusable.length) {
          event.preventDefault();
          const current = focusable.indexOf(document.activeElement as HTMLElement);
          const next = current < 0 ? (event.shiftKey ? focusable.length - 1 : 0)
            : (current + (event.shiftKey ? -1 : 1) + focusable.length) % focusable.length;
          focusable[next].focus();
        }
      }
      if (event.key === "Escape" && (imeRef.current.shouldCommitBeforeButtonSubmit()
          || event.nativeEvent.isComposing || event.keyCode === 229)) event.preventDefault();
    }}
    onCompositionStart={() => imeRef.current.startComposition()}
    onCompositionEnd={() => imeRef.current.endComposition()}
    onPointerDown={event => {
      event.stopPropagation();
      backdropPressRef.current = event.target === event.currentTarget;
    }}
    onClick={event => {
      event.stopPropagation();
      if (backdropPressRef.current && event.target === event.currentTarget) {
        const rect = event.currentTarget.getBoundingClientRect();
        if (event.clientX < rect.left || event.clientX > rect.right
            || event.clientY < rect.top || event.clientY > rect.bottom) {
          saveDraft(); onLater();
        }
      }
      backdropPressRef.current = false;
    }}>
    <form aria-label="Codex 的补充问题" onSubmit={event => {
      event.preventDefault();
      event.stopPropagation();
      if (!onReply || sendingRef.current || !dialogRef.current?.open) return;
      const data = new FormData(event.currentTarget);
      const answer = String(data.get("answer") ?? "").trim()
        || String(data.get("option") ?? "").trim();
      if (!answer) { setNotice("请填写回答或选择一个选项。"); return; }
      saveDraft();
      sendingRef.current = true;
      setNotice("");
      const receipt = onReply(supplementalAnswerPrompt([{ question: entry.question.title, answer }]));
      sendingRef.current = !!receipt;
      setSubmission(receipt ?? undefined);
      if (!receipt) setNotice("当前会话暂不可写，回答已保留，请稍后重试。");
    }}>
      <header className="async-question-heading">
        <span id={`${id}-heading`} ref={headingRef} tabIndex={-1}>
          <EngineIcon engine="codex" size={26} />Codex 想确认一下
        </span>
        {total > 1 && <span className="qa-progress" aria-live="polite"
          aria-label={`第 ${page + 1} 题，共 ${total} 题`}>{page + 1} / {total}</span>}
        <button className="async-question-close" type="button" aria-label="忽略当前问题"
          title="忽略当前问题" onClick={onDismiss}><Icon name="close" size={20} /></button>
      </header>
      <div className="async-question-body" ref={bodyRef} onScroll={saveDraft}>
        <fieldset disabled={!onReply || !!submission}>
          <legend><QuestionMarkdown>{entry.question.title}</QuestionMarkdown></legend>
          <div className="qa-options">
            {entry.question.options?.map((option, index) => <label
              className={`qa-opt async-question-option${draft.choice === option ? " selected" : ""}`} key={index}>
              <input type="radio" name="option" value={option} checked={draft.choice === option}
                onChange={() => updateDraft({ ...draftRef.current, choice: option, text: "" })} />
              <span className="qa-opt-label"><QuestionMarkdown>{option}</QuestionMarkdown></span>
            </label>)}
          </div>
          <label className="async-question-label-hidden" htmlFor={`${id}-answer`}>
            {entry.question.options?.length ? "其他回答（填写后替代选项）" : "你的回答"}
          </label>
          <div className="async-question-composer">
            <textarea ref={editorRef} id={`${id}-answer`} name="answer" rows={2}
              value={draft.text} maxLength={8192} placeholder="或输入其他回答…"
              onChange={event => updateDraft({ ...draftRef.current, text: event.target.value,
                choice: event.target.value ? null : draftRef.current.choice })}
              onKeyDown={event => {
                if (!imeRef.current.shouldSubmitKey({ key: event.key, shiftKey: event.shiftKey,
                  isComposing: event.nativeEvent.isComposing, keyCode: event.keyCode })) return;
                event.preventDefault();
                if (!event.repeat) event.currentTarget.form?.requestSubmit();
              }} />
          </div>
        </fieldset>
      </div>
      <footer className="async-question-footer">
        {total > 1 && <nav className="async-question-navigation" aria-label="切换待回答问题">
          <button type="button" className="qa-previous" disabled={page === 0}
            onClick={() => { saveDraft(); onNavigate(-1); }}><span aria-hidden="true">‹</span>上一题</button>
          <button type="button" className="qa-previous" disabled={page === total - 1}
            onClick={() => { saveDraft(); onNavigate(1); }}>下一题<span aria-hidden="true">›</span></button>
        </nav>}
        <div className="async-question-footer-actions">
          <button className="async-question-later" type="button"
            onClick={() => { saveDraft(); onLater(); }}>稍后回答</button>
          <button className="async-question-send" type="submit" disabled={!onReply || !canSubmit || !!submission}>
            {submission ? "等待确认" : "提交回答"}
          </button>
        </div>
        {replyHint && <p id={`${id}-hint`} className="async-question-hint" role="status">{replyHint}</p>}
        {notice && <p className="async-question-notice" role="status">{notice}</p>}
      </footer>
    </form>
  </dialog>, document.body);
}

/** This host lives beside the composer, outside virtualized history. Its
 * device/session-scoped store also survives navigation and native receipts. */
export default function AsyncQuestionHost({ scope, store, turns, historyTurns, pendingReplyId,
  onReply, enabled = true }: {
  scope: string;
  store: AsyncQuestionStore;
  turns: readonly Turn[];
  historyTurns?: readonly Turn[];
  pendingReplyId?: string | null;
  onReply?: (prompt: string) => Promise<QueryAcceptanceResult> | null;
  enabled?: boolean;
}) {
  const session = store.get(scope);
  const [, refresh] = useReducer(value => value + 1, 0);
  const mounted = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const resumeRef = useRef<HTMLButtonElement>(null);
  const focusResume = useRef(false);
  // Browse pages supply older questions while the newest runtime keeps live
  // questions and accepted replies available outside the visible history page.
  const questionTurns = useMemo(() => historyTurns && historyTurns !== turns
    ? mergeHistoryPageCopies([...historyTurns, ...turns], "first").filter((turn): turn is Turn => turn !== null)
    : turns, [turns, historyTurns]);
  const presentation = useMemo(() => presentAsyncQuestionReplies(questionTurns, pendingReplyId), [questionTurns, pendingReplyId]);
  const entries = useMemo(() => {
    const unique = new Map<string, AsyncQuestionEntry>();
    for (const turn of questionTurns) for (const block of turn.blocks) {
      if (block.kind !== "text" || block.delivery !== "async") continue;
      block.questions?.forEach((question, index) => {
        if (presentation.answeredQuestions.get(block.message_id)?.has(index)) return;
        const key = JSON.stringify([block.message_id, index]);
        if (!unique.has(key)) unique.set(key, { key, question });
      });
    }
    return [...unique.values()];
  }, [questionTurns, presentation]);
  const pending = entries.filter(entry => !session.retired.has(entry.key));
  const current = pending.find(entry => entry.key === session.activeKey) ?? pending[0];
  const page = current ? pending.indexOf(current) : 0;
  useLayoutEffect(() => {
    if (!enabled) return;
    const fresh = pending.some(entry => !session.seen.has(entry.key));
    session.seen = new Set(pending.map(entry => entry.key));
    if (current && session.activeKey !== current.key) {
      session.activeKey = current.key;
      if (fresh) session.collapsed = false;
      refresh();
    }
    if (session.collapsed && focusResume.current) {
      focusResume.current = false;
      // WebKit restores the dialog's old focus after close(). Run after that
      // restoration so keyboard users land on the new continuation control.
      const frame = requestAnimationFrame(() => resumeRef.current?.focus({ preventScroll: true }));
      return () => cancelAnimationFrame(frame);
    }
  }, [enabled, pending, current, session]);
  if (!enabled || !current) return null;
  if (session.collapsed) return <QuestionResume engine="codex" buttonRef={resumeRef}
    page={page + 1} total={pending.length} pending={session.submissions.has(current.key)}
    onResume={() => { session.collapsed = false; refresh(); }} />;
  return <AsyncQuestionDialog key={JSON.stringify([current.key, current.question])} entry={current}
    initialDraft={store.draft(scope, current)} page={page} total={pending.length}
    pendingReply={session.submissions.get(current.key)}
    onDraftChange={draft => store.saveDraft(scope, current, draft)}
    onDismiss={() => {
      store.retire(scope, current.key);
      session.activeKey = pending.find(entry => entry.key !== current.key)?.key ?? null;
      session.collapsed = true;
      focusResume.current = true;
      refresh();
    }}
    onLater={() => { session.collapsed = true; focusResume.current = true; refresh(); }}
    onNavigate={direction => { session.activeKey = pending[page + direction].key; refresh(); }}
    onReply={onReply && (prompt => {
      const existing = session.submissions.get(current.key);
      if (existing) return existing;
      const receipt = onReply(prompt);
      if (!receipt) return null;
      session.submissions.set(current.key, receipt);
      void receipt.then(result => {
        if (!store.isCurrent(scope, session)) return;
        session.submissions.delete(current.key);
        if (result.accepted) store.retire(scope, current.key);
        if (mounted.current) refresh();
      }, () => { /* An unknown outcome stays locked after collapse or navigation. */ });
      return receipt;
    })} />;
}
