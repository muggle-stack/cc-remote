// Directory picker for creating a session in an arbitrary cwd. Lists the
// wrapper host's filesystem via the `list_dir` protocol (browsers can't read
// it directly): breadcrumb + one-level-down navigation + a manual path input
// fallback. Confirming sends new_session(cwd).
import { useEffect, useState } from "react";
import type { DirEntry } from "../protocol";
import { Icon } from "../icons";
import { useImeSubmit } from "../use-ime-submit";

interface Props {
  open: boolean;
  path: string | null;
  parent: string | null;
  dirs: DirEntry[];
  onBrowse: (path: string | null) => void;
  onConfirm: (cwd: string) => void;
  onClose: () => void;
}

export function DirPicker({ open, path, parent, dirs, onBrowse, onConfirm, onClose }: Props) {
  const [manual, setManual] = useState("");
  const imeSubmit = useImeSubmit<HTMLInputElement>((value) => {
    const cwd = value.trim() || path;
    if (cwd) onConfirm(cwd);
  });
  // Each open starts fresh from $HOME so stale state from a previous pick
  // doesn't leak in. `open` is the only dependency on purpose.
  useEffect(() => {
    if (open) onBrowse(null);
    else setManual("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  if (!open) return null;

  const browse = (p: string) => { setManual(""); onBrowse(p); };
  return (
    <div className="dp-overlay" onClick={onClose}>
      <div className="dp" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="选择本地工作区">
        <div className="dp-head">
          <span className="dp-title">选择本地工作区</span>
          <button className="iconbtn" onClick={onClose} aria-label="关闭"><Icon name="close" /></button>
        </div>
        <div className="dp-crumbs" title={path || ""}>{path || "…"}</div>
        <div className="dp-list">
          {parent && (
            <button className="dp-row up" onClick={() => browse(parent)}>
              <Icon name="back" size={14} /><span>返回上级</span>
            </button>
          )}
          {dirs.length === 0 && <div className="dp-empty">无可见子目录</div>}
          {dirs.map((d) => (
            <button key={d.path} className="dp-row" onClick={() => browse(d.path)} title={d.path}>
              <Icon name="folder" size={16} /><span className="dp-name">{d.name}</span>
              <Icon name="chev" size={13} />
            </button>
          ))}
        </div>
        <div className="dp-foot">
          <input ref={imeSubmit.inputRef} className="dp-input" placeholder="或粘贴绝对路径…"
            value={manual} onChange={(e) => setManual(e.target.value)}
            onCompositionStart={imeSubmit.startComposition}
            onCompositionEnd={(e) => {
              imeSubmit.endComposition();
              setManual(e.currentTarget.value);
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
          <button className="dp-confirm"
            onPointerDown={imeSubmit.commitCompositionBeforePointerSubmit}
            onClick={imeSubmit.requestSubmit} disabled={!(manual.trim() || path)}>
            <Icon name="plus" size={15} />在此创建
          </button>
        </div>
      </div>
    </div>
  );
}
