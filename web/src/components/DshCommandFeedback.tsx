import { useState } from "react";
import type { DshState, DshCommandResult } from "../protocol";
import { Icon } from "../icons";

export default function DshCommandFeedback({ scope, state, result }: {
  scope: string; state?: DshState; result?: DshCommandResult;
}) {
  const [dismissed, setDismissed] = useState<string[]>([]);
  const key = `${scope}:${result?.request_id ?? ""}`;
  return <>
    {result?.text && !dismissed.includes(key) && <div className={`dsh-command-result ${result.status}`} role="status">
      <Icon name={result.status === "success" ? "term" : "info"} size={16} />
      <p>{result.status === "unknown" && "结果尚未确认，请先检查状态。\n"}{result.text}</p>
      <button type="button" aria-label="关闭命令提示" onClick={() => setDismissed(current => [...current.slice(-63), key])}><Icon name="close" size={15} /></button>
    </div>}
    {(state?.plan_active || state?.plan_pending) && <div className="dsh-plan-state" role="status" aria-label="DSH 计划状态">
      <Icon name="plan" size={14} />
      {state.plan_pending ? state.plan_active ? "退出计划待生效" : "进入计划待生效" : "计划模式"}
    </div>}
  </>;
}
