import { useEffect, useState } from "react";
import type { DshApiClient } from "../dsh-api";
import type { DshItem } from "../protocol";
import { ChoicePicker } from "./ChoicePicker";
import { Icon } from "../icons";

interface Step { step: number; wall: number | null; model: number | null; tool: number | null; input: number | null; output: number | null; ttft: number | null }
interface Tool { name: string; step: number; duration: number | null; inner: boolean; error: boolean }
interface Stats { turn: number; wall: number | null; model: number | null; tool: number | null;
  input: number | null; output: number | null; cacheHit: number | null; partial: boolean; truncated: boolean; pendingTools: number; steps: Step[]; tools: Tool[] }
const duration = (value: number | null | undefined) => value == null ? "—" : value < 1000 ? `${Math.round(value)} ms` : `${(value / 1000).toFixed(1)} s`;
const tokens = (value: number | null | undefined) => value == null ? "—" : value.toLocaleString();

export function DshStats({ sid, api, onTrajectory }: { sid: string; api: DshApiClient; onTrajectory: () => void }) {
  const [items, setItems] = useState<DshItem[]>([]), [selected, setSelected] = useState("");
  const [tab, setTab] = useState("overview"), [revision, setRevision] = useState(0);
  const [next, setNext] = useState<number | null>(null), [before, setBefore] = useState<number>();
  const [loading, setLoading] = useState(true), [error, setError] = useState("");
  useEffect(() => {
    const abort = new AbortController(); setLoading(true); setError("");
    api.read(sid, "trajectory", { query: "stats", before_seq: before, signal: abort.signal }).then(result => {
      if (abort.signal.aborted) return;
      if (result.error) throw new Error(result.error);
      setItems(old => [...(before == null ? [] : old.filter(row => !result.items.some(item => item.id === row.id))), ...result.items]
        .sort((a, b) => Number(b.id) - Number(a.id)));
      setSelected(old => old || result.items.at(-1)?.id || "");
      setNext(result.has_more ? result.next_seq ?? null : null);
    }).catch(err => { if (!abort.signal.aborted) setError(err.message); })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [sid, api, before, revision]);
  const row = items.find(item => item.id === selected) ?? items[0];
  const data = row?.data as unknown as Stats | undefined;
  useEffect(() => {
    if (row?.state !== "running") return;
    const timer = setTimeout(() => { setBefore(undefined); setRevision(value => value + 1); }, 5000);
    return () => clearTimeout(timer);
  }, [row]);
  return <div className="dsh-stats dsh-management-content">
    <div className="dsh-stats-select"><ChoicePicker label="选择统计轮次" value={row?.id ?? ""}
      options={items.map(item => ({ value: item.id, label: item.title }))} onChange={setSelected}>{row?.title || "选择轮次"}</ChoicePicker>
      <button className="iconbtn" aria-label="刷新统计" disabled={loading} onClick={() => { setBefore(undefined); setRevision(value => value + 1); }}><Icon name="refresh" size={17} /></button></div>
    <div className="dsh-catalog-tabs">{[["overview", "概览"], ["steps", "步骤"], ["tools", "工具"]].map(([key, title]) => <button key={key} aria-pressed={tab === key} onClick={() => setTab(key)}>{title}</button>)}</div>
    {error && <p role="alert" className="dsh-feature-error">{error}</p>}
    {loading && !data && <p role="status">读取统计…</p>}
    {!loading && !error && !data && <p className="dsh-feature-empty">暂无可统计的执行记录</p>}
    {data && <>
      {tab === "overview" && <>
        <div className="dsh-stats-metrics">{[["总用时", duration(data.wall)], ["模型", duration(data.model)], ["工具", duration(data.tool)], ["缓存命中", data.cacheHit == null ? "—" : `${(data.cacheHit * 100).toFixed(1)}%`]].map(([label, value]) => <div key={label}><small>{label}</small><strong>{value}</strong></div>)}</div>
        <div className="dsh-stats-bars">{[["模型", data.model], ["工具", data.tool]].map(([label, value]) => <div key={String(label)}><span>{label}</span><progress value={Number(value ?? 0)} max={Math.max(data.wall ?? 0, data.model ?? 0, data.tool ?? 0, 1)} /><span>{duration(value as number | null)}</span></div>)}</div>
        <div className="dsh-stats-tokens"><span>输入 {tokens(data.input)}</span><span>输出 {tokens(data.output)}</span></div>
        <p className="dsh-management-meta">输入包含缓存读取与写入；并行工具按实际时间区间合并。</p>
      </>}
      {tab === "steps" ? <div className="dsh-stats-table"><table><thead><tr><th>步骤</th><th>模型</th><th>工具</th><th>输入</th><th>输出</th></tr></thead><tbody>{data.steps.map(step => <tr key={step.step}><td>{step.step}</td><td>{duration(step.model)}</td><td>{duration(step.tool)}</td><td>{tokens(step.input)}</td><td>{tokens(step.output)}</td></tr>)}</tbody></table></div>
        : <><h3>工具耗时</h3><div className="dsh-stats-tools">{data.tools.slice(0, tab === "overview" ? 6 : undefined).map((tool, index) => <div key={index}><span>{tool.name}<small>步骤 {tool.step}{tool.inner ? " · 内层调用" : ""}{tool.error ? " · 失败" : ""}</small></span><span>{duration(tool.duration)}</span></div>)}{!data.tools.length && <p className="dsh-management-meta">这一轮没有工具调用。</p>}</div></>}
      {(data.partial || data.truncated || data.pendingTools > 0) && <p className="dsh-management-meta">{data.partial ? "部分测量尚不可用，已知数据先行显示。" : ""}{data.pendingTools > 0 ? ` ${data.pendingTools} 个工具尚未返回。` : ""}{data.truncated ? "明细过多，当前只展示前段。" : ""}</p>}
      <button className="dsh-feature-link" onClick={onTrajectory}>查看执行轨迹 →</button>
    </>}
    {next !== null && <button disabled={loading} onClick={() => setBefore(next)}>加载更早轮次</button>}
  </div>;
}
