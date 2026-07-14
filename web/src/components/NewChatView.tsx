// Empty-state "new chat" page: a centered composer (a la Claude app / Codex)
// with a working directory and optional attachments. Model, effort, and Codex
// modes use the local defaults; users can change them after the session starts.
import { useEffect, useRef, useState, type ClipboardEvent } from "react";
import { Icon } from "../icons";
import { attachmentBytes, pickFiles } from "../img";
import type { CodexPermissionMode, CodexServiceTier, CollaborationModeName, QueryImg, QueryFile } from "../protocol";
import { ImeSubmitGuard } from "../ime-submit";

interface Props {
  cwd: string;
  engine?: "claude" | "codex";  // which backend this new chat will use
  createError?: string | null;
  onPickCwd: () => void;  // open the directory picker
  onSend: (prompt: string, images?: QueryImg[], files?: QueryFile[],
           collaborationMode?: CollaborationModeName,
           permissionMode?: CodexPermissionMode,
           serviceTier?: CodexServiceTier) => boolean;
}

export function NewChatView({ cwd, engine = "claude", createError, onPickCwd, onSend }: Props) {
  const [text, setText] = useState("");
  const [images, setImages] = useState<QueryImg[]>([]);
  const [files, setFiles] = useState<QueryFile[]>([]);
  const [importing, setImporting] = useState(false);
  const [creating, setCreating] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const imeSubmitRef = useRef(new ImeSubmitGuard());
  const buttonSendTimerRef = useRef<number | null>(null);

  useEffect(() => {
    if (createError) setCreating(false);
  }, [createError]);

  useEffect(() => () => {
    if (buttonSendTimerRef.current !== null) {
      window.clearTimeout(buttonSendTimerRef.current);
    }
  }, []);

  const hasAttachments = images.length > 0 || files.length > 0;
  const canSend = (text.trim().length > 0 || hasAttachments) && !creating && !importing;

  const onPick = async (fl: FileList | File[] | null) => {
    if (importing) return;
    setImporting(true);
    try {
      const batch = await pickFiles(
        fl, images.length + files.length, attachmentBytes(images, files));
      if (batch.images.length) setImages((previous) => [...previous, ...batch.images]);
      if (batch.files.length) setFiles((previous) => [...previous, ...batch.files]);
      if (batch.errors.length) window.alert(batch.errors.join("；"));
    } finally {
      setImporting(false);
    }
  };

  const onPaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    const fs: File[] = [];
    for (let i = 0; i < items.length; i++) {
      const it = items[i];
      if (it.kind === "file") { const f = it.getAsFile(); if (f) fs.push(f); }
    }
    if (fs.length) { e.preventDefault(); void onPick(fs); }
  };

  const send = (value = taRef.current?.value ?? text) => {
    const prompt = value.trim();
    if ((!prompt && !hasAttachments) || creating || importing) return;
    setCreating(true);
    const queued = onSend(
      prompt, images.length ? images : undefined, files.length ? files : undefined,
      engine === "codex" ? "default" : undefined,
      engine === "codex" ? "never" : undefined,
      engine === "codex" ? "default" : undefined);
    if (!queued) setCreating(false);
  };

  const requestButtonSend = () => {
    if (buttonSendTimerRef.current !== null) return;
    buttonSendTimerRef.current = window.setTimeout(() => {
      buttonSendTimerRef.current = null;
      send();
    }, 0);
  };

  return (
    <div className="newchat">
      <div className="newchat-card">
        <div className="newchat-greet">{engine === "codex" ? "开始 Codex 新对话" : "开始新对话"}
          <span className={`newchat-engine ${engine}`}>{engine === "codex" ? "◇ Codex" : "✳ Claude"}</span>
        </div>
        <button className="newchat-cwd" onClick={onPickCwd} title="更改工作目录" disabled={creating}>
          <Icon name="folder" size={16} />
          <span className="newchat-cwd-path">{cwd === "~" ? "~ · 主目录" : (cwd || "未指定目录")}</span>
          <Icon name="edit" size={13} />
        </button>

        {hasAttachments && (
          <div className="attach show newchat-attach">
            {images.map((img, i) => (
              <span key={i} className="attach-img">
                <img src={`data:${img.media_type};base64,${img.data}`} alt="" />
                <button className="attach-x" onClick={() => setImages(images.filter((_, j) => j !== i))} aria-label="移除"><Icon name="close" size={12} /></button>
              </span>
            ))}
            {files.map((f, i) => (
              <span key={i} className="attach-file">
                <Icon name="read" size={14} />
                <span className="attach-fn">{f.filename}</span>
                <button className="attach-x" onClick={() => setFiles(files.filter((_, j) => j !== i))} aria-label="移除"><Icon name="close" size={12} /></button>
              </span>
            ))}
          </div>
        )}

        <textarea className="newchat-input" placeholder="发条消息开始…" ref={taRef}
          value={text} onChange={(e) => setText(e.target.value)} onPaste={onPaste} autoFocus rows={3}
          disabled={creating || importing}
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
          }} />

        <div className="newchat-foot">
          <div className="newchat-ctls">
            <button className="cmdbtn" onClick={() => fileRef.current?.click()} aria-label="添加图片或文件" title="添加图片或文件" disabled={creating || importing}>
              <Icon name="plus" size={18} />
            </button>
            <input ref={fileRef} type="file" multiple hidden onChange={(e) => { void onPick(e.target.files); e.target.value = ""; }} />
          </div>
          <div className="newchat-foot-right">
            <span className="newchat-hint">{createError
              ? `创建失败：${createError}`
              : importing ? "正在导入附件…" : creating ? "正在创建会话…" : "Enter 发送"}</span>
            <button className="newchat-send"
              onPointerDown={() => {
                if (imeSubmitRef.current.shouldCommitBeforeButtonSubmit()) taRef.current?.blur();
              }}
              onClick={requestButtonSend}
              disabled={!canSend}>
              <Icon name="send" size={16} />开始
            </button>
          </div>
        </div>
      </div>

    </div>
  );
}
