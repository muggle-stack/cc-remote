import { useEffect, useState } from "react";
import type { DshApiClient } from "../dsh-api";
import type { DshItem } from "../protocol";
import { Icon } from "../icons";
import { ChoicePicker } from "./ChoicePicker";
import { DateTimePicker } from "./DateTimePicker";
import { DshScheduleCleanup } from "./DshScheduleCleanup";

const rules = [
  { value: "at", label: "指定时间", description: "只执行一次" },
  { value: "every", label: "固定间隔", description: "每隔一段时间执行" },
  { value: "daily", label: "每天" }, { value: "weekly", label: "每周" },
  { value: "cron", label: "自定义 Cron", description: "使用五段式表达式" },
  { value: "rrule", label: "自定义 RRULE" },
];
const weekdays = ["一", "二", "三", "四", "五", "六", "日"];
const dateLabel = (value: unknown) => {
  const date = new Date(String(value ?? ""));
  return Number.isNaN(date.getTime()) ? "时间待确认" : date.toLocaleString("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
};
function description(item: DshItem) {
  const d = item.data ?? {};
  if (d.kind === "every") return `每 ${Number(d.everySeconds) / 60} 分钟`;
  if (d.kind === "daily") return `每天 ${String(d.time).slice(0, 5)}`;
  if (d.kind === "weekly") return `每周${(d.weekdays as number[] ?? []).map(n => weekdays[n - 1]).join("、")} ${String(d.time).slice(0, 5)}`;
  if (d.kind === "cron") return String(d.expression);
  if (d.kind === "rrule") return "RRULE · " + String(d.rrule);
  return "单次任务";
}

export function DshSchedules({ sid, api, onCompose }: { sid: string; api: DshApiClient; onCompose: (prompt: string) => void }) {
  const [items, setItems] = useState<DshItem[]>([]), [sessions, setSessions] = useState<DshItem[]>([]);
  const [revision, setRevision] = useState(0), [loading, setLoading] = useState(true), [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [editing, setEditing] = useState<DshItem | "new" | null>(null);
  const [next, setNext] = useState<number | null>(null);
  const [menu, setMenu] = useState("");
  const [remove, setRemove] = useState("");
  const [history, setHistory] = useState<{ id: string; rows: DshItem[] } | null>(null);
  useEffect(() => {
    const abort = new AbortController(); setLoading(true);
    Promise.all([api.read(sid, "schedules", { signal: abort.signal }),
      api.read(sid, "schedules", { query: "sessions", signal: abort.signal })]).then(([list, catalog]) => {
      if (abort.signal.aborted) return;
      setItems(list.items); setSessions(catalog.items); setNext(list.has_more ? list.next_seq ?? null : null);
      if (list.error || catalog.error) setNotice(list.error || catalog.error || "");
    }).catch(error => { if (!abort.signal.aborted) setNotice(error.message); })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [sid, api, revision]);
  useEffect(() => {
    if (editing || busy || !items.some(item => item.data?.executionActive || item.data?.deliveryFloorEndsAt)) return;
    const timer = setTimeout(() => setRevision(n => n + 1), 2500);
    return () => clearTimeout(timer);
  }, [items, editing, busy]);
  const mutate = async (body: Record<string, unknown>) => {
    setBusy(true); setNotice("");
    try {
      const result = await api.control(sid, "manage", JSON.stringify(body));
      setNotice(result.text);
      if (result.status === "success") { setEditing(null); setRemove(""); }
    } catch (error) {
      setNotice(`${(error as Error).message}；请先刷新核实结果，操作不会自动重发。`);
    } finally { setBusy(false); setRevision(n => n + 1); }
  };
  const loadMore = async () => {
    if (next === null) return;
    setBusy(true);
    try {
      const page = await api.read(sid, "schedules", { before_seq: next });
      if (page.error) throw new Error(page.error);
      setItems(old => [...old, ...page.items.filter(row => !old.some(item => item.id === row.id))]);
      setNext(page.has_more ? page.next_seq ?? null : null);
    } catch (error) { setNotice((error as Error).message); }
    finally { setBusy(false); }
  };
  const taskAction = (item: DshItem, action: string) => void mutate({ action, id: item.id, expectedUpdatedAt: item.data?.updatedAt ?? null });
  const targetLabel = (item: DshItem) => {
    const target = item.data?.target as { kind?: string; create?: { cwd?: string } } | undefined;
    if (target?.kind === "new") return "每次新建会话" + (target.create?.cwd ? ` · ${target.create.cwd}` : "");
    if (target?.kind === "new_once") return "首次新建，之后复用" + (target.create?.cwd ? ` · ${target.create.cwd}` : "");
    return sessions.find(s => s.id === item.sid)?.title ?? item.sid ?? "目标会话";
  };
  const showHistory = async (item: DshItem) => {
    setMenu(""); setBusy(true);
    try {
      const result = await api.read(sid, "schedule_history", { query: item.id, target_sid: item.sid ?? undefined });
      if (result.error) throw new Error(result.error);
      setHistory({ id: item.id, rows: result.items });
    } catch (error) { setNotice((error as Error).message); }
    finally { setBusy(false); }
  };
  return <div className="dsh-management dsh-schedules">
    <div className="dsh-schedules-heading"><span>{editing ? editing === "new" ? "新建任务" : "编辑任务" : "按计划，继续你的工作"}</span>
      {!editing && <><button className="iconbtn" aria-label="刷新任务" disabled={loading || busy} onClick={() => setRevision(n => n + 1)}><Icon name="refresh" size={17} /></button>
        {!!items.length && <button className="dsh-primary" disabled={busy} onClick={() => setEditing("new")}><Icon name="plus" size={16} />新建任务</button>}
        <DshScheduleCleanup sid={sid} api={api} disabled={loading || busy} onBusy={setBusy} onRefresh={() => setRevision(value => value + 1)} /></>}
    </div>
    <div className="dsh-management-content">
      {notice && <p role="status" className="dsh-management-notice">{notice}</p>}
      {editing && editing !== "new" && items.some(row => row.id === editing.id && row.data?.updatedAt !== editing.data?.updatedAt) &&
        <p role="status">任务已有新版本。<button type="button" disabled={busy} onClick={() => setEditing(items.find(row => row.id === editing.id) ?? editing)}>读取最新版本，保留草稿</button></p>}
      {editing ? <ScheduleEditor key={typeof editing === "string" ? "new" : editing.id} item={editing === "new" ? undefined : editing}
        sid={sid} sessions={sessions} busy={busy} onCancel={() => setEditing(null)}
        onSave={body => void mutate(body)} /> : <>
        {loading && !items.length && <p role="status" className="dsh-feature-empty">读取中…</p>}
        {!loading && !items.length && <div className="dsh-schedule-empty"><span><Icon name="clock" size={28} /></span>
          <h3>把下一件事交给计划</h3><p>选择时间和执行位置，让 DSH 继续工作。</p>
          <button className="dsh-primary" onClick={() => setEditing("new")}><Icon name="plus" size={17} />新建任务</button>
          <button className="dsh-schedule-converse" onClick={() => onCompose("请帮我创建一个定时任务，先和我确认任务内容、时间和时区。")}>也可以通过对话创建</button>
        </div>}
        {items.map(item => <article className="dsh-schedule-row" key={item.id}>
          <div className="dsh-schedule-title"><span className="dsh-schedule-symbol"><Icon name="clock" size={19} /></span>
            <div><strong>{item.title}</strong><small>{description(item)}{item.data?.timeZone ? ` · ${item.data.timeZone}` : ""}</small></div>
            <span className="dsh-schedule-state" data-active={item.state === "active"}>{item.data?.executionActive ? "执行中" : item.state === "active" ? "已安排" : item.state === "paused" ? "已暂停" : "已结束"}</span>
            <button className="iconbtn" aria-label={`${item.title}的操作`} aria-expanded={menu === item.id} onClick={() => setMenu(menu === item.id ? "" : item.id)}><Icon name="dots" size={18} /></button>
          </div>
          <p>{item.detail}</p>
          <div className="dsh-schedule-meta"><span>{item.state === "active" ? "下次执行" : "计划时间"} · {dateLabel(item.data?.scheduledAt)}</span>
            <span><Icon name="chat" size={13} />{targetLabel(item)}</span></div>
          {!!item.data?.deliveryFloorEndsAt && <small className="dsh-management-meta">下次可手动执行 · {dateLabel(item.data.deliveryFloorEndsAt)}</small>}
          <div className="dsh-management-actions"><button disabled={busy || !!item.data?.executionActive || (!!item.data?.deliveryFloorEndsAt && new Date(String(item.data.deliveryFloorEndsAt)).getTime() > Date.now())} onClick={() => taskAction(item, "schedule_run")}>立即运行</button>
            {item.state !== "ended" && <button disabled={busy} onClick={() => taskAction(item, item.state === "paused" ? "schedule_resume" : "schedule_pause")}>{item.state === "paused" ? "恢复" : "暂停"}</button>}</div>
          {menu === item.id && <div className="dsh-schedule-menu"><button disabled={busy} onClick={() => { setEditing(item); setMenu(""); }}>编辑任务</button><button disabled={busy} onClick={() => void showHistory(item)}>执行记录</button><button disabled={busy} onClick={() => { setRemove(item.id); setMenu(""); }}>删除任务</button></div>}
          {remove === item.id && <div className="dsh-inline-confirm"><p>删除这个任务及其投递记录？已有会话内容会保留。</p><button disabled={busy} onClick={() => setRemove("")}>取消</button><button disabled={busy} onClick={() => taskAction(item, "schedule_delete")}>确认删除</button></div>}
          {history?.id === item.id && <div className="dsh-deliveries"><strong>执行记录</strong><p>记录表示指令已送达会话，任务完成情况请查看会话回复。</p>{history.rows.map(row => <p key={row.id}>{dateLabel(row.title)} · 已送达 · {sessions.find(s => s.id === row.sid)?.title ?? row.sid ?? "目标待确认"}</p>)}{!history.rows.length && <p>还没有执行记录</p>}<button onClick={() => setHistory(null)}>收起</button></div>}
        </article>)}
        {next !== null && <button disabled={busy} onClick={() => void loadMore()}>加载更多任务</button>}
      </>}
    </div>
  </div>;
}

function ScheduleEditor({ item, sid, sessions, busy, onSave, onCancel }: { item?: DshItem; sid: string; sessions: DshItem[]; busy: boolean; onSave: (body: Record<string, unknown>) => void; onCancel: () => void }) {
  const [title, setTitle] = useState(item?.title ?? ""), [prompt, setPrompt] = useState(item?.detail ?? "");
  const originalTarget = item?.data?.target as { kind?: string; sid?: string; create?: { cwd?: string; agentPreset?: string } } | undefined;
  const [targetDirty, setTargetDirty] = useState(false);
  const [targetKind, setTargetKind] = useState(originalTarget?.kind ?? "session");
  const [target, setTarget] = useState(item?.sid ?? sid);
  const [cwd, setCwd] = useState(originalTarget?.create?.cwd ?? "");
  const [kind, setKind] = useState(item ? "keep" : "daily"), [time, setTime] = useState("09:00");
  const [date, setDate] = useState(""), [minutes, setMinutes] = useState("60"), [cron, setCron] = useState("0 9 * * 1-5");
  const [zone, setZone] = useState(String(item?.data?.timeZone ?? Intl.DateTimeFormat().resolvedOptions().timeZone));
  const [rrule, setRrule] = useState(String(item?.data?.rrule ?? "FREQ=WEEKLY;BYDAY=MO,FR"));
  const [days, setDays] = useState<number[]>([1, 2, 3, 4, 5]);
  const choices = item ? [{ value: "keep", label: "保持当前时间" }, ...rules] : rules;
  const save = () => {
    const timing = kind === "at" ? { at: { date: date.split("T")[0], time: date.split("T")[1] + ":00", time_zone: zone } }
      : kind === "every" ? { every_seconds: Number(minutes) * 60 }
      : kind === "daily" ? { daily: { time: time + ":00", time_zone: zone } }
      : kind === "weekly" ? { weekly: { time: time + ":00", time_zone: zone, weekdays: days } }
      : kind === "rrule" ? { rrule } : { cron: { expression: cron, time_zone: zone } };
    const destination = targetKind === "session" ? { kind: targetKind, sid: target }
      : { kind: targetKind, create: { ...(cwd ? { cwd } : {}), ...(originalTarget?.create?.agentPreset ? { agentPreset: originalTarget.create.agentPreset } : {}) } };
    const patch = { title, prompt, ...(!item || targetDirty ? { target: destination } : {}), ...(kind === "keep" ? {} : { timing: { kind, ...timing } }) };
    onSave(item ? { action: "schedule_update", id: item.id, expectedUpdatedAt: item.data?.updatedAt ?? null, patch }
      : { action: "schedule_create", request: patch });
  };
  return <form className="dsh-schedule-editor" onSubmit={event => { event.preventDefault(); save(); }}>
    <label>任务名称<input aria-label="任务名称" required value={title} maxLength={120} placeholder="例如：检查项目进展" onChange={e => setTitle(e.target.value)} /></label>
    <label>任务内容<textarea aria-label="任务内容" required value={prompt} maxLength={24000} placeholder="到时间后，你希望 DSH 做什么？" onChange={e => setPrompt(e.target.value)} /></label>
    <fieldset className="dsh-target-choices" disabled={busy}><legend>执行位置</legend>
      {[["session", "已有会话", "继续指定会话中的工作"], ["new_once", "首次新建，之后复用", "首次执行时创建会话，保留后续上下文"], ["new", "每次新建会话", "每次执行都使用独立上下文"]].map(([value, label, detail]) =>
        <label key={value} data-selected={targetKind === value}><input type="radio" name="schedule-target" value={value} checked={targetKind === value} onChange={() => { setTargetKind(value); setTargetDirty(true); }} /><span><strong>{label}</strong><small>{detail}</small></span></label>)}
    </fieldset>
    {targetKind === "session" ? <div className="dsh-schedule-field"><span>目标会话</span><ChoicePicker label="选择目标会话" value={target}
      options={sessions.map(s => ({ value: s.id, label: s.title, description: s.detail }))} onChange={value => { setTarget(value); setTargetDirty(true); }}>
      {sessions.find(s => s.id === target)?.title ?? target}</ChoicePicker></div>
      : <label>项目目录<input list="dsh-schedule-projects" value={cwd} placeholder="留空使用 DSH 默认项目" onChange={e => { setCwd(e.target.value); setTargetDirty(true); }} />
        <datalist id="dsh-schedule-projects">{[...new Set(sessions.map(s => s.detail).filter(Boolean))].map(path => <option key={path} value={path} />)}</datalist>
        <small>选择已有项目，或填写这台设备上的绝对路径。</small></label>}
    <div className="dsh-schedule-field"><span>重复规则</span><ChoicePicker label="选择重复规则" value={kind} options={choices} onChange={setKind}>{choices.find(c => c.value === kind)?.label}</ChoicePicker></div>
    {kind === "at" && <DateTimePicker value={date} onChange={setDate} />}
    {kind === "every" && <label>间隔（分钟）<input type="number" min={1} step={1} required value={minutes} onChange={e => setMinutes(e.target.value)} /></label>}
    {["daily", "weekly"].includes(kind) && <label>执行时间<input aria-label="执行时间" required value={time} placeholder="09:00" pattern="([01][0-9]|2[0-3]):[0-5][0-9]" onChange={e => setTime(e.target.value)} /></label>}
    {kind === "weekly" && <div className="dsh-schedule-week" role="group" aria-label="每周执行日期">{weekdays.map((d, i) => <button type="button" key={d} aria-pressed={days.includes(i + 1)} onClick={() => setDays(old => old.includes(i + 1) ? old.filter(n => n !== i + 1) : [...old, i + 1].sort())}>{d}</button>)}</div>}
    {kind === "cron" && <label>Cron 表达式<input required value={cron} onChange={e => setCron(e.target.value)} /><small>分钟 · 小时 · 日 · 月 · 星期</small></label>}
    {kind === "rrule" && <label>RRULE 规则<input required value={rrule} onChange={e => setRrule(e.target.value)} /></label>}
    {["at", "daily", "weekly", "cron"].includes(kind) && <label>时区<input required value={zone} placeholder="Asia/Shanghai" onChange={e => setZone(e.target.value)} /></label>}
    <div className="dsh-schedule-form-actions"><button type="button" disabled={busy} onClick={onCancel}>取消</button><button className="dsh-primary" disabled={busy || (targetKind !== "session" && !!cwd && !cwd.startsWith("/")) || !title.trim() || !prompt.trim() || (kind === "at" && !date) || (kind === "weekly" && !days.length)}>{busy ? "正在保存…" : item ? "保存修改" : "创建任务"}</button></div>
  </form>;
}
