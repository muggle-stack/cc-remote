import { useEffect, useId, useRef, useState, type CSSProperties } from "react";
import { useChatDialogGeometry } from "../chat-dialog-geometry";
import type { GoalStatus, ThreadGoal } from "../protocol";
import { Icon } from "../icons";
import type { TurnPlanProgress } from "../plan-progress";
import { planProgressPresentation } from "../plan-progress";
import {
  PlanProgressContent,
  PlanProgressFloatingCard,
} from "./PlanProgressPopover";

interface Props {
  engine: "claude" | "codex" | "dsh";
  goal: ThreadGoal | null;
  revealed: boolean;
  open: boolean;
  loading?: boolean;
  completedGoalRetired?: boolean;
  plan?: TurnPlanProgress | null;
  onLoadPlanDetail?: () => void;
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
  const [planOpen, setPlanOpen] = useState(false);
  const closeGoal = p.onClose;
  const goalScopeRef = useRef<HTMLDivElement>(null);
  const planChipRef = useRef<HTMLButtonElement>(null);
  const planPopoverId = useId();
  const dialogGeometry = useChatDialogGeometry({
    open: p.open,
    maxWidth: 560,
    maxHeight: 740,
    scopeRef: goalScopeRef,
  });
  useEffect(() => {
    setObjective(p.goal?.objective ?? "");
    setStatus(p.goal?.status ?? "active");
    setBudget(p.goal?.tokenBudget ? String(p.goal.tokenBudget) : "");
  }, [p.goal, p.open]);
  // Authoritative detail may replace a provisional plan item id inside the
  // same turn. Keep the sheet open across that refresh; only a new turn owns a
  // genuinely different plan entry.
  useEffect(() => setPlanOpen(false), [p.plan?.turnId]);

  const goalRevealed = p.revealed && !p.completedGoalRetired;
  // An explicit /goal read may still open the completed Goal for inspection,
  // but a Plan owned by the next task must remain a separate monitor.
  const planMergedIntoGoal = !!p.plan && !p.completedGoalRetired
    && (goalRevealed || p.open);
  const standalonePlan = planMergedIntoGoal ? null : p.plan;
  useEffect(() => {
    if (planMergedIntoGoal && !p.open) setPlanOpen(false);
  }, [planMergedIntoGoal, p.open]);
  useEffect(() => {
    if (!p.open) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeGoal();
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [closeGoal, p.open]);
  if (!goalRevealed && !p.open && !standalonePlan) return null;
  const goal = p.goal;
  const used = goal?.tokensUsed ?? 0;
  const total = goal?.tokenBudget ?? null;
  const progress = total ? Math.min(100, used / total * 100) : null;
  const visualProgress = goal?.status === "complete" ? 100 : progress;
  const engineName = p.engine === "dsh" ? "DSH" : p.engine === "codex" ? "Codex" : "Claude";
  const planPresentation = p.plan
    ? planProgressPresentation(p.plan.block, p.plan.detailLoading) : null;
  const planHeadline = planPresentation?.stale
    ? planPresentation.stateLabel
    : planPresentation?.currentStep ?? planPresentation?.description
      ?? planPresentation?.stateLabel;
  const openGoal = () => p.onOpen();

  return <>
    {standalonePlan && planPresentation && (
      <div ref={goalScopeRef} className="goal-chip-wrap plan-chip-wrap">
        <button ref={planChipRef} type="button" className="goal-chip plan-chip"
          aria-expanded={planOpen} aria-controls={planPopoverId}
          onClick={() => {
            const next = !planOpen;
            if (next) p.onLoadPlanDetail?.();
            setPlanOpen(next);
          }} aria-label={`查看计划进度，${planPresentation.progressLabel}`}>
          <span className={`goal-chip-ring plan-chip-ring${planPresentation.complete ? " complete" : ""}${planPresentation.failed ? " failed" : ""}${planPresentation.stale ? " stale" : ""}`}
            aria-hidden="true"
            style={{ "--goal-progress": `${planPresentation.progress * 3.6}deg` } as CSSProperties}>
            <Icon name={planPresentation.complete ? "verify" : "plan"} size={11} />
          </span>
          <span className="goal-chip-label">计划</span>
          <span className="goal-chip-objective">
            {planHeadline}
          </span>
          <span className="goal-chip-status">{planPresentation.progressLabel}</span>
        </button>
        <PlanProgressFloatingCard anchorRef={planChipRef}
          block={standalonePlan.block} open={planOpen}
          onOpenChange={setPlanOpen} id={planPopoverId}
          detailLoading={standalonePlan.detailLoading} compact />
      </div>
    )}
    {goalRevealed && !goal &&
      <div ref={goalScopeRef} className="goal-chip-wrap goal-loading" role="status"
        aria-label={p.loading ? "正在恢复 Goal" : "Goal 暂时不可用，可重试"}>
        <button className="goal-chip goal-chip-loading" onClick={openGoal}>
          <span className="goal-chip-dot goal-chip-dot-active" aria-hidden="true" />
          <span className="goal-chip-label">Goal</span>
          <span className="goal-chip-objective">
            {p.loading ? "正在恢复…" : "点击重试"}
          </span>
        </button>
      </div>}
    {goalRevealed && goal && <div ref={goalScopeRef}
      className={`goal-chip-wrap goal-${goal.status}`}>
      <button className="goal-chip" onClick={openGoal}
        aria-label={`查看 Goal，${statusName[goal.status]}`}>
        {visualProgress != null
          ? <span className={`goal-chip-ring goal-chip-ring-${goal.status}`}
              aria-hidden="true"
              style={{ "--goal-progress": `${visualProgress * 3.6}deg` } as CSSProperties}>
              <Icon name={goal.status === "complete" ? "verify" : "plan"} size={11} />
            </span>
          : <span className={`goal-chip-dot goal-chip-dot-${goal.status}`} aria-hidden="true" />}
        <span className="goal-chip-label">Goal</span>
        <span className="goal-chip-objective">{goal.objective}</span>
        <span className={`goal-chip-status goal-chip-status-${goal.status}`}>
          {statusName[goal.status]}
        </span>
      </button>
      <button className="goal-chip-dismiss" onClick={p.onDismiss} aria-label="隐藏 Goal">
        <Icon name="close" size={12} />
      </button>
    </div>}

    {p.open && dialogGeometry && <>
      <div className="scrim show" onClick={p.onClose} />
      <section className="sheet show goal-sheet" role="dialog" aria-modal="true"
        aria-label={`${engineName} Goal`} style={dialogGeometry}>
        <div className="sheet-grip" />
        <header className="goal-sheet-head">
          <span className="goal-sheet-icon"><Icon name="plan" size={18} /></span>
          <span><b>{engineName} Goal</b><small>目标、预算与执行进展</small></span>
          <button onClick={p.onClose} aria-label="关闭"><Icon name="close" size={17} /></button>
        </header>

        <div className="goal-sheet-scroll">
          {p.plan && planPresentation && <section className="goal-plan-section">
            <button type="button" className="goal-plan-entry"
              aria-expanded={planOpen}
              aria-label={`查看计划进度，${planPresentation.progressLabel}`}
              onClick={() => {
                const next = !planOpen;
                if (next) p.onLoadPlanDetail?.();
                setPlanOpen(next);
              }}>
              <span className={`goal-chip-ring plan-chip-ring${planPresentation.complete ? " complete" : ""}${planPresentation.failed ? " failed" : ""}${planPresentation.stale ? " stale" : ""}`}
                aria-hidden="true"
                style={{ "--goal-progress": `${planPresentation.progress * 3.6}deg` } as CSSProperties}>
                <Icon name={planPresentation.complete ? "verify" : "plan"} size={11} />
              </span>
              <span className="goal-plan-entry-copy">
                <b>计划</b>
                <small>{planHeadline}</small>
              </span>
              <strong>{planPresentation.progressLabel}</strong>
              <span className={`goal-plan-entry-chev${planOpen ? " open" : ""}`}
                aria-hidden="true"><Icon name="chev" size={15} /></span>
            </button>
            {planOpen && <div className="goal-plan-expanded"
              aria-label="计划执行状态">
              <PlanProgressContent block={p.plan.block}
                detailLoading={p.plan.detailLoading} />
            </div>}
          </section>}

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
            {goal.lastReason && <div className="goal-last-check"><small>最近进展</small><p>{goal.lastReason}</p></div>}
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
