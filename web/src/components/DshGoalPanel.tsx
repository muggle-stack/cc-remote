import { useEffect, useState } from "react";
import type { DshGoal } from "../protocol";
import { Icon } from "../icons";

const phases = { active: "目标有效", paused: "已暂停", blocked: "需要处理", complete: "已完成" };

/** Native rounds and activation are separate from Codex token budgets. */
export default function DshGoalPanel({ goal, disabled, onCommand }: {
  goal: DshGoal;
  disabled: boolean;
  onCommand: (line: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [objective, setObjective] = useState("");
  const [submitted, setSubmitted] = useState<string | null>(null);
  useEffect(() => {
    if (submitted !== null && goal.objective === submitted) {
      setEditing(false);
      setSubmitted(null);
    }
  }, [goal.objective, submitted]);
  const phase = goal.phase === "active" && goal.activation === "armed" ? "自动续行已启用"
    : goal.phase === "active" && goal.activation === "disarmed" ? "等待继续" : phases[goal.phase];
  return <details className="dsh-goal">
    <summary><Icon name="spark" size={16} /><span>{goal.objective}</span><small>{phase}</small></summary>
    <div className="dsh-goal-body">
      <p>{goal.objective}</p>
      <div className="dsh-goal-meter"><span>目标轮次</span><span>{goal.rounds} / {goal.max_rounds}</span></div>
      <progress value={goal.rounds} max={goal.max_rounds} aria-label="目标轮次" />
      {goal.blocked_reason && <p className="dsh-connection-note">{goal.blocked_reason}</p>}
      {goal.phase === "active" && !goal.activation && <p className="dsh-goal-note">自动续行状态尚未确认；打开历史不会启动目标。</p>}
      {editing && <form onSubmit={event => {
        event.preventDefault();
        if (objective.trim()) { setSubmitted(objective.trim()); onCommand(`/goal edit ${objective.trim()}`); }
      }}>
        <textarea aria-label="编辑目标" value={objective} onChange={event => setObjective(event.target.value)} maxLength={64000} />
        <button type="submit" disabled={disabled || !objective.trim()}>保存</button>
        <button type="button" onClick={() => setEditing(false)}>取消</button>
      </form>}
      <div className="dsh-goal-actions">
        {goal.phase === "active" && goal.activation === "armed"
          ? <button disabled={disabled} onClick={() => onCommand("/goal pause")}>暂停续行</button>
          : goal.phase !== "complete" && <button disabled={disabled} onClick={() => onCommand("/goal resume")}>继续目标</button>}
        <button disabled={disabled} onClick={() => { setSubmitted(null); setObjective(goal.objective); setEditing(true); }}>编辑</button>
        <button disabled={disabled} onClick={() => onCommand("/goal clear")}>清除目标</button>
      </div>
    </div>
  </details>;
}
