import type { Notice } from "./protocol";
import type { ConnState } from "./ws";
import { cleanProductText, conversationNotices } from "./notice-presentation";

export const HEADER_SUCCESS_TTL_MS = 6_000;
export type ConnectionPhase = "connecting" | "reconnecting" | "disconnected" | "offline" | "syncing";
export type FeedbackTone = "progress" | "warning" | "success" | "info";

/** All global command banners share the status entry; details remain readable. */
export function headerBannerKind(banner?: string): "connection" | "failure" | null {
  if (!banner) return null;
  if (["正在连接…", "正在重新连接…", "设备正在重新连接…",
    "设备离线，正在等待重新连接…", "machine offline — waiting for reconnect",
    "machine reconnected — syncing…"].includes(banner)) return "connection";
  return "failure";
}

export function headerConnectionPhase(
  connState: ConnState, wrapperOnline: boolean, syncing: boolean, banner?: string,
  wrapperConnection?: "offline" | "syncing",
): ConnectionPhase | null {
  if (connState === "connecting") return "connecting";
  if (connState === "disconnected") return "disconnected";
  if (connState === "reconnecting") return "reconnecting";
  if (!wrapperOnline) return wrapperConnection
    ?? (banner === "machine offline — waiting for reconnect"
      || banner === "设备离线，正在等待重新连接…" ? "offline" : "syncing");
  return syncing ? "syncing" : null;
}

export function isHeaderNotice(notice: Notice): boolean {
  // Keep native diagnostics in their dedicated status sheet and routine
  // compaction receipts inline, exactly as before.
  return conversationNotices([notice]).length > 0;
}

export interface HeaderFeedbackItem {
  id: string;
  label: string;
  tone: FeedbackTone;
  message: string;
  detail?: string | null;
  noticeId?: string;
  banner?: string;
  bannerRevision?: number;
}

export function headerNoticeItem(notice: Notice, scope: string): HeaderFeedbackItem {
  const presented = conversationNotices([notice])[0];
  const info = notice.severity === "info";
  const rollback = notice.notice_id.startsWith("rollback-");
  const schedule = notice.notice_id.startsWith("schedule-");
  const preserveRuntimeCopy = notice.category === "runtime" && !rollback
    && notice.severity === "warning" && !/^(schedule-|compact-)/.test(notice.notice_id);
  const success = info && (rollback || schedule);
  const label = notice.notice_id.startsWith("checkpoint-") ? "回滚受限"
    : rollback ? info ? "已撤销" : "操作未完成"
    : schedule ? info ? "任务已完成" : "任务未完成"
    : notice.notice_id.startsWith("compact-") ? "压缩未完成"
    : ({ runtime: "操作未完成", guardian: "安全提示", config: "配置提示",
      deprecation: "兼容提示", security: "安全提示", rate_limit: "使用限额" })[notice.category];
  return {
    id: JSON.stringify([scope, notice.notice_id]), noticeId: notice.notice_id,
    label: info && !success ? "会话提示" : label,
    tone: success ? "success" : info ? "info" : "warning",
    message: preserveRuntimeCopy ? cleanProductText(notice.message) || presented.message : presented.message,
    detail: preserveRuntimeCopy && notice.detail ? cleanProductText(notice.detail) : presented.detail,
  };
}

export interface ConnectionFeedback {
  scope: string;
  phase: ConnectionPhase | null;
  episode: number;
  interrupted: boolean;
  recovered: boolean;
  dismissed: boolean;
}

/** Dismissal follows the whole outage through reconnect and sync, not its text. */
export function advanceConnectionFeedback(
  previous: ConnectionFeedback | null, scope: string, phase: ConnectionPhase | null,
): ConnectionFeedback {
  if (!previous || previous.scope !== scope) return {
    scope, phase, episode: 0, interrupted: phase === "offline" || phase === "reconnecting" || phase === "disconnected",
    recovered: false, dismissed: false,
  };
  if (previous.phase === phase) return previous;
  if (phase === null) return {
    ...previous, phase, recovered: previous.interrupted && !previous.dismissed,
    interrupted: false,
  };
  return {
    ...previous, phase,
    episode: previous.episode + (previous.phase === null ? 1 : 0),
    dismissed: previous.phase === null ? false : previous.dismissed,
    recovered: false,
    interrupted: previous.interrupted || phase === "offline" || phase === "reconnecting" || phase === "disconnected",
  };
}

export function connectionFeedbackItem(state: ConnectionFeedback): HeaderFeedbackItem | null {
  if (state.dismissed || (!state.phase && !state.recovered)) return null;
  const [label, message] = state.phase === "connecting"
    ? ["连接中", "正在连接 cc-remote，连接完成后会自动同步会话。"]
    : state.phase === "disconnected"
      ? ["连接中断", "连接已中断，请检查页面提示后重新连接。"]
    : state.phase === "reconnecting"
      ? ["重连中", "连接暂时中断，正在自动重连。恢复后会继续同步会话。"]
      : state.phase === "offline"
        ? ["设备离线", "设备暂时离线，正在等待重新连接。设备恢复在线后会自动同步会话。"]
        : state.phase === "syncing"
          ? ["同步中", "正在同步会话状态和历史记录，完成后此提示会自动消失。"]
          : ["已重连", "连接已恢复，会话状态已同步。"];
  return {
    id: JSON.stringify([state.scope, "connection", state.episode]), label, message,
    tone: state.phase === "offline" || state.phase === "disconnected" ? "warning" : state.phase ? "progress" : "success",
  };
}
