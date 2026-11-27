import { useState } from "react";
import type { ToolBlock } from "../domain/conversation";
import type { SessionMessageTool } from "../session-message-receipts";
import { localSessionReference, relatedSessionTitle, type SessionMessageNavigation } from "../session-messages";
import { Icon } from "../icons";
import { RawToolData } from "./ToolDetails";
import "./session-message-tool.css";

export function SessionMessageToolStatus({ message, pending }: {
  message: SessionMessageTool; pending: boolean;
}) {
  const status = message.status === "sending" && !pending ? "unknown" : message.status;
  const label = { sending: "发送中", sent: "已发送", queued: "已排队", failed: "发送失败", unknown: "状态未确认" }[status];
  return <span className={`session-message-tool-status ${status}`} role="status">
    {status === "sending" ? <span className="tool-st run" />
      : <Icon name={status === "sent" ? "verify" : status === "failed" ? "close" : "info"} size={14} />}
    <span>{label}</span>
  </span>;
}

export function SessionMessageToolDetails({ block, message, sessionLink, onOpenSession }: {
  block: ToolBlock; message: SessionMessageTool;
} & SessionMessageNavigation) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const target = sessionLink?.(message.threadId);
  const title = target?.title ?? relatedSessionTitle(undefined, message.threadId);
  const local = localSessionReference(message.threadId);
  const account = target?.account
    ?? (local?.engine !== "antigravity" && local?.sid.includes("@") ? local.sid.split("@")[0] : undefined);
  const enabled = !!onOpenSession && !!target?.available;
  const output = block.result?.content || block.output;
  const copy = async () => {
    try {
      if (!navigator.clipboard) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(message.message ?? "");
      setCopyState("copied");
    } catch { setCopyState("failed"); }
  };
  return <div className="session-message-tool-details">
    <div className="session-message-tool-target">
      <Icon name="message" size={14} />
      <span>发送给</span>
      <button type="button" disabled={!enabled}
        title={enabled ? "进入会话" : "会话不可用或不在当前可见范围中"}
        aria-label={`进入会话 ${title}`} onClick={() => onOpenSession?.(message.threadId)}>
        <span>{title}</span>{enabled && <span aria-hidden="true">↗</span>}
      </button>
      {account && <span className="session-message-tool-account" title={`账号 ${account}`}>{account}</span>}
    </div>
    {message.message != null && <>
      <div className="tool-lbl">发送内容</div>
      <div className="session-message-tool-content">
        <button type="button" className="session-message-tool-copy"
          aria-label={copyState === "failed" ? "复制失败，重试复制发送内容" : "复制发送内容"}
          title={copyState === "copied" ? "已复制" : copyState === "failed" ? "复制失败，请重试" : "复制发送内容"}
          onClick={() => void copy()}>
          <Icon name={copyState === "copied" ? "check" : "copy"} size={14} />
        </button>
        <div>{message.message}</div>
      </div>
    </>}
    <RawToolData label="原始参数" value={block.input} />
    {output && <RawToolData label="原始输出" value={output + (block.result?.truncated ? "\n…（输出已截断）" : "")} />}
  </div>;
}
