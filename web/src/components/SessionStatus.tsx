import type { State } from "../protocol";

const labels: Record<State | "reconnecting", string> = {
  idle: "空闲", running: "运行", interrupting: "中断", draining: "收尾", reconnecting: "同步中",
};

export function SessionStatus({ state, awaitingConfirmation = false }: {
  state: State | "reconnecting";
  awaitingConfirmation?: boolean;
}) {
  const label = awaitingConfirmation && state !== "reconnecting" ? "待确认" : labels[state];
  return <span className={`hstat ${state}`} role="status" aria-label={`会话状态：${label}`}>
    <span className="sd" aria-hidden="true" /><span className="hstat-label">{label}</span>
  </span>;
}
