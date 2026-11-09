import { Fragment, lazy, Suspense, useCallback, useEffect, useRef, useState, type ComponentProps } from "react";
import { Icon } from "../icons";
import { uuid } from "../util";
import type { Engine, FilePreview, Space, SessionArtifactInfo, SessionArtifacts } from "../protocol";
import type { RelayWs } from "../ws";
import type { ArtifactPanel as ArtifactPanelType } from "./ArtifactPanel";
import { PanelExpandButton, PanelFrame } from "./RightPanelPresentation";
import { PanelResizer } from "./PanelResizer";
import { useArtifactThumbnails } from "./artifact-thumbnails";
import "./SessionArtifactsPanel.css";

const ArtifactPanel = lazy(() => import("./ArtifactPanel").then(m => ({ default: m.ArtifactPanel })));

function dateGroup(timestamp: number) {
  const date = new Date(timestamp * 1000);
  const today = new Date();
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  if (date.toDateString() === today.toDateString()) return "今天";
  if (date.toDateString() === yesterday.toDateString()) return "昨天";
  return date.toLocaleDateString("zh-CN", {
    ...(date.getFullYear() !== today.getFullYear() ? { year: "numeric" as const } : {}),
    month: "long", day: "numeric",
  });
}

type PreviewProps = Omit<ComponentProps<typeof ArtifactPanelType>, "active" | "hasBtw" | "onTab" | "artifact" | "displayName"> & {
  artifact: ComponentProps<typeof ArtifactPanelType>["artifact"] | null;
};

export default function SessionArtifactsPanel({ sid, engine, space, ws, revision, onListen,
  selected, onSelect, onBack, onClose, preview }: {
  sid: string; engine: Engine; space: Space; ws: RelayWs | null; revision: string;
  onListen: (listener: ((message: SessionArtifacts | FilePreview) => boolean) | null) => void;
  selected: SessionArtifactInfo | null;
  onSelect: (entry: SessionArtifactInfo) => void; onBack: () => void; onClose: () => void;
  preview: PreviewProps;
}) {
  const [entries, setEntries] = useState<SessionArtifactInfo[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [cursor, setCursor] = useState<number | null>(null);
  const [more, setMore] = useState(false);
  const [history, setHistory] = useState<{ before: string | null; more: boolean }>({ before: null, more: true });
  const pending = useRef<{ id: string; scan: boolean; append: boolean } | null>(null);
  const refreshPending = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const firstScan = useRef(false);
  const paged = useRef(false);
  const lastRequest = useRef<[number | null, boolean, string | null]>([null, false, null]);
  const scrollRef = useRef<HTMLDivElement>(null);
  const hasPreview = !!selected && !!preview.artifact;
  const thumbnails = useArtifactThumbnails(scrollRef, entries, !hasPreview, ws, sid, engine, space);
  const receiveThumbnail = thumbnails.receive;

  const request = useCallback((before: number | null = null, scan = false, historyBefore: string | null = null) => {
    if (pending.current) {
      // Turn completions may arrive while an older page is still loading.
      // Coalesce head refreshes without replacing that page or its cursor.
      if (before === null && !scan) refreshPending.current = true;
      return;
    }
    lastRequest.current = [before, scan, historyBefore];
    const id = uuid();
    setError("");
    if (!ws?.sendGetSessionArtifacts(sid, engine, space, id, before, scan, historyBefore)) {
      setError("连接暂时不可用，请重试"); return;
    }
    pending.current = { id, scan, append: before !== null };
    setBusy(true);
    timer.current = setTimeout(() => {
      pending.current = null; setBusy(false); setError("读取超时，请重试");
    }, 30_000);
  }, [ws, sid, engine, space]);

  useEffect(() => {
    onListen(message => {
      if (message.type === "file_preview") return receiveThumbnail(message);
      const owner = pending.current;
      if (message.sid !== sid || owner?.id !== message.request_id) return false;
      if (timer.current) clearTimeout(timer.current);
      pending.current = null;
      setBusy(false);
      if (message.error) { setError(message.error); return true; }
      setEntries(current => {
        const merged = new Map(current.map(item => [item.id, item]));
        message.entries.forEach(item => merged.set(item.id, item));
        return [...merged.values()].sort((a, b) => b.created_at - a.created_at);
      });
      if (owner.append || !paged.current) {
        setCursor(message.next_before ?? null);
        setMore(message.has_more);
      }
      if (owner.append) paged.current = true;
      if (owner.scan) setHistory({ before: message.history_before ?? null, more: message.history_more });
      // Paint the durable catalog first; then inspect one bounded history page.
      if (!firstScan.current) {
        firstScan.current = true;
        request(null, true);
      }
      return true;
    });
    return () => {
      onListen(null);
      if (timer.current) clearTimeout(timer.current);
      pending.current = null;
      refreshPending.current = false;
    };
  }, [onListen, request, sid, receiveThumbnail]);

  useEffect(() => { request(); }, [request, revision]);
  useEffect(() => {
    if (!busy && !pending.current && refreshPending.current) {
      refreshPending.current = false;
      request();
    }
  }, [busy, request]);
  return <PanelFrame className="session-artifacts-panel" label="Artifacts">
    <PanelResizer ariaLabel="调整 Artifacts 面板宽度" />
    <div className="session-artifacts-list" hidden={hasPreview}>
      <div className="artifact-head">
        <div className="artifact-title"><Icon name="archive" size={18} /><b>Artifacts</b></div>
        <button className="iconbtn" aria-label="刷新 Artifacts" disabled={busy} onClick={() => {
          thumbnails.refresh(); firstScan.current = false; request();
        }}><Icon name="refresh" /></button>
        <PanelExpandButton label="Artifacts" />
        <button className="iconbtn" aria-label="收起 Artifacts" onClick={onClose}><Icon name="chevrons-right" /></button>
      </div>
      <p className="session-artifacts-caption">当前会话的附件与交付文件</p>
      <div className="session-artifacts-scroll" ref={scrollRef}>
        {!entries.length && !busy && !error && <div className="session-artifacts-empty">
          <Icon name="archive" size={32} /><p>还没有文件</p><small>上传的附件和回答中的文件会整理在这里。</small>
        </div>}
        {entries.map((entry, index) => <Fragment key={entry.id}>
          {(index === 0 || dateGroup(entry.created_at) !== dateGroup(entries[index - 1].created_at)) &&
            <h3 className="session-artifacts-date">{dateGroup(entry.created_at)}</h3>}
          <button className="session-artifact-row" data-artifact-image={entry.kind === "image" ? entry.id : undefined}
          title={entry.path} disabled={!entry.available} onClick={() => onSelect(entry)}>
          <span className={`session-artifact-icon ${entry.kind}`}>
            {entry.kind === "image" && thumbnails.sources.get(entry.id)
              ? <img src={thumbnails.sources.get(entry.id)!} alt="" decoding="async" />
              : <Icon name={entry.kind === "image" ? "image" : entry.kind === "file" ? "code" : "read"} size={22} />}
          </span>
          <span className="session-artifact-info"><b>{entry.name}</b><small>
            {entry.source === "upload" ? "上传附件" : "AI 交付"}
            {!entry.available && " · 文件已移动或删除"}
          </small></span><Icon name="chevron-right" size={16} />
        </button></Fragment>)}
        {error && <p className="session-artifacts-error" role="alert">{error}</p>}
        {(busy || more || history.more || error) && <div className="session-artifacts-footer"><button
          className="session-artifacts-more" disabled={busy} onClick={() => {
          if (error) request(...lastRequest.current);
          else request(cursor, !more, history.before);
        }}><Icon name={error ? "refresh" : "clock"} size={14} />
          <span role={busy ? "status" : undefined}>{busy ? "正在整理…" : error ? "重试" : "查看更早的文件"}</span>
          {!error && <Icon name="chev" size={13} />}
        </button></div>}
      </div>
    </div>
    {hasPreview && <>
      <Suspense fallback={<p role="status">加载预览…</p>}>
        <ArtifactPanel key={`${preview.artifact?.sid}:${preview.artifact?.file}:${preview.artifact?.requestId}`}
          {...preview} artifact={preview.artifact!} active="diff" hasBtw={false} onTab={() => {}}
          onBack={onBack} backLabel="返回 Artifacts"
          displayName={selected?.path === preview.artifact?.file ? selected?.name : undefined}
          onRefresh={(path, line) => {
            if (selected?.path === path) onSelect(selected);
            else preview.onRefresh?.(path, line);
          }} />
      </Suspense>
    </>}
  </PanelFrame>;
}
