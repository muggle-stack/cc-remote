import type { State } from "../protocol";

const labels: Record<State | "reconnecting", string> = {
  idle: "空闲", running: "运行", interrupting: "中断", draining: "收尾", reconnecting: "同步中",
};

export function SessionStatus({ state }: { state: State | "reconnecting" }) {
  return <span className={`hstat ${state}`} role="status" aria-label={`会话状态：${labels[state]}`}>
    <span className="sd" aria-hidden="true" /><span className="hstat-label">{labels[state]}</span>
  </span>;
}
