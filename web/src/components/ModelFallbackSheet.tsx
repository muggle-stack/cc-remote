import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { ModelFallbackPrompt } from "../protocol";
import { EngineIcon } from "../icons";
import type { QuestionDraft } from "./QuestionSheet";
import { QuestionCollapseButton, QuestionResume } from "./QuestionPresentation";
import "./ModelFallbackSheet.css";

function modelLabel(model: string): string {
  return model.replace(/^claude-/, "").replace(/\[1m\]$/i, "")
    .replace(/-(\d)/g, ".$1").replace(/^([a-z]+)/, word => word[0].toUpperCase() + word.slice(1))
    .replace(/\.(\d)/, " $1");
}

export function ModelFallbackSheet({ fallback, draft, sessionLabel, accountLabel, onAnswer,
  errorRevision }: {
  fallback: ModelFallbackPrompt;
  draft?: { key: string; store: Map<string, QuestionDraft> };
  sessionLabel?: string;
  accountLabel?: string;
  errorRevision?: string | number;
  onAnswer: (answer: string) => boolean | void;
}) {
  const [initial] = useState(() => draft?.store.get(draft.key));
  const [collapsed, setCollapsed] = useState(initial?.collapsed ?? false);
  const [automatic, setAutomatic] = useState(initial?.selected.includes("automatic") ?? false);
  const [pending, setPending] = useState(false);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const titleRef = useRef<HTMLDivElement>(null);
  const resumeRef = useRef<HTMLButtonElement>(null);
  const focusResume = useRef(false);
  const save = (nextCollapsed: boolean, nextAutomatic: boolean) => {
    if (!draft) return;
    draft.store.delete(draft.key);
    draft.store.set(draft.key, { text: "", selected: nextAutomatic ? ["automatic"] : [],
      scrollTop: 0, collapsed: nextCollapsed });
    while (draft.store.size > 64) draft.store.delete(draft.store.keys().next().value!);
  };
  const collapse = () => {
    save(true, automatic);
    focusResume.current = true;
    setCollapsed(true);
  };
  useLayoutEffect(() => {
    if (collapsed) {
      if (focusResume.current) resumeRef.current?.focus({ preventScroll: true });
      focusResume.current = false;
      return;
    }
    const dialog = dialogRef.current;
    dialog?.showModal();
    titleRef.current?.focus({ preventScroll: true });
    return () => dialog?.close();
  }, [collapsed]);
  useEffect(() => { setPending(false); }, [errorRevision]);
  const answer = (choice: string) => {
    if (pending || onAnswer(choice) === false) return;
    setPending(true);
  };
  const content = <dialog ref={dialogRef} className="question-dialog model-fallback-dialog"
    aria-labelledby="model-fallback-heading" aria-description={[accountLabel, sessionLabel].filter(Boolean).join(" · ") || undefined}
    data-lock-horizontal-swipe
    onCancel={event => { event.preventDefault(); collapse(); }}
    onKeyDown={event => event.stopPropagation()}>
    <div ref={titleRef} tabIndex={-1} className="question-dialog-title">
      <EngineIcon engine="claude" size={26} />
      <span>Claude 想确认一下</span>
      <QuestionCollapseButton onCollapse={collapse} />
    </div>
    <div className="sheet-scroll fallback-body">
      <div className="qa-header">模型回退</div>
      <h2 id="model-fallback-heading" className="qa-question">是否切换到备用模型？</h2>
      <p className="fallback-description">当前模型拒绝了本次请求。</p>
      <div className="fallback-models">
        <div><span>当前模型</span><strong title={fallback.original_model}>{modelLabel(fallback.original_model)}</strong></div>
        <span className="fallback-arrow" aria-hidden="true">→</span>
        <div><span>备用模型</span><strong title={fallback.fallback_model}>{modelLabel(fallback.fallback_model)}</strong></div>
      </div>
      <label className="fallback-automatic">
        <input type="checkbox" checked={automatic} disabled={pending} onChange={event => {
          setAutomatic(event.target.checked); save(collapsed, event.target.checked);
        }} />
        <span>以后自动切换</span>
      </label>
    </div>
    <div className="qa-footer fallback-actions">
      <button type="button" className="qa-previous" disabled={pending} onClick={() => answer("拒绝回退")}>拒绝回退</button>
      <button type="button" className="qa-previous" disabled={pending || fallback.original_prompt == null}
        onClick={() => answer("编辑提问")}>编辑提问</button>
      <button type="button" className="qa-next" disabled={pending}
        onClick={() => answer(automatic ? "同意回退并记住" : "同意回退")}>
        {pending ? "正在提交…" : "同意回退"}
      </button>
    </div>
  </dialog>;
  return <>
    {collapsed && <QuestionResume engine="claude" buttonRef={resumeRef} label="等待确认模型切换"
      onResume={() => { save(false, automatic); setCollapsed(false); }} />}
    {createPortal(content, document.body)}
  </>;
}
