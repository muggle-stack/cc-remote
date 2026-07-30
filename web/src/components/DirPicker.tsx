// Directory picker for selecting an arbitrary cwd. Lists the
// wrapper host's filesystem via the `list_dir` protocol (browsers can't read
// it directly): breadcrumb + one-level-down navigation + a manual path input.
import { useEffect, useState } from "react";
import type { DirEntry } from "../protocol";
import { Icon } from "../icons";
import { useImeSubmit } from "../use-ime-submit";

interface Props {
  open: boolean;
  path: string | null;
  parent: string | null;
  dirs: DirEntry[];
  responseRequestId?: string | null;
  onBrowse: (path: string | null) => string | null;
  onConfirm: (cwd: string) => void;
  onClose: () => void;
  initialPath?: string | null;
  title?: string;
  confirmLabel?: string;
  busy?: boolean;
  error?: string | null;
  waitForInitialBrowse?: boolean;
}

interface PendingBrowse {
  path: string | null;
  requestId: string | null;
}

export function DirPicker({
  open,
  path,
  parent,
  dirs,
  responseRequestId = null,
  onBrowse,
  onConfirm,
  onClose,
  initialPath = null,
  title = "选择本地工作区",
  confirmLabel = "在此创建",
  busy = false,
  error = null,
  waitForInitialBrowse = false,
}: Props) {
  const [manual, setManual] = useState("");
  const [pendingBrowse, setPendingBrowse] = useState<PendingBrowse | null>(
    () => open && waitForInitialBrowse
      ? { path: initialPath, requestId: null }
      : null,
  );
  const imeSubmit = useImeSubmit<HTMLInputElement>((value) => {
    const cwd = value.trim() || (pendingBrowse === null ? path : null);
    if (cwd) onConfirm(cwd);
  });
  // Each open starts from its caller-owned path so stale state from a previous
  // pick doesn't leak in. `open` is the only dependency on purpose.
  useEffect(() => {
    if (open) {
      setManual("");
      const requestId = onBrowse(initialPath);
      setPendingBrowse(
        waitForInitialBrowse ? { path: initialPath, requestId } : null,
      );
    } else {
      setManual("");
      setPendingBrowse(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => {
    if (!open || pendingBrowse === null
        || pendingBrowse.requestId === null) return;
    if (responseRequestId === pendingBrowse.requestId) {
      setPendingBrowse(null);
    }
  }, [open, pendingBrowse, responseRequestId]);

  if (!open) return null;

  const browse = (p: string) => {
    setManual("");
    setPendingBrowse({ path: p, requestId: onBrowse(p) });
  };
  const listingPending = pendingBrowse !== null;
  const visiblePath = listingPending ? pendingBrowse.path : path;
  const visibleParent = listingPending ? null : parent;
  const visibleDirs = listingPending ? [] : dirs;
  return (
    <div className="dp-overlay" onClick={() => { if (!busy) onClose(); }}>
      <div className="dp" onClick={(e) => e.stopPropagation()} role="dialog" aria-label={title}>
        <div className="dp-head">
          <span className="dp-title">{title}</span>
          <button className="iconbtn" onClick={onClose} disabled={busy}
            aria-label="关闭"><Icon name="close" /></button>
        </div>
        <div className="dp-crumbs" title={visiblePath || ""}>
          {visiblePath || "…"}
        </div>
        <div className="dp-list">
          {visibleParent && (
            <button className="dp-row up" onClick={() => browse(visibleParent)} disabled={busy}>
              <Icon name="back" size={14} /><span>返回上级</span>
            </button>
          )}
          {listingPending
            ? <div className="dp-empty">正在读取目录…</div>
            : visibleDirs.length === 0
              && <div className="dp-empty">无可见子目录</div>}
          {visibleDirs.map((d) => (
            <button key={d.path} className="dp-row" onClick={() => browse(d.path)}
              title={d.path} disabled={busy}>
              <Icon name="folder" size={16} /><span className="dp-name">{d.name}</span>
              <Icon name="chev" size={13} />
            </button>
          ))}
        </div>
        <div className="dp-foot">
          {error && <div className="dp-error" role="alert">{error}</div>}
          <input ref={imeSubmit.inputRef} className="dp-input" placeholder="或粘贴绝对路径…"
            value={manual} onChange={(e) => setManual(e.target.value)}
            disabled={busy}
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
            onClick={imeSubmit.requestSubmit}
            disabled={
              busy || !(manual.trim() || (!listingPending && path))
            }>
            {busy
              ? <><span className="dp-spinner" />迁移中…</>
              : <><Icon name="plus" size={15} />{confirmLabel}</>}
          </button>
        </div>
      </div>
    </div>
  );
}
