import { useEffect, useState, type CSSProperties } from "react";
import type { GoalStatus, ThreadGoal } from "../protocol";
import { Icon } from "../icons";

interface Props {
  engine: "claude" | "codex";
  goal: ThreadGoal | null;
  revealed: boolean;
  open: boolean;
  onOpen: () => void;
  onClose: () => void;
  onDismiss: () => void;
  onSave: (objective: string, status: GoalStatus, tokenBudget: number | null) => void;
  onClear: () => void;
}

const statusName: Record<GoalStatus, string> = {
  active: "进行中", paused: "已暂停", blocked: "受阻",
  usageLimited: "用量受限", budgetLimited: "预算已满", complete: "已完成",
};

function tokens(value?: number | null): string {
  if (value == null) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(value >= 10000 ? 0 : 1)}k` : String(value);
}

function duration(value?: number | null): string {
  if (value == null) return "—";
  if (value < 60) return `${value} 秒`;
  if (value < 3600) return `${Math.floor(value / 60)} 分钟`;
  return `${Math.floor(value / 3600)} 小时 ${Math.floor(value % 3600 / 60)} 分`;
}

export function GoalPanel(p: Props) {
  const [objective, setObjective] = useState("");
  const [status, setStatus] = useState<GoalStatus>("active");
  const [budget, setBudget] = useState("");
  useEffect(() => {
    setObjective(p.goal?.objective ?? "");
    setStatus(p.goal?.status ?? "active");
    setBudget(p.goal?.tokenBudget ? String(p.goal.tokenBudget) : "");
  }, [p.goal, p.open]);

  if (!p.revealed && !p.open) return null;
  const goal = p.goal;
  const used = goal?.tokensUsed ?? 0;
  const total = goal?.tokenBudget ?? null;
  const progress = total ? Math.min(100, used / total * 100) : null;
  const engineName = p.engine === "codex" ? "Codex" : "Claude";

  return <>
    {p.revealed && goal && <div className={`goal-card goal-${goal.status}`}>
      <button className="goal-card-main" onClick={p.onOpen} aria-label="查看 Goal">
        <span className="goal-symbol"><Icon name="verify" size={16} /></span>
        <span className="goal-card-copy">
          <span className="goal-card-kicker">
            <span>{engineName} Goal</span>
            <span className={`goal-status goal-status-${goal.status}`}>{statusName[goal.status]}</span>
          </span>
          <span className="goal-card-objective">{goal.objective}</span>
          <span className="goal-card-meta">
            {duration(goal.timeUsedSeconds)} · {tokens(goal.tokensUsed)} tokens
            {goal.iterations != null && ` · ${goal.iterations} 轮检查`}
          </span>
        </span>
        {progress != null && <span className="goal-ring" style={{ "--goal-progress": `${progress * 3.6}deg` } as CSSProperties}>
          <span>{Math.round(progress)}%</span>
        </span>}
      </button>
      <button className="goal-card-dismiss" onClick={p.onDismiss} aria-label="隐藏 Goal"><Icon name="close" size={14} /></button>
    </div>}

    {p.open && <>
      <div className="scrim show" onClick={p.onClose} />
      <section className="sheet show goal-sheet" role="dialog" aria-modal="true" aria-label={`${engineName} Goal`}>
        <div className="sheet-grip" />
        <header className="goal-sheet-head">
          <span className="goal-sheet-icon"><Icon name="verify" size={19} /></span>
          <span><b>{engineName} Goal</b><small>持续运行，直到目标达成或被清除</small></span>
          <button onClick={p.onClose} aria-label="关闭"><Icon name="close" size={17} /></button>
        </header>

        <div className="goal-sheet-scroll">
          {goal && <div className="goal-overview">
            <div className="goal-overview-row">
              <span className={`goal-status goal-status-${goal.status}`}>{statusName[goal.status]}</span>
              <span className="goal-overview-time">已运行 {duration(goal.timeUsedSeconds)}</span>
            </div>
            <p>{goal.objective}</p>
            {progress != null && <div className="goal-budget">
              <div><span>Token 预算</span><b>{tokens(used)} / {tokens(total)}</b></div>
              <i><span style={{ width: `${progress}%` }} /></i>
            </div>}
            <div className="goal-stats">
              <div><small>已用 Token</small><b>{tokens(goal.tokensUsed)}</b></div>
              <div><small>运行时间</small><b>{duration(goal.timeUsedSeconds)}</b></div>
              <div><small>{p.engine === "claude" ? "检查轮次" : "预算状态"}</small><b>{p.engine === "claude" ? String(goal.iterations ?? 0) : (total ? `${Math.round(progress ?? 0)}%` : "不限")}</b></div>
            </div>
            {goal.lastReason && <div className="goal-last-check"><small>最近检查</small><p>{goal.lastReason}</p></div>}
          </div>}

          <div className="goal-editor">
            <label><span>{goal ? "修改目标" : "设置目标"}</span>
              <textarea value={objective} maxLength={16384}
                onChange={(e) => setObjective(e.target.value)}
                placeholder="描述一个可验证、可以明确判断是否完成的目标…" />
            </label>
            {p.engine === "codex" && <div className="goal-editor-grid">
              <label><span>状态</span><select value={status} onChange={(e) => setStatus(e.target.value as GoalStatus)}>
                {Object.entries(statusName).map(([value, name]) => <option key={value} value={value}>{name}</option>)}
              </select></label>
              <label><span>Token 预算</span><input type="number" min="1" inputMode="numeric" value={budget}
                placeholder="不限制" onChange={(e) => setBudget(e.target.value)} /></label>
            </div>}
          </div>
        </div>

        <footer className="goal-sheet-actions">
          {goal && <button className="goal-danger" onClick={p.onClear}>清除 Goal</button>}
          <button className="goal-cancel" onClick={p.onClose}>取消</button>
          <button className="goal-primary" disabled={!objective.trim()} onClick={() => {
            p.onSave(objective.trim(), p.engine === "codex" ? status : "active", budget ? Number(budget) : null);
          }}>{goal ? "保存修改" : "开始 Goal"}</button>
        </footer>
      </section>
    </>}
  </>;
}
