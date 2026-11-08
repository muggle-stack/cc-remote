import { useEffect, useRef, useState } from "react";
import type { DshApiClient } from "../dsh-api";
import { Icon } from "../icons";
import { DshConfirm } from "./DshConfirm";

export function DshScheduleCleanup({ sid, api, disabled, onBusy, onRefresh }: {
  sid: string; api: DshApiClient; disabled: boolean; onBusy: (busy: boolean) => void; onRefresh: () => void;
}) {
  const [menu, setMenu] = useState(false), [open, setOpen] = useState(false), [busy, setBusy] = useState(false);
  const [plan, setPlan] = useState<{ count: number; revision: string } | null>(null);
  const [notice, setNotice] = useState("");
  const root = useRef<HTMLDivElement>(null), trigger = useRef<HTMLButtonElement>(null);
  const readAbort = useRef<AbortController | null>(null);
  useEffect(() => () => readAbort.current?.abort(), []);
  useEffect(() => {
    if (!menu) return;
    const outside = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setMenu(false); };
    const key = (event: KeyboardEvent) => { if (event.key === "Escape") { event.stopPropagation(); setMenu(false); trigger.current?.focus(); } };
    root.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus();
    document.addEventListener("pointerdown", outside); document.addEventListener("keydown", key);
    return () => { document.removeEventListener("pointerdown", outside); document.removeEventListener("keydown", key); };
  }, [menu]);
  const prepare = async () => {
    setMenu(false); setOpen(true); setPlan(null); setNotice(""); setBusy(true);
    const abort = new AbortController(); readAbort.current?.abort(); readAbort.current = abort;
    try {
      const result = await api.read(sid, "schedules", { query: "cleanup", signal: abort.signal });
      if (abort.signal.aborted) return;
      const data = result.items[0]?.data;
      if (result.error || typeof data?.count !== "number" || typeof data?.revision !== "string") throw new Error(result.error || "无法读取清理清单");
      setPlan({ count: data.count, revision: data.revision });
    } catch (err) { if (!abort.signal.aborted) setNotice((err as Error).message); }
    finally { if (!abort.signal.aborted) setBusy(false); }
  };
  const confirm = async () => {
    if (!plan?.count || busy) return;
    const revision = plan.revision; setPlan(null); setBusy(true); onBusy(true); setNotice("");
    try {
      const result = await api.control(sid, "manage", JSON.stringify({ action: "schedule_cleanup", revision }));
      setNotice(result.text); onRefresh();
    } catch (err) { setNotice(`${(err as Error).message}；请刷新核实结果，操作不会自动重发。`); }
    finally { setBusy(false); onBusy(false); }
  };
  const close = () => { if (!busy) { setOpen(false); trigger.current?.focus(); } };
  return <div className="dsh-cleanup-menu" ref={root}>
    <button className="iconbtn" ref={trigger} aria-label="自动化更多操作" aria-haspopup="menu" aria-expanded={menu} disabled={disabled || busy} onClick={() => setMenu(value => !value)}><Icon name="dots" size={18} /></button>
    {menu && <div className="dsh-schedule-menu" role="menu"><button role="menuitem" onClick={() => void prepare()}>清理已结束任务…</button></div>}
    {open && <DshConfirm title="清理已结束任务？" busy={busy} onClose={close}>
      {plan ? <><p>{plan.count ? `将清理 ${plan.count} 个已结束任务及其投递记录。` : "没有需要清理的已结束任务。"}<br />已有会话内容保留。</p><p className="dsh-management-meta">进行中的任务不受影响。</p></>
        : <p role="status">{busy ? "正在处理…" : notice}</p>}
      <footer><button disabled={busy} onClick={close}>{!plan && !busy ? "关闭" : "取消"}</button>
        {plan && <button className="dsh-danger" disabled={busy || !plan.count} onClick={() => void confirm()}>清理 {plan.count} 项</button>}
        {!plan && !busy && <button onClick={() => void prepare()}>重新读取</button>}
      </footer>
    </DshConfirm>}
  </div>;
}
