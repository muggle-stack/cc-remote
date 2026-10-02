import type { Turn } from "../domain/conversation";
import { outgoingSessionMessages } from "../session-messages";
import { Icon } from "../icons";
import "./session-messages.css";

export default function SessionMessageLinks({ turn, source, resolve, onOpen }: {
  turn: Turn;
  source?: boolean;
  resolve?: (nativeId: string) => { title: string; available: boolean };
  onOpen?: (nativeId: string) => void;
}) {
  const link = (id: string, label: string, key: string) => {
    const target = resolve?.(id);
    const enabled = !!onOpen && !!target?.available;
    return <button key={key} type="button"
      className={source ? "session-message-source" : "session-message-sent"}
      disabled={!enabled} title={enabled ? "查看会话" : "会话不可用或不在当前设备与账号中"}
      onClick={() => onOpen?.(id)}>
      <Icon name="message" size={13} />
      <span>{label} {target?.title ?? id.slice(0, 8)}</span>
      {enabled && <span aria-hidden="true">↗</span>}
    </button>;
  };
  if (source) return turn.sourceThreadId
    ? link(turn.sourceThreadId, "来自会话 ·", "source") : null;
  return outgoingSessionMessages(turn).map((receipt) => link(
    receipt.threadId,
    receipt.status === "sent" ? "已发送给" : receipt.status === "failed"
      ? "未能发送给" : turn.done ? "发送状态未确认 ·" : "正在发送给",
    receipt.itemId,
  ));
}
