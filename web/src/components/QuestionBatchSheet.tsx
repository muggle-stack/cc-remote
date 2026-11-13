import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { AskQuestion, Engine } from "../protocol";
import { EngineIcon } from "../icons";
import { useImeSubmit } from "../use-ime-submit";
import type { QuestionDraft } from "./QuestionSheet";
import { QuestionCollapseButton, QuestionResume } from "./QuestionPresentation";

type PageDraft = { text: string; selected: string[]; scrollTop: number };
const emptyPage = (): PageDraft => ({ text: "", selected: [], scrollTop: 0 });
function answerFor(question: AskQuestion, page: PageDraft): string | string[] {
  const text = page.text.trim();
  return question.multi_select
    ? [...new Set([...page.selected, ...(text ? [text] : [])])]
    : text || page.selected[0] || "";
}
function complete(question: AskQuestion, page: PageDraft): boolean {
  const answer = answerFor(question, page);
  return answer.length > 0 && (!Array.isArray(answer) || answer.length <= 5);
}

export function QuestionBatchSheet({ engine, questions, draft, onSubmit }: {
  engine: Engine;
  questions: AskQuestion[];
  draft?: { key: string; store: Map<string, QuestionDraft> };
  onSubmit: (answers: (string | string[])[]) => boolean | void;
}) {
  const engineName = { claude: "Claude", codex: "Codex", dsh: "DSH" }[engine];
  const [initial] = useState(() => {
    const saved = draft?.store.get(draft.key);
    return saved?.signature === JSON.stringify(questions) ? saved : undefined;
  });
  const [page, setPage] = useState(Math.min(initial?.page ?? 0, questions.length - 1));
  const [collapsed, setCollapsed] = useState(initial?.collapsed ?? false);
  const resumeRef = useRef<HTMLButtonElement>(null);
  const focusResume = useRef(false);
  const [pages, setPages] = useState<PageDraft[]>(initial?.pages ?? questions.map(emptyPage));
  const pagesRef = useRef(pages);
  const [pending, setPending] = useState(false);
  const pendingRef = useRef(false);
  const [error, setError] = useState("");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const headingRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const direction = useRef<"previous" | "next">("next");
  const question = questions[page];
  const current = pages[page];
  const last = page === questions.length - 1;

  const save = (next: PageDraft[], index = page, minimized = collapsed) => {
    pagesRef.current = next;
    setPages(next);
    if (!draft) return;
    draft.store.delete(draft.key);
    draft.store.set(draft.key, { ...next[index], page: index, pages: next,
      signature: JSON.stringify(questions), collapsed: minimized });
    // Answers stay in bounded in-memory UI state, never browser storage.
    while (draft.store.size > 64) draft.store.delete(draft.store.keys().next().value!);
  };
  const update = (patch: Partial<PageDraft>) => {
    const next = pagesRef.current.map((value, index) => index === page ? { ...value, ...patch } : value);
    save(next);
    setError("");
  };
  const updateText = (text: string) => update({ text,
    ...(!question.multi_select ? { selected: [] } : {}) });
  const navigate = (value: string) => {
    if (pendingRef.current) return;
    const next = pagesRef.current.map((entry, index) => index === page
      ? { ...entry, text: question.allow_text ? value : "",
        scrollTop: scrollRef.current?.scrollTop ?? 0 } : entry);
    save(next);
    if (direction.current === "previous") {
      const index = Math.max(0, page - 1);
      save(next, index); setPage(index); setError("");
      return;
    }
    if (!complete(question, next[page])) return;
    if (!last) {
      save(next, page + 1); setPage(page + 1); setError("");
      return;
    }
    const missing = questions.findIndex((q, index) => !complete(q, next[index]));
    if (missing !== -1) { save(next, missing); setPage(missing); return; }
    const answers = questions.map((q, index) => answerFor(q, next[index]));
    if (onSubmit(answers) === false) {
      setError("回答暂未发送，请重试。");
      return;
    }
    pendingRef.current = true; setPending(true); setError("");
    // Only the authoritative ask_user_closed retires this form. Retain all
    // drafts on transport/service failure; retry uses the same pending ask_id.
    timer.current = setTimeout(() => {
      pendingRef.current = false; setPending(false);
      setError("尚未收到确认，可重试提交。");
    }, 5000);
  };
  const ime = useImeSubmit<HTMLInputElement>(navigate);
  const preserveDraft = useRef(() => {});
  preserveDraft.current = () => save(pagesRef.current.map((value, index) => index === page ? {
    ...value, text: ime.inputRef.current?.value ?? value.text,
    scrollTop: scrollRef.current?.scrollTop ?? value.scrollTop,
  } : value));
  useEffect(() => () => { if (timer.current !== null) clearTimeout(timer.current); }, []);
  useLayoutEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (collapsed) {
      if (focusResume.current) resumeRef.current?.focus({ preventScroll: true });
      focusResume.current = false;
      return;
    }
    dialog.showModal();
    return () => {
      preserveDraft.current();
      dialog.close();
    };
  }, [collapsed]);
  useLayoutEffect(() => {
    if (collapsed) return;
    if (scrollRef.current) scrollRef.current.scrollTop = pagesRef.current[page].scrollTop;
    // Choice pages do not summon a keyboard. Keep free-text-only accessibility.
    const target = question.allow_text && question.options.length === 0
      ? ime.inputRef.current : headingRef.current;
    target?.focus({ preventScroll: true });
  }, [page, question, ime.inputRef, collapsed]);
  useLayoutEffect(() => {
    const body = scrollRef.current;
    if (!body) return;
    const observer = new ResizeObserver(() => {
      if (!dialogRef.current?.open) return;
      const input = ime.inputRef.current;
      if (!input || document.activeElement !== input) return;
      const viewport = body.getBoundingClientRect();
      const rect = input.getBoundingClientRect();
      if (rect.height > viewport.height) return;
      const margin = Math.min(4, (viewport.height - rect.height) / 2);
      if (rect.bottom > viewport.bottom - margin) body.scrollTop += Math.ceil(rect.bottom - viewport.bottom + margin);
      else if (rect.top < viewport.top + margin) body.scrollTop -= Math.ceil(viewport.top - rect.top + margin);
    });
    observer.observe(body);
    return () => observer.disconnect();
  }, [ime.inputRef]);
  const content = <dialog ref={dialogRef} className="question-dialog question-batch"
    aria-label="操作确认" aria-modal={!collapsed || undefined} data-lock-horizontal-swipe
    onCancel={event => event.preventDefault()} onKeyDown={event => event.stopPropagation()}>
    <div ref={headingRef} tabIndex={-1} className="question-dialog-title">
      <EngineIcon engine={engine} size={26} />
      <span>{engineName} 想确认一下</span>
      {questions.length > 1 && <span className="qa-progress" aria-live="polite"
        aria-label={`第 ${page + 1} 题，共 ${questions.length} 题`}>{page + 1} / {questions.length}</span>}
      <QuestionCollapseButton onPointerDown={ime.commitCompositionBeforePointerSubmit}
        onCollapse={() => {
          const next = pagesRef.current.map((value, index) => index === page ? {
            ...value, text: ime.inputRef.current?.value ?? value.text,
            scrollTop: scrollRef.current?.scrollTop ?? value.scrollTop,
          } : value);
          save(next, page, true);
          focusResume.current = true;
          setCollapsed(true);
        }} />
    </div>
    <div ref={scrollRef} className="sheet-scroll" onScroll={() => {
      if (!dialogRef.current?.open) return;
      const scrollTop = scrollRef.current?.scrollTop ?? 0;
      if (scrollTop !== pagesRef.current[page].scrollTop) update({ scrollTop });
    }}>
      {(question.header || question.multi_select) && <div className="qa-page-meta">
        <span>{question.header}</span>{question.multi_select && <span>可多选</span>}
      </div>}
      <div className="qa-question">{question.question}</div>
      <div className="qa-options">
        {question.options.map(option => <button key={option.label}
          className={`qa-opt${current.selected.includes(option.label) ? " selected" : ""}`}
          aria-pressed={current.selected.includes(option.label)} disabled={pending}
          onClick={() => update(question.multi_select ? {
            selected: current.selected.includes(option.label)
              ? current.selected.filter(label => label !== option.label)
              : [...current.selected, option.label],
          } : { selected: [option.label], text: "" })}>
          <span className="qa-opt-label">{option.label}</span>
          {option.ds && <span className="qa-opt-ds">{option.ds}</span>}
        </button>)}
      </div>
      {question.allow_text && <div className="qa-text-answer">
        <input ref={ime.inputRef} type={question.secret ? "password" : "text"}
          value={current.text} disabled={pending} maxLength={4096}
          aria-label={question.secret ? "输入敏感内容" : "输入回答"}
          placeholder={question.secret ? "输入敏感内容" : question.multi_select ? "补充其他回答…" : "或输入其他回答…"}
          onClick={() => {
            if (!question.multi_select && pagesRef.current[page].selected.length) update({ selected: [] });
          }}
          onChange={event => updateText(event.target.value)}
          onCompositionStart={ime.startComposition}
          onCompositionEnd={event => { ime.endComposition(); updateText(event.currentTarget.value); }}
          onKeyDown={event => {
            if (!ime.shouldSubmitKey({ key: event.key, shiftKey: event.shiftKey,
              isComposing: event.nativeEvent.isComposing, keyCode: event.nativeEvent.keyCode })) return;
            event.preventDefault(); direction.current = "next"; ime.requestSubmit();
          }} />
      </div>}
      {error && <div className="qa-submit-error" role="status">{error}</div>}
    </div>
    <div className="qa-footer">
      {questions.length > 1 && <button className="qa-previous" disabled={page === 0 || pending}
        onPointerDown={ime.commitCompositionBeforePointerSubmit}
        onClick={() => { direction.current = "previous"; ime.requestSubmit(); }}>
        <span aria-hidden="true">‹</span>上一题
      </button>}
      <button className="qa-next" disabled={pending || !complete(question, current)}
        onPointerDown={ime.commitCompositionBeforePointerSubmit}
        onClick={() => { direction.current = "next"; ime.requestSubmit(); }}>
        {pending ? "提交中…" : last ? "提交回答" : "下一题"}
        {!last && <span aria-hidden="true">›</span>}
      </button>
    </div>
  </dialog>;
  return <>
    {collapsed && <QuestionResume engine={engine} buttonRef={resumeRef}
      page={page + 1} total={questions.length} pending={pending} onResume={() => {
        save(pagesRef.current, page, false); setCollapsed(false);
      }} />}
    {typeof document === "undefined" ? content : createPortal(content, document.body)}
  </>;
}
