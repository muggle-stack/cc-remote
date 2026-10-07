import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import type { ComposerPaste } from "../composer-pastes";
import {
  composerPastePreview,
  countTextLines,
  makeComposerPaste,
} from "../composer-pastes";
import { Icon } from "../icons";

interface Props {
  pastes: readonly ComposerPaste[];
  onChange: (pastes: ComposerPaste[]) => void;
  disabled?: boolean;
}

export function PasteCards({ pastes, onChange, disabled = false }: Props) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingChars, setEditingChars] = useState(0);
  const [editingLines, setEditingLines] = useState(1);
  const editorRef = useRef<HTMLTextAreaElement>(null);
  const lineCountTimerRef = useRef<number | null>(null);
  const editingPaste = editingId
    ? pastes.find((paste) => paste.id === editingId) ?? null
    : null;

  const cancelLineCount = () => {
    if (lineCountTimerRef.current === null) return;
    window.clearTimeout(lineCountTimerRef.current);
    lineCountTimerRef.current = null;
  };

  const closeEditor = () => {
    cancelLineCount();
    setEditingId(null);
    setEditingChars(0);
    setEditingLines(1);
  };

  useEffect(() => {
    if (editingId && !pastes.some((paste) => paste.id === editingId)) {
      if (lineCountTimerRef.current !== null) {
        window.clearTimeout(lineCountTimerRef.current);
        lineCountTimerRef.current = null;
      }
      setEditingId(null);
      setEditingChars(0);
      setEditingLines(1);
    }
  }, [editingId, pastes]);

  useEffect(() => () => {
    if (lineCountTimerRef.current !== null) {
      window.clearTimeout(lineCountTimerRef.current);
    }
  }, []);

  if (pastes.length === 0) return null;

  return <>
    {pastes.map((paste) => (
      <article key={paste.id} className="paste-card">
        <button className="paste-open" type="button" disabled={disabled}
          onClick={() => {
            setEditingId(paste.id);
            setEditingChars(paste.chars);
            setEditingLines(paste.lines);
          }}>
          <span className="paste-card-icon"><Icon name="read" size={15} /></span>
          <span className="paste-card-body">
            <span className="paste-card-preview">
              {composerPastePreview(paste.text)}
            </span>
            <span className="paste-card-meta">
              粘贴内容 · {paste.chars} 字符 · {paste.lines} 行
            </span>
          </span>
        </button>
        <button className="attach-x" type="button" aria-label="移除粘贴内容"
          disabled={disabled}
          onClick={() => onChange(pastes.filter(
            (candidate) => candidate.id !== paste.id))}>
          <Icon name="close" size={12} />
        </button>
      </article>
    ))}

    {editingPaste && createPortal(
      <div className="paste-preview-backdrop" role="presentation"
        onMouseDown={(event) => {
          if (event.target === event.currentTarget) closeEditor();
        }}>
        <section className="paste-preview" role="dialog" aria-modal="true"
          aria-label="编辑粘贴内容" onKeyDown={(event) => {
            if (event.key === "Escape") closeEditor();
          }}>
          <header>
            <span>
              <b>编辑粘贴内容</b>
              <small>{editingChars} 字符 · {editingLines} 行</small>
            </span>
            <button type="button" aria-label="关闭" onClick={closeEditor}>
              <Icon name="close" size={16} />
            </button>
          </header>
          <textarea key={editingPaste.id} ref={editorRef}
            defaultValue={editingPaste.text} autoFocus
            onInput={(event) => {
              setEditingChars(event.currentTarget.value.length);
              cancelLineCount();
              lineCountTimerRef.current = window.setTimeout(() => {
                lineCountTimerRef.current = null;
                const editor = editorRef.current;
                if (editor) setEditingLines(countTextLines(editor.value));
              }, 120);
            }} />
          <footer>
            <button type="button" onClick={closeEditor}>取消</button>
            <button type="button" className="primary" disabled={!editingChars}
              onClick={() => {
                const editingText = editorRef.current?.value
                  ?? editingPaste.text;
                onChange(pastes.map((paste) => paste.id === editingPaste.id
                  ? makeComposerPaste(editingText, paste.id)
                  : paste));
                closeEditor();
              }}>保存</button>
          </footer>
        </section>
      </div>, document.body,
    )}
  </>;
}
