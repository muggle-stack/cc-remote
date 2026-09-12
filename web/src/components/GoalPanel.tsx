import { useEffect, useId, useRef, useState, type CSSProperties } from "react";
import EngineGoalDialog from "./EngineGoalDialog";
import { goalStatusName as statusName } from "../goal-presentation";
import type { GoalStatus, ThreadGoal } from "../protocol";
import { Icon } from "../icons";
import type { TurnPlanProgress } from "../plan-progress";
import { planProgressPresentation } from "../plan-progress";
import {
  PlanProgressContent,
  PlanProgressFloatingCard,
} from "./PlanProgressPopover";

interface Props {
  engine: "claude" | "codex";
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
  disabled?: boolean;
  onStatus?: (status: GoalStatus) => void | Promise<void>;
  onSave: (objective: string, status: GoalStatus, tokenBudget: number | null) => void | Promise<void>;
  onClear: () => void | Promise<void>;
}

export function GoalPanel(p: Props) {
  const [planOpen, setPlanOpen] = useState(false);
  const goalScopeRef = useRef<HTMLDivElement>(null);
  const planChipRef = useRef<HTMLButtonElement>(null);
  const planPopoverId = useId();
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
  if (!goalRevealed && !p.open && !standalonePlan) return null;
  const goal = p.goal;
  const used = goal?.tokensUsed ?? 0;
  const total = goal?.tokenBudget ?? null;
  const progress = total ? Math.min(100, used / total * 100) : null;
  const visualProgress = progress;
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

    {p.open && <EngineGoalDialog key={p.goal ? "existing" : "new"}
      engine={p.engine} goal={p.goal} loading={p.loading} disabled={p.disabled}
      scopeRef={goalScopeRef} onClose={p.onClose} onSave={p.onSave}
      onClear={p.onClear} onStatus={p.onStatus}>
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
    </EngineGoalDialog>}
  </>;
}
