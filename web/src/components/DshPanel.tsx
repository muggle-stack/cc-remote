import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { PanelFrame, PanelExpandButton } from "./RightPanelPresentation";
import { PanelResizer } from "./PanelResizer";
import { DshManagement } from "./DshManagement";
import { DshSettings } from "./DshSettings";
import { DshStats } from "./DshStats";
import { DshToolsMenu } from "./DshToolsMenu";
import { dshToolLabels as labels } from "../dsh-tool-labels";
import type { DshApiClient } from "../dsh-api";
import type { DshItem, DshState } from "../protocol";
import { EngineIcon, Icon } from "../icons";
import { MessageBlock } from "./MessageBlock";
import { mergeDshPages } from "../dsh-management-pages";
import "./DshFeatures.css";
import "./DshTools.css";

const DshTerminal = lazy(() => import("./DshTerminal"));

export type DshPanelSelection = { kind: "subagents" | "jobs" | "deliverables" | "diagnostics" | "conversation" | "plugins" | "workflows" | "trajectory" | "stats" | "schedules" | "settings" | "presets" | "bundles" | "terminals"; target?: string; query?: string };
const states: Record<string, string> = { error: "运行异常", enabled: "已启用", disabled: "已停用", running: "运行中", inactive: "未运行", stopping: "停止中", completed: "已结束", killed: "已停止", failed: "失败", active: "正常", unknown: "未知", unavailable: "暂不可用", corrupt: "记录异常" };

export default function DshPanel({ sid, selection, api, state, onClose, onOpenFile, onOpenSession, onOpenAgent, onCompose }: {
  onOpenAgent: (run: string, title?: string) => void; onCompose: (prompt: string, preset?: string) => void;
  sid: string; selection: DshPanelSelection; api: DshApiClient; state?: DshState | null;
  onClose: () => void; onOpenFile: (path: string, sid?: string, line?: number) => void; onOpenSession: (sid: string) => void;
}) {
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobOutput, setJobOutput] = useState("");
  const [jobLossy, setJobLossy] = useState(false);
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
  useEffect(() => () => { downloadAbort.current?.abort(); readAbort.current?.abort(); }, []);
  useEffect(() => () => { if (downloadUrl) URL.revokeObjectURL(downloadUrl); }, [downloadUrl]);

  useEffect(() => {
    const abort = new AbortController(); readAbort.current = abort;
    setNext(null); setError(null);
    if (tab === "stats" || tab === "jobs" || tab === "settings" || tab === "presets" || tab === "bundles" || tab === "schedules" || tab === "terminals") { setLoading(false); return; }
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

  useEffect(() => {
    if (tab !== "jobs" || !jobId) return;
    const abort = new AbortController();
    let offset: number | undefined, timer: ReturnType<typeof setTimeout>;
    setJobOutput(""); setJobLossy(false);
    const read = async () => {
      try {
        const result = await api.read(sid, "job_output", { job_id: jobId, before_seq: offset, signal: abort.signal });
        if (abort.signal.aborted) return;
        if (result.error) { setError(result.error); return; }
        offset = result.next_seq ?? undefined;
        setJobLossy(previous => previous || result.items.some(item => item.state === "lossy"));
        setJobOutput(previous => (previous + result.items.map(item => item.detail).join("")).slice(-60000));
        timer = setTimeout(read, 2000);
      } catch (error) { if (!abort.signal.aborted) setError((error as Error).message); }
    };
    void read();
    return () => { abort.abort(); clearTimeout(timer); };
  }, [api, sid, tab, jobId, revision]);
  const control = async (kind: "job_stop" | "plugin_enable" | "plugin_disable", value: string) => {
    setActionPending(true); setNotice("");
    try { const result = await api.control(sid, kind, value); setNotice(result.text); setRevision(x => x + 1); }
    catch (error) { setNotice((error as Error).message); }
    finally { setActionPending(false); }
  };
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
      const result = await api.read(sid, tab === "workflows" || tab === "trajectory" ? tab : "conversation", { target_sid: child?.sid ?? undefined, before_seq: next, signal: abort.signal });
      if (abort.signal.aborted) return;
      if (result.error) setError(result.error);
      else { setItems(previous => mergeDshPages(result.items, previous, tab)); setNext(result.next_seq ?? null); }
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
  return <PanelFrame className="dsh-panel" role="complementary" label="DSH 会话工具">
      <PanelResizer ariaLabel="调整 DSH 面板宽度" />
      <header className="dsh-panel-header"><EngineIcon engine="dsh" size={23} /><strong>{child ? child.title : labels[tab]}</strong>
        <DshToolsMenu selected={tab} onSelect={kind => { setTab(kind); setChild(null); setParent(sid); setTrail([]); setNotice(""); }} />
        <PanelExpandButton label="DSH 会话工具" /><button className="iconbtn" aria-label="关闭会话工具" onClick={onClose}><Icon name="close" /></button>
      </header>
      {tab === "stats" ? <DshStats key={sid} sid={sid} api={api} onTrajectory={() => setTab("trajectory")} /> : tab === "terminals" ? <Suspense fallback={<p role="status">加载终端…</p>}><DshTerminal sid={sid} api={api} /></Suspense> : tab === "settings" ? <DshSettings sid={sid} api={api} /> : ["presets", "bundles", "schedules"].includes(tab) ? <DshManagement key={`${tab}:${selection.query ?? ""}`} initialSearch={selection.query} sid={sid} kind={tab as "presets" | "bundles" | "schedules"} api={api} onCompose={onCompose} /> : <>
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
        {next !== null && <button disabled={loading} onClick={loadOlder}>加载更早记录</button>}
        {tab === "jobs" && !child ? <>
          {!state?.connected && <p>连接中断，任务状态尚未更新。</p>}
          {(state?.jobs ?? []).map(job => <article key={job.id} className="dsh-feature-row"><strong>{job.label}</strong><small>{states[job.status]}</small><p>{job.detail}</p><button onClick={() => setJobId(jobId === job.id ? null : job.id)}>{jobId === job.id ? "收起输出" : "查看输出"}</button>
            {["running", "stopping"].includes(job.status) && <button disabled={actionPending || job.status === "stopping"} onClick={() => control("job_stop", job.id)}>停止任务</button>}
            {jobId === job.id && <>{jobLossy && <p>较早输出已被原生运行时清理。</p>}<pre className="dsh-job-output">{jobOutput || "暂无输出"}</pre></>}</article>)}
          {!state?.jobs?.length && <p className="dsh-feature-empty">当前没有后台任务</p>}
        </> : items.map(item => child || tab === "conversation"
          ? <article key={item.id} className="dsh-reader-message"><small>{item.title}</small><MessageBlock text={item.detail} done onOpenFile={(path, line) => onOpenFile(path, child?.sid ?? sid, line)} /></article>
          : <article key={item.id} className="dsh-feature-row">
              {item.path ? <button className="dsh-feature-link" onClick={() => onOpenFile(item.path!)}>{item.title}</button>
                : tab === "subagents" ? <button className="dsh-feature-link" disabled={!item.mode} onClick={() => item.sid && onOpenAgent(item.sid, item.title)}>{item.title}</button>
                : <strong>{item.title}</strong>}
              {item.state && <small data-state={item.state}>{states[item.state] ?? item.state}</small>}
              {item.detail && <p>{item.detail}</p>}
              {tab === "workflows" && Array.isArray(item.data?.members) && <div className="dsh-workflow-members">{(item.data.members as { sid?: string; label: string; phase?: string; state: string }[]).filter(member => member.sid).map(member => <button key={member.sid} onClick={() => onOpenAgent(member.sid!, member.label)}><span>{member.phase && <small>{member.phase} · </small>}{member.label}</span><small>{states[member.state] ?? member.state} ›</small></button>)}</div>}
              {tab === "trajectory" && Array.isArray(item.data?.events) && <details className="dsh-trajectory"><summary>{String(item.data.events.length)} 项 · 输入 {Number(item.data.input).toLocaleString()} · 输出 {Number(item.data.output).toLocaleString()}</summary><ol>{(item.data.events as { type: string; label: string; time?: number }[]).map((event, index) => <li key={index}><span>{event.label}</span><small>{event.type}</small></li>)}</ol></details>}
              {tab === "plugins" && item.controllable && <button disabled={actionPending} onClick={() => control(item.state === "enabled" ? "plugin_disable" : "plugin_enable", item.id)}>{item.state === "enabled" ? "停用" : "启用"}</button>}
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
    </>}
  </PanelFrame>;
}
