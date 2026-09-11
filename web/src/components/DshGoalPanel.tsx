import { useRef, useState, type RefObject } from "react";
import type { DshGoal, DshGoalAction } from "../protocol";
import { validGoalLimit } from "../goal-presentation";
import { EngineIcon, Icon } from "../icons";
import { GoalLimit, GoalMeter, GoalMore, GoalObjective, GoalSheet } from "./GoalDialog";

function phase(goal: DshGoal) {
  if (goal.phase === "active") return goal.activation === "armed" ? "自动续行" : "等待继续";
  return { paused: "已暂停", blocked: "需要处理", complete: "已完成" }[goal.phase];
}

export default function DshGoalPanel({ goal, disabled, open, onOpen, onClose, onAction }: {
  goal: DshGoal | null; disabled: boolean; open: boolean; onOpen: () => void; onClose: () => void;
  onAction: (action: DshGoalAction) => Promise<void>;
}) {
  const scopeRef = useRef<HTMLDivElement>(null);
  return <>
    {goal && <div ref={scopeRef} className="goal-chip-wrap dsh-goal">
      <button className="goal-chip" onClick={onOpen} aria-label={`查看 DSH Goal，${phase(goal)}`}>
        <EngineIcon engine="dsh" size={19} /><span className="goal-chip-label">Goal</span>
        <span className="goal-chip-objective">{goal.objective}</span>
        <span className="goal-chip-status">{phase(goal)}</span>
      </button>
    </div>}
    {open && <DshGoalDialog goal={goal} disabled={disabled}
      scopeRef={scopeRef} onClose={onClose} onAction={onAction} />}
  </>;
}

function DshGoalDialog({ goal, disabled, scopeRef, onClose, onAction }: {
  goal: DshGoal | null; disabled: boolean; scopeRef: RefObject<HTMLElement | null>;
  onClose: () => void; onAction: (action: DshGoalAction) => Promise<void>;
}) {
  const [editing, setEditing] = useState(!goal);
  const [objective, setObjective] = useState(goal?.objective ?? "");
  const [rounds, setRounds] = useState(String(goal?.max_rounds ?? 256));
  const [editRef, setEditRef] = useState(goal ? { goal_id: goal.id, revision: goal.revision } : {});
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const locked = disabled || pending;
  const exhausted = !!goal && goal.rounds >= goal.max_rounds;
  const run = async (action: DshGoalAction) => {
    if (locked) return;
    setPending(true); setError("");
    try { await onAction(action); setEditing(false); if (action.action === "clear") onClose(); }
    catch (e) { setError(e instanceof Error ? e.message : "操作未完成，请重试。"); }
    finally { setPending(false); }
  };
  const transition = (action: DshGoalAction["action"]) => {
    if (goal) void run({ action, goal_id: goal.id, revision: goal.revision });
  };
  const edit = () => {
    setObjective(goal?.objective ?? ""); setRounds(String(goal?.max_rounds ?? 256));
    setEditRef(goal ? { goal_id: goal.id, revision: goal.revision } : {});
    setEditing(true); setError("");
  };
  return <GoalSheet engine="dsh" title="DSH 持续目标" status={!editing && goal ? phase(goal) : undefined}
    scopeRef={scopeRef} onClose={onClose} footer={editing ? <>
      <GoalLimit kind="rounds" value={rounds} onChange={setRounds} disabled={locked} />
      <button className="goal-cancel" onClick={() => goal ? setEditing(false) : onClose()}>取消</button>
      <button className="goal-primary" disabled={locked || !objective.trim() || !validGoalLimit(rounds)
          || Number(rounds) < (goal?.rounds ?? 0)} onClick={() => void run({
        action: editRef.goal_id ? "edit" : "create", ...editRef, objective: objective.trim(), max_rounds: Number(rounds),
      })}>{pending ? "正在保存" : editRef.goal_id ? "保存修改" : "开始目标"}<Icon name="arrow-right" size={16} /></button>
    </> : <>
      <button className="goal-edit" disabled={locked} onClick={edit}><Icon name="edit" size={15} />编辑目标</button>
      {goal && goal.phase !== "complete" && <button className="goal-secondary"
        disabled={locked || (exhausted && goal.activation !== "armed")}
        onClick={() => transition(goal.phase === "active" && goal.activation === "armed" ? "pause" : "resume")}>
        <Icon name={goal.activation === "armed" ? "pause" : "run"} size={14} />
        {goal.phase === "active" && goal.activation === "armed" ? "暂停续行" : "继续目标"}
      </button>}
      <GoalMore disabled={locked}>
        {goal?.phase !== "complete" && <button onClick={() => transition("complete")}>标记完成</button>}
        <button className="goal-danger" onClick={() => transition("clear")}>清除目标</button>
      </GoalMore>
    </>}>
    {editing ? <GoalObjective value={objective} onChange={setObjective} disabled={pending}
      hint="围绕目标持续执行，直到完成或暂停。" /> : goal && <>
      <p className="goal-objective">{goal.objective}</p>
      <GoalMeter label="轮次用量" used={goal.rounds} total={goal.max_rounds} rounds />
      {goal.blocked_reason && <div className="goal-last-check"><small>需要处理</small><p>{goal.blocked_reason}</p></div>}
      {exhausted && goal.phase !== "complete" && <p className="goal-hint">本轮目标已用完轮次，可编辑上限后继续。</p>}
    </>}
    {error && <div className="goal-error" role="alert">{error}
      {editing && goal && (goal.id !== editRef.goal_id || goal.revision !== editRef.revision) && <>
        <p>最新目标：{goal.objective}</p>
        <button className="goal-conflict-retry" onClick={() => {
          setEditRef({ goal_id: goal.id, revision: goal.revision }); setError("");
        }}>保留输入，使用最新版本</button>
      </>}
    </div>}
  </GoalSheet>;
}
