import { useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { AsyncQuestionSpec } from "../protocol";
import type { TextBlock, Turn } from "../domain/conversation";
import { Icon } from "../icons";
import { supplementalAnswerPrompt } from "../async-question-presentation";
import { ImeSubmitGuard } from "../ime-submit";

export interface AsyncQuestionDraft {
  choices: (string | null)[];
  texts: string[];
}

/** Nonblocking questions are not approval leases. The native dialog only owns
 * UI focus; replies still use the existing scoped query/steer outbox. */
function AsyncQuestionDialog({ questions, initialDraft,
  onDraftChange, onReply, onClose, answered, replyMode }: {
  questions: AsyncQuestionSpec[];
  initialDraft?: AsyncQuestionDraft;
  onDraftChange: (draft: AsyncQuestionDraft) => void;
  onReply?: (prompt: string) => boolean;
  onClose: () => void;
  answered: boolean;
  replyMode?: "query" | "steer";
}) {
  const id = useId();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const headingRef = useRef<HTMLSpanElement>(null);
  const backdropPressRef = useRef(false);
  const sendingRef = useRef(false);
  const imeRef = useRef(new ImeSubmitGuard());
  const [notice, setNotice] = useState("");
  const [draft, setDraft] = useState<AsyncQuestionDraft>(() => initialDraft ?? {
    choices: questions.map(q => q.options?.[0] ?? null),
    texts: questions.map(() => ""),
  });
  const draftRef = useRef(draft);
  const updateDraft = (next: AsyncQuestionDraft) => {
    draftRef.current = next;
    setDraft(next);
    onDraftChange(next);
    setNotice("");
    sendingRef.current = false;
  };
  const canSubmit = draft.texts.some(text => text.trim()) || draft.choices.some(Boolean);
  const replyHint = !onReply ? "当前会话暂不可写，回答草稿会保留"
    : replyMode === "steer" ? "补充会发送给当前任务，不会中断执行"
    : null;

  useLayoutEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    dialog.showModal();
    // Opening a question must not summon the mobile keyboard or submit the
    // preselected first answer. The browser traps focus and restores it on close.
    headingRef.current?.focus({ preventScroll: true });
    return () => { dialog.close(); };
  }, []);

  useLayoutEffect(() => {
    const body = bodyRef.current;
    if (!body) return;
    // The visual viewport may shrink after textarea focus. Keep its editor
    // within the scrolling body, without scrolling the conversation beneath.
    const observer = new ResizeObserver(() => {
      const active = document.activeElement;
      if (!(active instanceof HTMLTextAreaElement) || !body.contains(active)) return;
      const viewport = body.getBoundingClientRect();
      const editor = active.getBoundingClientRect();
      if (editor.height > viewport.height) return; // Native caret scrolling owns oversized editors.
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

  return createPortal(<dialog className="async-question-dialog" ref={dialogRef} aria-modal="true"
    aria-labelledby={`${id}-heading`} aria-describedby={replyHint ? `${id}-hint` : undefined}
    onCancel={(event) => { event.preventDefault(); onClose(); }}
    onKeyDown={(event) => {
      event.stopPropagation();
      if (event.key === "Tab" && !event.altKey && !event.ctrlKey && !event.metaKey) {
        // Native modality makes the page inert, but browsers can still tab to
        // their chrome. Keep the explicit keyboard loop within this window.
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
          || event.nativeEvent.isComposing || event.keyCode === 229)) {
        event.preventDefault();
      }
    }}
    onCompositionStart={() => imeRef.current.startComposition()}
    onCompositionEnd={() => imeRef.current.endComposition()}
    onPointerDown={(event) => {
      event.stopPropagation();
      backdropPressRef.current = event.target === event.currentTarget;
    }}
    onClick={(event) => {
      event.stopPropagation();
      if (backdropPressRef.current && event.target === event.currentTarget) {
        const rect = event.currentTarget.getBoundingClientRect();
        if (event.clientX < rect.left || event.clientX > rect.right
            || event.clientY < rect.top || event.clientY > rect.bottom) onClose();
      }
      backdropPressRef.current = false;
    }}>
    <form aria-label="助手的补充问题" onSubmit={(event) => {
      event.preventDefault();
      event.stopPropagation();
      if (!onReply || sendingRef.current || !dialogRef.current?.open) return;
      // Keyboard and pointer submits share the same current DOM values and
      // duplicate-send guard, including answers to multiple questions.
      const data = new FormData(event.currentTarget);
      const answers = questions.flatMap((question, index) => {
        const answer = String(data.get(`text-${index}`) ?? "").trim()
          || String(data.get(`option-${index}`) ?? "").trim();
        return answer ? [{ question: question.title, answer }] : [];
      });
      if (!answers.length) { setNotice("请填写至少一个回答。"); return; }
      sendingRef.current = true;
      const sent = onReply(supplementalAnswerPrompt(answers));
      sendingRef.current = sent;
      if (sent) onClose();
      else setNotice("暂时无法发送。回答已保留，请稍后重试。");
    }}>
      <header className="async-question-heading">
        <span id={`${id}-heading`} ref={headingRef} tabIndex={-1}>
          <Icon name="message" size={20} />助手询问
        </span>
        {answered && <span className="async-question-answered">已回答</span>}
        <button className="async-question-close" type="button" aria-label="关闭助手询问" onClick={onClose}>
          <Icon name="close" size={20} />
        </button>
      </header>
      <div className="async-question-body" ref={bodyRef}>
        {questions.map((question, index) => <fieldset key={index} disabled={!onReply}>
          <legend>{question.title}</legend>
          {question.options?.map((option, optionIndex) => <label className="async-question-option" key={optionIndex}>
            <input type="radio" name={`option-${index}`} value={option}
              checked={draft.choices[index] === option}
              onChange={() => updateDraft({
                ...draftRef.current,
                choices: draftRef.current.choices.map((value, i) => i === index ? option : value),
              })} />
            <span>{option}</span><span className="async-question-option-check" aria-hidden="true"><Icon name="check" size={18} /></span>
          </label>)}
          <label className="async-question-label-hidden" htmlFor={`${id}-${index}`}>
            {question.options?.length ? "其他回答（填写后替代选项）" : "你的回答"}
          </label>
          <div className="async-question-composer">
            <textarea id={`${id}-${index}`} name={`text-${index}`} rows={3}
              value={draft.texts[index] ?? ""}
              onChange={(event) => updateDraft({ ...draftRef.current,
                texts: draftRef.current.texts.map((value, i) => i === index ? event.target.value : value),
              })}
              onKeyDown={(event) => {
                if (!imeRef.current.shouldSubmitKey({ key: event.key, shiftKey: event.shiftKey,
                  isComposing: event.nativeEvent.isComposing, keyCode: event.keyCode })) return;
                event.preventDefault();
                if (!event.repeat) event.currentTarget.form?.requestSubmit();
              }}
              maxLength={8192} placeholder="补充一点信息…" />
          </div>
        </fieldset>)}
      </div>
      <footer className="async-question-footer">
        {replyHint && <span id={`${id}-hint`} className={`async-question-hint${onReply && replyMode === "steer" ? " running" : ""}`}>
          {replyHint}
        </span>}
        <div className="async-question-footer-actions">
          <button className="async-question-later" type="button" onClick={onClose}>稍后回答</button>
          <button className="async-question-send" type="submit" disabled={!onReply || !canSubmit}>
            发送回答<Icon name="send" size={16} />
          </button>
        </div>
        {notice && <p className="async-question-notice" role="status">{notice}</p>}
      </footer>
    </form>
  </dialog>, document.body);
}

/** Keep the editor and a bounded draft cache outside virtualized history rows.
 * The host is lazy-loaded on the first click and keyed by device/session scope. */
export default function AsyncQuestionHost({ messageId, turns, answeredMessageIds,
  onReply, onClose, replyMode }: {
  messageId: string | null;
  turns: readonly Turn[];
  answeredMessageIds: ReadonlySet<string>;
  onReply?: (prompt: string) => boolean;
  onClose: () => void;
  replyMode?: "query" | "steer";
}) {
  const drafts = useRef(new Map<string, { signature: string; draft: AsyncQuestionDraft }>());
  const turn = messageId ? turns.find(t => t.blocks.some(block => block.kind === "text"
    && block.delivery === "async" && block.message_id === messageId)) : undefined;
  const block = turn?.blocks.find((b): b is TextBlock => b.kind === "text"
    && b.delivery === "async" && b.message_id === messageId);
  const available = !!block?.questions?.length;
  useLayoutEffect(() => {
    // A removed history item must not reopen itself if a later page restores it.
    if (messageId && !available) onClose();
  }, [messageId, available, onClose]);
  if (!turn || !block?.questions?.length) return null;
  const signature = JSON.stringify(block.questions);
  const stored = drafts.current.get(block.message_id);
  return <AsyncQuestionDialog key={`${block.message_id}:${signature}`}
    questions={block.questions}
    initialDraft={stored?.signature === signature ? stored.draft : undefined}
    answered={answeredMessageIds.has(block.message_id)}
    replyMode={replyMode} onReply={onReply} onClose={onClose}
    onDraftChange={(draft) => {
      drafts.current.delete(block.message_id);
      drafts.current.set(block.message_id, { signature, draft });
      if (drafts.current.size > 32) drafts.current.delete(drafts.current.keys().next().value!);
    }} />;
}
