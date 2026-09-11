import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { DshApi } from "../dsh-api";
import type { DshItem, DshState } from "../protocol";
import { Icon } from "../icons";
import { MessageBlock } from "./MessageBlock";
import "./DshFeatures.css";

export type DshPanelSelection = { kind: "subagents" | "jobs" | "deliverables" | "diagnostics" | "conversation"; target?: string; query?: string };
const labels = { subagents: "子代理", jobs: "后台任务", deliverables: "产出文件", diagnostics: "连接诊断", conversation: "会话记录" };
const states: Record<string, string> = { running: "运行中", inactive: "未运行", stopping: "停止中", completed: "已结束", killed: "已停止", failed: "失败", active: "正常", unknown: "未知", unavailable: "暂不可用", corrupt: "记录异常" };

export default function DshPanel({ sid, selection, api, state, onClose, onOpenFile, onOpenSession }: {
  sid: string; selection: DshPanelSelection; api: DshApi; state?: DshState | null;
  onClose: () => void; onOpenFile: (path: string, sid?: string, line?: number) => void; onOpenSession: (sid: string) => void;
}) {
  const [tab, setTab] = useState(selection.kind);
  const [parent, setParent] = useState(sid);
  const [trail, setTrail] = useState<string[]>([]);
  const [child, setChild] = useState<DshItem | null>(selection.target ? { id: selection.target, sid: selection.target, title: "会话记录", detail: "", state: "", has_children: false } : null);
  const [items, setItems] = useState<DshItem[]>([]);
  const [next, setNext] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [revision, setRevision] = useState(0);
  const [prompt, setPrompt] = useState("");
  const [actionPending, setActionPending] = useState(false);
  const [notice, setNotice] = useState("");
  const [downloadProgress, setDownloadProgress] = useState<number | null>(null);
  const [downloadUrl, setDownloadUrl] = useState<string | null>(null);
  const downloadAbort = useRef<AbortController | null>(null);
  const readAbort = useRef<AbortController | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const currentTarget = useRef(child?.sid ?? sid);
  currentTarget.current = child?.sid ?? sid;
  useEffect(() => {
    setItems([]); setNotice(""); setActionPending(false);
    downloadAbort.current?.abort(); setDownloadUrl(null); setDownloadProgress(null);
  }, [child, parent, tab]);
  useEffect(() => {
    if (tab !== "subagents" || child && child.state !== "running") return;
    const timer = setInterval(() => setRevision(value => value + 1), 5000);
    return () => clearInterval(timer);
  }, [tab, child]);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    root.current?.querySelector<HTMLButtonElement>("button")?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); closeRef.current(); }
      if (event.key === "Tab") {
        const nodes = [...(root.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input,textarea,a[href]') ?? [])];
        const first = nodes[0], last = nodes.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    };
    window.addEventListener("keydown", keydown);
    return () => { window.removeEventListener("keydown", keydown); previous?.focus(); downloadAbort.current?.abort(); readAbort.current?.abort(); };
  }, []);
  useEffect(() => () => { if (downloadUrl) URL.revokeObjectURL(downloadUrl); }, [downloadUrl]);

  useEffect(() => {
    const abort = new AbortController(); readAbort.current = abort;
    setNext(null); setError(null);
    if (tab === "jobs") { setLoading(false); return; }
    setLoading(true);
    api.read(sid, child ? "conversation" : tab === "conversation" ? "conversation" : tab,
      { target_sid: child?.sid ?? (parent !== sid ? parent : undefined), query: selection.query, signal: abort.signal })
      .then(result => {
        if (abort.signal.aborted) return;
        setItems(result.items); setNext(result.next_seq ?? null); setError(result.error ?? null);
      }).catch(err => { if (!abort.signal.aborted) setError(String(err.message)); })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [api, sid, tab, parent, child, selection.query, revision]);

  const act = async (action: "queue" | "steer" | "stop") => {
    if (!child?.sid) return;
    const target = child.sid;
    setActionPending(true); setNotice("");
    try {
      const result = await api.act(parent, child.sid, action, prompt);
      if (currentTarget.current !== target) return;
      setNotice(result.text);
      if (result.status === "success") { setPrompt(""); setRevision(x => x + 1); }
    } catch (err) { if (currentTarget.current === target) setNotice((err as Error).message); }
    finally { if (currentTarget.current === target) setActionPending(false); }
  };
  const loadOlder = async () => {
    if (next === null) return;
    setLoading(true); setError(null);
    const abort = new AbortController(); readAbort.current?.abort(); readAbort.current = abort;
    try {
      const result = await api.read(sid, "conversation", { target_sid: child?.sid ?? undefined, before_seq: next, signal: abort.signal });
      if (abort.signal.aborted) return;
      if (result.error) setError(result.error);
      else { setItems(previous => [...result.items, ...previous]); setNext(result.next_seq ?? null); }
    } catch (err) { if (!abort.signal.aborted) setError((err as Error).message); }
    finally { if (!abort.signal.aborted) setLoading(false); }
  };
  const exportSession = async () => {
    const abort = new AbortController(); downloadAbort.current = abort;
    setDownloadProgress(0); setError(null); setDownloadUrl(null);
    try {
      const blob = await api.download(child?.sid ?? sid, setDownloadProgress, abort.signal);
      if (!abort.signal.aborted) setDownloadUrl(URL.createObjectURL(blob));
    } catch (err) { if (!abort.signal.aborted) setError((err as Error).message); }
    finally { if (downloadAbort.current === abort) setDownloadProgress(null); }
  };
  return createPortal(<div className="dsh-panel-backdrop" onClick={onClose}>
    <div ref={root} className="dsh-panel" role="dialog" aria-modal="true" aria-label="DSH 会话工具" onClick={e => e.stopPropagation()}>
      <header><strong>{child ? child.title : labels[tab]}</strong><button className="iconbtn" aria-label="关闭会话工具" onClick={onClose}><Icon name="close" /></button></header>
      <nav aria-label="会话工具">
        {(["subagents", "jobs", "deliverables", "diagnostics"] as const).map(kind => <button key={kind}
          aria-current={tab === kind && !child ? "page" : undefined} onClick={() => { setTab(kind); setChild(null); setParent(sid); setTrail([]); setNotice(""); }}>{labels[kind]}</button>)}
      </nav>
      <div className="dsh-panel-actions">
        {(child || trail.length > 0) && <button onClick={() => {
          if (child) { setChild(null); if (tab === "conversation") setTab("subagents"); }
          else { setParent(trail.at(-1)!); setTrail(x => x.slice(0, -1)); }
          setPrompt(""); setNotice("");
        }}>‹ 返回</button>}
        <button disabled={loading} onClick={() => setRevision(x => x + 1)}>刷新</button>
        {selection.target && child?.sid && !child.mode && <button onClick={() => onOpenSession(child.sid!)}>在主聊天查看</button>}
        {downloadProgress === null ? <button onClick={exportSession}>导出完整会话</button>
          : <button onClick={() => downloadAbort.current?.abort()}>取消导出 · {Math.round(downloadProgress * 100)}%</button>}
        {downloadUrl && <a href={downloadUrl} download={`dsh-session-${(child?.sid ?? sid).replace(/^dsh@/, "")}.zip`}>保存 ZIP</a>}
      </div>
      <div className="dsh-panel-body" aria-busy={loading}>
        {error && <p role="alert" className="dsh-feature-error">{error}<button onClick={() => setRevision(x => x + 1)}>重试</button></p>}
        {next !== null && <button disabled={loading} onClick={loadOlder}>加载更早的消息</button>}
        {tab === "jobs" && !child ? <>
          {!state?.connected && <p>连接中断，任务状态尚未更新。</p>}
          {(state?.jobs ?? []).map(job => <article key={job.id} className="dsh-feature-row"><strong>{job.label}</strong><small>{states[job.status]}</small><p>{job.detail}</p></article>)}
          {!state?.jobs?.length && <p className="dsh-feature-empty">当前没有后台任务</p>}
        </> : items.map(item => child || tab === "conversation"
          ? <article key={item.id} className="dsh-reader-message"><small>{item.title}</small><MessageBlock text={item.detail} done onOpenFile={(path, line) => onOpenFile(path, child?.sid ?? sid, line)} /></article>
          : <article key={item.id} className="dsh-feature-row">
              {item.path ? <button className="dsh-feature-link" onClick={() => onOpenFile(item.path!)}>{item.title}</button>
                : tab === "subagents" ? <button className="dsh-feature-link" disabled={!item.mode} onClick={() => { setChild(item); setPrompt(""); setNotice(""); }}>{item.title}</button>
                : <strong>{item.title}</strong>}
              {item.state && <small data-state={item.state}>{states[item.state] ?? item.state}</small>}
              {item.detail && <p>{item.detail}</p>}
              {item.has_children && item.sid && <button onClick={() => { setTrail(x => [...x, parent]); setParent(item.sid!); }}>查看下级子代理 ›</button>}
            </article>)}
        {loading && <p role="status">读取中…</p>}
        {!loading && !error && !items.length && tab !== "jobs" && <p className="dsh-feature-empty">{tab === "deliverables" ? "暂无已记录的产出文件" : child ? "暂无可显示的消息" : "暂无记录"}</p>}
      </div>
      {child?.mode === "continuable" && child.controllable && <form className="dsh-child-composer" onSubmit={e => { e.preventDefault(); void act("queue"); }}>
        <textarea value={prompt} onChange={e => setPrompt(e.target.value)} placeholder="发送给这个子代理…" aria-label="子代理消息" />
        <div><button disabled={actionPending || !prompt.trim()} type="submit">排队发送</button><button type="button" disabled={actionPending || !prompt.trim()} onClick={() => act("steer")}>引导</button>
          <button type="button" disabled={actionPending} onClick={() => act("stop")}>停止子代理</button></div>
      </form>}
      {notice && <p role="status" className="dsh-action-notice">{notice}</p>}
    </div>
  </div>, document.body);
}
