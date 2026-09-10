import { useCallback, useEffect, useRef, useState } from "react";
import type { FilesListed } from "../protocol";
import type { RelayWs } from "../ws";
import { Icon } from "../icons";
import { PanelResizer } from "./PanelResizer";
import "./FileBrowserPanel.css";

export function FileBrowserPanel({ sid, initialPath, ws, hidden, onListen,
  onOpenFile, onClose }: {
  sid: string; initialPath: string; ws: RelayWs | null; hidden: boolean;
  onListen: (listener: ((message: FilesListed) => void) | null) => void;
  onOpenFile: (path: string) => void; onClose: () => void;
}) {
  const [listing, setListing] = useState<FilesListed | null>(null);
  const [path, setPath] = useState(initialPath);
  const [showHidden, setShowHidden] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pending = useRef<{ id: string; append: boolean } | null>(null);
  const timeout = useRef<ReturnType<typeof setTimeout> | null>(null);
  const openFile = useRef(onOpenFile);
  useEffect(() => { openFile.current = onOpenFile; }, [onOpenFile]);
  const browse = useCallback((target: string, includeHidden: boolean,
    offset = 0, revision?: string | null) => {
    if (timeout.current) clearTimeout(timeout.current);
    const id = crypto.randomUUID();
    pending.current = { id, append: offset > 0 };
    setError(null);
    if (!ws?.sendBrowseFiles(sid, target, id, includeHidden, offset, revision)) {
      pending.current = null;
      setLoading(false);
      setError("连接不可用，请重试");
      return;
    }
    setLoading(true);
    timeout.current = setTimeout(() => {
      pending.current = null;
      setLoading(false);
      setError("读取目录超时，请重试");
    }, 15000);
  }, [ws, sid]);
  useEffect(() => {
    onListen((message) => {
      if (message.sid !== sid || message.request_id !== pending.current?.id) return;
      const append = pending.current.append;
      pending.current = null;
      if (timeout.current) clearTimeout(timeout.current);
      setLoading(false);
      if (message.error) { setError(message.error); return; }
      if (message.kind === "file") { openFile.current(message.path); return; }
      setPath(message.path);
      setListing((previous) => ({ ...message,
        entries: append && previous?.path === message.path
          ? [...previous.entries, ...message.entries] : message.entries }));
    });
    browse(initialPath, false);
    return () => {
      onListen(null);
      if (timeout.current) clearTimeout(timeout.current);
    };
  }, [sid, onListen, browse, initialPath]);

  return <div className="workspace-files" hidden={hidden} data-lock-horizontal-swipe="true">
    <PanelResizer ariaLabel="调整文件面板宽度" />
    <div className="artifact-head">
      <Icon name="folder-open" size={18} /><strong>会话文件</strong>
      <span className="artifact-path" title={listing?.root}>{listing?.root}</span>
      <button className="iconbtn" title="刷新目录" aria-label="刷新目录"
        onClick={() => browse(listing?.path ?? initialPath, showHidden)}><Icon name="refresh" size={17} /></button>
      <button className="iconbtn" title="关闭文件浏览" aria-label="关闭文件浏览" onClick={onClose}><Icon name="close" size={18} /></button>
    </div>
    <form className="workspace-path" onSubmit={(event) => {
      event.preventDefault(); browse(path, showHidden);
    }}>
      <input aria-label="文件或目录路径" value={path} onChange={(event) => setPath(event.target.value)}
        spellCheck={false} placeholder="输入文件或目录路径" />
      <button type="submit" disabled={loading}>打开</button>
    </form>
    <div className="workspace-options">
      <button disabled={!listing?.parent || loading} onClick={() => browse(listing!.parent!, showHidden)}>
        <Icon name="chevron-left" size={15} />上级目录
      </button>
      <label><input type="checkbox" checked={showHidden} onChange={(event) => {
        setShowHidden(event.target.checked);
        browse(listing?.path ?? initialPath, event.target.checked);
      }} />隐藏文件</label>
    </div>
    {error && <p className="workspace-error" role="alert">{error}</p>}
    <div className="workspace-entries" aria-busy={loading}>
      {listing?.entries.map((entry) => <button key={entry.path} className="workspace-entry"
        disabled={loading || entry.kind === "unsupported"}
        title={entry.kind === "unsupported" ? "符号链接或特殊文件不支持预览" : entry.name}
        onClick={() => entry.kind === "directory" ? browse(entry.path, showHidden) : onOpenFile(entry.path)}>
        <Icon name={entry.kind === "directory" ? "folder" : "read"} size={18} />
        <span>{entry.name}</span>{entry.kind === "directory" && <Icon name="chevron-right" size={15} />}
      </button>)}
      {!loading && !error && listing && !listing.entries.length && <p className="workspace-empty">此目录为空</p>}
      {loading && <p className="workspace-empty" role="status">读取中…</p>}
      {listing?.next_offset != null && <button className="workspace-more" disabled={loading}
        onClick={() => browse(listing.path, showHidden, listing.next_offset!, listing.revision)}>加载更多</button>}
    </div>
  </div>;
}
