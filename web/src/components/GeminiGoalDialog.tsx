import { useState } from "react";
import { Icon } from "../icons";
import { GoalObjective, GoalSheet } from "./GoalDialog";

export default function GeminiGoalDialog({ disabled, onClose, onSend }: {
  disabled: boolean;
  onClose: () => void;
  onSend: (prompt: string) => boolean;
}) {
  const [objective, setObjective] = useState("");
  const [failed, setFailed] = useState(false);
  const description = objective.trim();

  return <GoalSheet engine="antigravity" title="新目标" onClose={onClose} footer={<>
    <button type="button" className="goal-cancel" onClick={onClose}>取消</button>
    <button type="button" className="goal-primary" disabled={disabled || !description}
      onClick={() => {
        if (onSend(`/goal ${description}`)) onClose();
        else setFailed(true);
      }}>
      开始目标<Icon name="arrow-right" size={16} />
    </button>
  </>}>
    <GoalObjective value={objective} onChange={value => {
      setObjective(value);
      setFailed(false);
    }} hint="写清目标和完成条件，Gemini 会持续检查。" />
    {failed && <p className="goal-error" role="alert">目标未发送，请重试。</p>}
  </GoalSheet>;
}
