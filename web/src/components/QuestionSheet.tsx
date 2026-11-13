import ReactMarkdown from "react-markdown";
import { useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { AskOption, AskQuestion, Engine } from "../protocol";
import { EngineIcon } from "../icons";
import { useImeSubmit } from "../use-ime-submit";
import { QuestionBatchSheet } from "./QuestionBatchSheet";
import { QuestionCollapseButton, QuestionResume } from "./QuestionPresentation";
import "./QuestionSheet.css";

export interface QuestionDraft {
  text: string;
  selected: string[];
  scrollTop: number;
  page?: number;
  pages?: { text: string; selected: string[]; scrollTop: number }[];
  signature?: string;
  collapsed?: boolean;
}

interface Props {
  engine: Engine;
  question: string;
  planDocument?: string | null;
  header?: string | null;
  sourceLabel?: string;
  submissionError?: string | null;
  options: AskOption[];
  allowText?: boolean;
  secret?: boolean;
  multiSelect?: boolean;
  questions?: AskQuestion[] | null;
  draft?: { key: string; store: Map<string, QuestionDraft> };
  onAnswer: (answer: string | string[]) => boolean | void;
  onAnswers?: (answers: (string | string[])[]) => boolean | void;
}

export function QuestionSheet(props: Props) {
  return props.questions?.length ? <QuestionBatchSheet key={JSON.stringify(props.questions)}
    engine={props.engine} questions={props.questions} draft={props.draft} sourceLabel={props.sourceLabel}
    submissionError={props.submissionError}
    onSubmit={props.onAnswers ?? (() => false)} /> : <SingleQuestionSheet {...props} />;
}

function SingleQuestionSheet({
  engine, question, header, sourceLabel, submissionError, options, allowText, secret, multiSelect, onAnswer, draft, planDocument,
}: Props) {
  const engineName = { claude: "Claude", codex: "Codex", dsh: "DSH" }[engine];
  const dialogRef = useRef<HTMLDialogElement>(null);
  const headingRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [initialDraft] = useState(() => draft?.store.get(draft.key));
  const [collapsed, setCollapsed] = useState(initialDraft?.collapsed ?? false);
  const resumeRef = useRef<HTMLButtonElement>(null);
  const focusResume = useRef(false);
  const savedScroll = useRef(initialDraft?.scrollTop ?? 0);
  const focusTextOnOpen = useRef(!initialDraft && allowText && options.length === 0);
  const [text, setText] = useState(initialDraft?.text ?? "");
  const [selected, setSelected] = useState<string[]>(initialDraft?.selected ?? []);
  const permissionGroup = useId();
  const permissionOptions = options.filter(option => option.label !== "继续修改");
  const selectedPermission = permissionOptions.find(option => option.label === selected[0])?.label
    ?? permissionOptions[0]?.label;
  const saveDraft = (next: Partial<QuestionDraft>) => {
    if (!draft) return;
    const value: QuestionDraft = { text, selected,
      scrollTop: savedScroll.current, collapsed, ...next };
    draft.store.delete(draft.key);
    draft.store.set(draft.key, value);
    // Only in-memory UI state; never persist answers (including secrets).
    while (draft.store.size > 64) draft.store.delete(draft.store.keys().next().value!);
  };
  const updateText = (value: string) => { setText(value); saveDraft({ text: value }); };
  const answer = (value: string | string[]) => {
    onAnswer(value);
  };
  const imeSubmit = useImeSubmit<HTMLInputElement>((value) => {
    if (!value.trim()) return;
    answer(multiSelect ? [...selected, value] : value);
  });
  const preserveDraft = useRef(() => {});
  preserveDraft.current = () => saveDraft({
    text: imeSubmit.inputRef.current?.value ?? text,
    scrollTop: scrollRef.current?.scrollTop ?? savedScroll.current,
  });
  const toggle = (label: string) => {
    const next = selected.includes(label)
      ? selected.filter((value) => value !== label) : [...selected, label];
    setSelected(next);
    saveDraft({ selected: next });
  };
  useLayoutEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (collapsed) {
      if (focusResume.current) resumeRef.current?.focus({ preventScroll: true });
      focusResume.current = false;
      return;
    }
    dialog.showModal();
    // Keep the original text-only autofocus; choice questions open without a keyboard.
    const target = focusTextOnOpen.current ? imeSubmit.inputRef.current : headingRef.current;
    target?.focus({ preventScroll: true });
    focusTextOnOpen.current = false;
    if (scrollRef.current) scrollRef.current.scrollTop = savedScroll.current;
    return () => {
      preserveDraft.current();
      dialog.close();
    };
  }, [imeSubmit.inputRef, collapsed]);
  useLayoutEffect(() => {
    const body = scrollRef.current;
    if (!body) return;
    const observer = new ResizeObserver(() => {
      if (!dialogRef.current?.open) return;
      const input = imeSubmit.inputRef.current;
      if (!input || document.activeElement !== input) return;
      const viewport = body.getBoundingClientRect();
      const editor = input.getBoundingClientRect();
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
  }, [imeSubmit.inputRef]);
  const content = (
      <dialog ref={dialogRef} className="question-dialog" aria-label="操作确认" aria-modal={!collapsed || undefined}
        onCancel={(event) => event.preventDefault()}
        onKeyDown={(event) => event.stopPropagation()} data-lock-horizontal-swipe>
        <div ref={headingRef} tabIndex={-1} className="question-dialog-title">
          <EngineIcon engine={engine} size={26} />
          <span>{engineName} 想确认一下</span>
          <QuestionCollapseButton onPointerDown={imeSubmit.commitCompositionBeforePointerSubmit}
            onCollapse={() => {
              // Keep this component mounted: hiding the dialog does not answer,
              // cancel, or replace the native callback waiting on the server.
              const value = imeSubmit.inputRef.current?.value ?? text;
              setText(value);
              savedScroll.current = scrollRef.current?.scrollTop ?? savedScroll.current;
              saveDraft({ text: value, collapsed: true });
              focusResume.current = true;
              setCollapsed(true);
            }} />
        </div>
        <div ref={scrollRef} className="sheet-scroll" onScroll={() => {
          if (!dialogRef.current?.open) return;
          savedScroll.current = scrollRef.current?.scrollTop ?? savedScroll.current;
          saveDraft({});
        }}>
          {sourceLabel && <div className="qa-source">{sourceLabel}</div>}
          {submissionError && <p className="qa-submit-error" role="alert">{submissionError}</p>}
          {header && <div className="qa-header">{header}</div>}
          <div className="qa-question">{question}</div>
          {planDocument != null && <div className="plan-approval">
            {planDocument ? <div className="plan-document"><ReactMarkdown skipHtml>{planDocument}</ReactMarkdown></div>
              : <p>本次请求未附带计划正文。</p>}
          </div>}
          {planDocument == null && <div className="qa-options">
            {options.map((o, i) => (
              <button key={i}
                className={`qa-opt${selected.includes(o.label) ? " selected" : ""}`}
                aria-pressed={multiSelect ? selected.includes(o.label) : undefined}
                onClick={() => multiSelect ? toggle(o.label) : answer(o.label)}>
                <span className="qa-opt-label">{o.label}</span>
                {o.ds && <span className="qa-opt-ds">{o.ds}</span>}
              </button>
            ))}
            {multiSelect && (
              <button className="qa-multi-submit" disabled={selected.length === 0}
                onClick={() => answer(selected)}>确定</button>
            )}
          </div>}
          {allowText && <div className="qa-text-answer">
            <input ref={imeSubmit.inputRef} type={secret ? "password" : "text"} value={text}
              autoFocus={focusTextOnOpen.current} placeholder={secret ? "输入敏感内容" : "输入回答"}
              onChange={(e) => updateText(e.target.value)}
              onCompositionStart={imeSubmit.startComposition}
              onCompositionEnd={(e) => {
                imeSubmit.endComposition();
                updateText(e.currentTarget.value);
              }}
              onKeyDown={(e) => {
                if (!imeSubmit.shouldSubmitKey({
                  key: e.key,
                  shiftKey: e.shiftKey,
                  isComposing: e.nativeEvent.isComposing,
                  keyCode: e.nativeEvent.keyCode,
                })) return;
                e.preventDefault();
                imeSubmit.requestSubmit();
              }} />
            <button disabled={!text.trim()}
              onPointerDown={imeSubmit.commitCompositionBeforePointerSubmit}
              onClick={imeSubmit.requestSubmit}>确定</button>
          </div>}
        </div>
        {planDocument != null && <div className="plan-approval plan-approval-footer">
            <fieldset className="plan-permission-picker">
              <legend>批准后使用的权限</legend>
              <div className="plan-permission-options">
                {permissionOptions.map(option => <label key={option.label}
                  className={`plan-permission-option${selectedPermission === option.label ? " selected" : ""}`}>
                  <input type="radio" name={permissionGroup} value={option.label}
                    checked={selectedPermission === option.label} onChange={() => {
                      const next = [option.label]; setSelected(next); saveDraft({ selected: next });
                    }} />
                  <span className="plan-permission-indicator" aria-hidden="true" />
                  <span>{option.label}</span>
                </label>)}
              </div>
            </fieldset>
            <div className="plan-approval-actions">
              <button type="button" onClick={() => answer("继续修改")}>继续修改</button>
              <button type="button" className="primary" disabled={!selectedPermission}
                onClick={() => { if (selectedPermission) answer(selectedPermission); }}>批准计划</button>
            </div>
        </div>}
      </dialog>
  );
  return <>
    {collapsed && <QuestionResume engine={engine} sourceLabel={sourceLabel} buttonRef={resumeRef} onResume={() => {
      saveDraft({ collapsed: false }); setCollapsed(false);
    }} />}
    {typeof document === "undefined" ? content : createPortal(content, document.body)}
  </>;
}
