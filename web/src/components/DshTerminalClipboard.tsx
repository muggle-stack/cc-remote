import { useEffect, useRef, useState, type RefObject } from "react";
import type { Terminal } from "@xterm/xterm";
import { terminalSelectedText } from "../dsh-terminal-selection";
import { Icon } from "../icons";
import { CenteredSheet } from "./CenteredSheet";

export interface TerminalClipboardTarget {
  terminal: Terminal;
  paste(text: string): boolean;
}

function bufferText(terminal: Terminal): string {
  const buffer = terminal.buffer.active;
  const lines: string[] = [];
  for (let index = 0; index < buffer.length; index++) {
    const line = buffer.getLine(index);
    if (!line) continue;
    const text = line.translateToString(!buffer.getLine(index + 1)?.isWrapped);
    if (line.isWrapped && lines.length) lines[lines.length - 1] += text;
    else lines.push(text);
  }
  while (lines.at(-1) === "") lines.pop();
  return lines.join("\n");
}

export function DshTerminalClipboard({ targetRef, connected }: {
  targetRef: RefObject<TerminalClipboardTarget | null>;
  connected: boolean;
}) {
  const [dialog, setDialog] = useState<{ kind: "copy" | "paste"; target: TerminalClipboardTarget } | null>(null);
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const [reading, setReading] = useState(false);
  const field = useRef<HTMLTextAreaElement>(null);
  const mounted = useRef(false);
  const pending = useRef(false);
  const selection = useRef("");
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  const current = (target: TerminalClipboardTarget) => mounted.current && targetRef.current === target;
  const openCopy = async () => {
    const target = targetRef.current;
    if (!target) return;
    const value = selection.current || terminalSelectedText(target.terminal);
    selection.current = "";
    if (value && navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(value);
        return;
      } catch { /* Keep native selection and the manual copy surface available. */ }
    }
    if (!current(target)) return;
    setText(value || bufferText(target.terminal));
    setError(""); setDialog({ kind: "copy", target });
  };
  const copy = async () => {
    if (!dialog || !current(dialog.target) || !field.current) return;
    const { selectionStart, selectionEnd } = field.current;
    const value = selectionEnd > selectionStart ? text.slice(selectionStart, selectionEnd) : text;
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(value);
    } catch {
      if (!current(dialog.target) || !field.current) return;
      field.current.focus();
      field.current.setSelectionRange(selectionEnd > selectionStart ? selectionStart : 0,
        selectionEnd > selectionStart ? selectionEnd : text.length);
      // Native copy remains available when the async clipboard API is blocked.
      try {
        if (!document.execCommand("copy")) throw new Error("Copy unavailable");
      } catch {
        setError("请长按选中的文字，使用系统菜单复制。");
        return;
      }
    }
    if (current(dialog.target)) setDialog(null);
  };
  const paste = async () => {
    const target = targetRef.current;
    if (!target || !connected || pending.current) return;
    pending.current = true; setReading(true);
    try {
      if (!navigator.clipboard?.readText) throw new Error("Clipboard unavailable");
      const value = await navigator.clipboard.readText();
      if (!current(target)) return;
      if (value && target.paste(value)) return;
      setText(""); setError(""); setDialog({ kind: "paste", target });
    } catch {
      if (current(target)) {
        setText(""); setError(""); setDialog({ kind: "paste", target });
      }
    } finally {
      pending.current = false;
      if (mounted.current) setReading(false);
    }
  };
  const visible = dialog && dialog.target === targetRef.current ? dialog : null;
  return <>
    <button aria-label="复制终端文字" disabled={!targetRef.current}
      onPointerDown={() => { selection.current = targetRef.current ? terminalSelectedText(targetRef.current.terminal) : ""; }}
      onClick={() => void openCopy()}><Icon name="copy" size={15} />复制</button>
    <button aria-label="粘贴到终端" disabled={!connected || reading} onClick={() => void paste()}>粘贴</button>
    {!!visible && <CenteredSheet open label={visible.kind === "copy" ? "复制终端文字" : "粘贴到终端"}
      maxWidth={640} maxHeight={520} minimumHeight={280} className="dsh-terminal-clipboard" onClose={() => setDialog(null)}>
      <p>{visible.kind === "copy" ? "长按选择要复制的文字。" : "在下面长按粘贴，再写入终端。"}</p>
      <textarea ref={field} aria-label={visible.kind === "copy" ? "终端文字" : "粘贴内容"} value={text}
        readOnly={visible.kind === "copy"} spellCheck={false} autoCapitalize="off" autoCorrect="off"
        onChange={event => setText(event.target.value)} />
      {error && <p role="alert">{error}</p>}
      <div className="dsh-terminal-clipboard-actions">
        {visible.kind === "copy" ? <>
          <button onClick={() => { field.current?.focus(); field.current?.select(); }}>全选</button>
          <button disabled={!text} onClick={() => void copy()}>复制</button>
        </> : <button disabled={!text || !connected} onClick={() => {
          if (current(visible.target) && visible.target.paste(text)) setDialog(null);
          else setError("终端未连接，请重新连接后粘贴。");
        }}>写入终端</button>}
      </div>
    </CenteredSheet>}
  </>;
}
