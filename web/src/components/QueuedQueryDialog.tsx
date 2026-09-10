import { useEffect, useRef, useState } from "react";

import { Icon } from "../icons";
export { QueuedQueryChip } from "./QueuedQueryChip";

import { MAX_PROMPT_CHARS } from "../composer-pastes";

export interface QueuedQueryEditor {
  sid: string;
  msgId: string;
  preview: string;
  prompt: string | null;
  kind: "queue" | "replace" | null;
  state: "submitting" | "queued" | "failed";
  imageCount: number;
  fileCount: number;
  loading: boolean;
  saving: boolean;
  error: string | null;
}

interface DialogProps {
  editor: QueuedQueryEditor | null;
  onClose: () => void;
  onSave: (prompt: string) => boolean;
  onRetry: () => boolean;
}

export function QueuedQueryDialog({
  editor, onClose, onSave, onRetry,
}: DialogProps) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(
    () => editor?.prompt ?? editor?.preview ?? "");
  const detailLoadedRef = useRef(editor?.prompt != null);
  const wasSavingRef = useRef(false);
  const open = editor !== null;
  const prompt = editor?.prompt ?? null;
  const saving = editor?.saving ?? false;
  const error = editor?.error ?? null;

  useEffect(() => {
    if (prompt === null || detailLoadedRef.current) return;
    detailLoadedRef.current = true;
    setDraft(prompt);
  }, [prompt]);

  useEffect(() => {
    const wasSaving = wasSavingRef.current;
    wasSavingRef.current = saving;
    if (wasSaving && !saving && !error && prompt !== null) {
      setEditing(false);
      setDraft(prompt);
    }
  }, [error, prompt, saving]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !saving) onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose, open, saving]);

  if (!editor) return null;
  const attachmentCount = editor.imageCount + editor.fileCount;
  const emptyDraft = draft.trim().length === 0 && attachmentCount === 0;
  const canEdit = editor.state !== "submitting";
  const canRetry = editor.state === "failed"
    || (editor.state === "queued" && !!editor.error);

  return (
    <>
      <div className="scrim show" onClick={() => {
        if (!editor.saving) onClose();
      }} />
      <section className="sheet show queued-query-sheet" role="dialog"
        aria-modal="true" aria-label="排队消息详情">
        <div className="sheet-grip" />
        <div className="queued-query-head">
          <div>
            <div className="sheet-title">排队消息</div>
            <div className="queued-query-kind">
              {editor.state === "failed"
                ? "提交失败，内容仍保存在当前浏览器"
                : editor.state === "submitting"
                  ? "正在等待服务端确认"
                  : editor.kind === "replace"
                    ? "下一条替换消息"
                    : "按顺序等待发送"}
            </div>
          </div>
          <button type="button" className="iconbtn" onClick={onClose}
            disabled={editor.saving} aria-label="关闭排队消息详情">
            <Icon name="close" size={17} />
          </button>
        </div>

        <div className="queued-query-body">
          {editor.loading ? (
            <div className="queued-query-loading" role="status">
              正在读取完整消息…
            </div>
          ) : editor.prompt === null ? (
            <div className="queued-query-error" role="alert">
              {editor.error ?? "无法读取这条排队消息。"}
            </div>
          ) : editing ? (
            <textarea className="queued-query-editor" value={draft}
              maxLength={MAX_PROMPT_CHARS}
              autoFocus
              aria-label="编辑排队消息"
              onChange={(event) => setDraft(event.target.value)} />
          ) : (
            <div className="queued-query-prompt" data-testid="queued-full-prompt">
              {editor.prompt || "（仅包含附件）"}
            </div>
          )}

          {!editor.loading && editor.prompt !== null && attachmentCount > 0 && (
            <div className="queued-query-attachments">
              {editor.imageCount > 0 && <span>{editor.imageCount} 张图片</span>}
              {editor.fileCount > 0 && <span>{editor.fileCount} 个文件</span>}
              <span>编辑文字不会移除附件</span>
            </div>
          )}
          {!editor.loading && editor.error && editor.prompt !== null && (
            <div className="queued-query-error" role="alert">{editor.error}</div>
          )}
        </div>

          {!editor.loading && editor.prompt !== null && (canEdit || canRetry) && (
          <div className="queued-query-actions">
            {editing ? (
              <>
                <button type="button" className="queued-query-secondary"
                  disabled={editor.saving}
                  onClick={() => {
                    setDraft(editor.prompt ?? "");
                    setEditing(false);
                  }}>
                  取消
                </button>
                <button type="button" className="queued-query-primary"
                  disabled={editor.saving || emptyDraft}
                  onClick={() => {
                    if (onSave(draft) && editor.state === "failed") {
                      setEditing(false);
                    }
                  }}>
                  {editor.saving ? "正在保存…" : "保存修改"}
                </button>
              </>
            ) : (
              <>
                {canEdit && (
                  <button type="button" className="queued-query-secondary"
                    disabled={editor.saving}
                    onClick={() => setEditing(true)}>
                    <Icon name="edit" size={14} />编辑
                  </button>
                )}
                {canRetry && (
                  <button type="button" className="queued-query-primary"
                    disabled={editor.saving}
                    onClick={onRetry}>
                    {editor.saving ? "正在重试…" : "重试发送"}
                  </button>
                )}
              </>
            )}
          </div>
        )}
      </section>
    </>
  );
}
