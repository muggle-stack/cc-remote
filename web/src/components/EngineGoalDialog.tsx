import { useState, type ReactNode, type RefObject } from "react";
import type { GoalStatus, ThreadGoal } from "../protocol";
import { goalStatusName, goalTokens, validGoalLimit } from "../goal-presentation";
import { Icon } from "../icons";
import { GoalLimit, GoalMeter, GoalMore, GoalObjective, GoalSheet } from "./GoalDialog";

function duration(seconds: number) {
  return seconds < 60 ? `${seconds}s` : seconds < 3600 ? `${Math.floor(seconds / 60)}m`
    : `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`;
}

export default function EngineGoalDialog({ engine, goal, loading, disabled, scopeRef, onClose, onSave,
  onClear, onStatus, children }: {
  engine: "codex" | "claude"; goal: ThreadGoal | null; loading?: boolean; disabled?: boolean;
  scopeRef: RefObject<HTMLElement | null>; onClose: () => void;
  onSave: (objective: string, status: GoalStatus, budget: number | null) => void | Promise<void>;
  onClear: () => void | Promise<void>; onStatus?: (status: GoalStatus) => void | Promise<void>;
  children?: ReactNode;
}) {
  const [editing, setEditing] = useState(!goal);
  const [objective, setObjective] = useState(goal?.objective ?? "");
  const [budget, setBudget] = useState(goal?.tokenBudget ? String(goal.tokenBudget) : "");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const codex = engine === "codex";
  const locked = pending || disabled || loading;
  const valid = !!objective.trim() && (!codex || validGoalLimit(budget, !goal?.tokenBudget));
  const edit = () => {
    setObjective(goal?.objective ?? ""); setBudget(goal?.tokenBudget ? String(goal.tokenBudget) : "");
    setError(""); setEditing(true);
  };
  const run = async (action: () => void | Promise<void>, close = false) => {
    if (locked) return;
    setPending(true); setError("");
    try { await action(); if (close) onClose(); else setEditing(false); }
    catch (e) { setError(e instanceof Error ? e.message : "操作未完成，请重试。"); }
    finally { setPending(false); }
  };
  const footer = editing ? <>
    {codex && <GoalLimit value={budget} onChange={setBudget}
      allowUnlimited={!goal?.tokenBudget} disabled={locked} />}
    <button type="button" className="goal-cancel" onClick={() => goal ? setEditing(false) : onClose()}>取消</button>
    <button type="button" className="goal-primary" disabled={locked || !valid}
      onClick={() => void run(() => onSave(objective.trim(), goal?.status ?? "active", codex && budget ? Number(budget) : null))}>
      {pending ? "正在保存" : goal ? "保存修改" : "开始目标"}<Icon name="arrow-right" size={16} />
    </button>
  </> : <>
    <button type="button" className="goal-edit" disabled={locked} onClick={edit}><Icon name="edit" size={15} />修改目标</button>
    {codex && goal && goal.status !== "complete" && onStatus && <button type="button" className="goal-secondary"
      disabled={locked} onClick={() => void run(() => onStatus(goal.status === "active" ? "paused" : "active"))}>
      <Icon name={goal.status === "active" ? "pause" : "run"} size={14} />
      {goal.status === "active" ? "暂停续行" : "继续目标"}
    </button>}
    <GoalMore disabled={locked}>
      {codex && goal?.status !== "complete" && onStatus && <button type="button"
        onClick={() => void run(() => onStatus("complete"))}>标记完成</button>}
      <button type="button" className="goal-danger" onClick={() => void run(onClear, true)}>清除目标</button>
    </GoalMore>
  </>;
  return <GoalSheet engine={engine} scopeRef={scopeRef} onClose={onClose} footer={footer}
    title={`${codex ? "Codex" : "Claude"} · ${editing ? goal ? "修改目标" : "新目标" : "当前目标"}`}
    status={!editing && goal ? goalStatusName[goal.status] : undefined}>
    {children}
    {loading && !goal ? <p className="goal-hint" role="status">正在读取目标…</p>
      : editing ? <GoalObjective value={objective} onChange={setObjective} disabled={pending}
        hint={codex ? "围绕目标持续执行，直到完成或暂停。" : "写清完成条件，Claude 会持续检查。"} />
      : goal && <>
        <p className="goal-objective">{goal.objective}</p>
        {codex && goal.tokenBudget && <GoalMeter label="Token 预算用量" used={goal.tokensUsed} total={goal.tokenBudget} />}
        <div className="goal-stats">
          {!codex && <div><small>检查轮次</small><b>{goal.iterations ?? 0} 次</b></div>}
          <div><small>运行时间</small><b>{duration(goal.timeUsedSeconds)}</b></div>
          <div><small>已用 Token</small><b>{goalTokens(goal.tokensUsed)}</b></div>
          {codex && <div><small>Token 预算</small><b>{goal.tokenBudget ? goalTokens(goal.tokenBudget) : "不限"}</b></div>}
        </div>
        {!codex && <div className="goal-last-check"><small>最近一次检查</small>
          <p>{goal.lastReason || (goal.status === "complete" ? "目标已完成。" : "等待首次检查结果。")}</p>
        </div>}
      </>}
    {error && <p className="goal-error" role="alert">{error}</p>}
  </GoalSheet>;
}
