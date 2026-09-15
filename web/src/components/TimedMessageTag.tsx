import type { TimedMessage } from "../protocol";
import { Icon } from "../icons";
import "./timed-task.css";

export function TimedMessageTag({ task }: { task: TimedMessage }) {
  return <details className="timed-message-tag">
    <summary><Icon name="clock" size={13} />定时任务<Icon name="chev" size={12} /></summary>
    <div><strong>{task.title}</strong><span>计划发送于 {new Date(task.scheduled_at * 1000).toLocaleString()}</span></div>
  </details>;
}
