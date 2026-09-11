import type { ErrorMsg } from "./protocol";

const OWNERSHIP_GUIDANCE = /(本机终端|原生 .*CLI|Codex App|Claude TUI)/;
const INCOMPLETE_CLAUDE_SESSION =
  "Claude 会话历史不完整，无法恢复；可从会话菜单删除该条目。";
const PROVIDER_AUTH_TURN_FAILURE =
  "模型服务认证已失效或当前账号无权限，请检查当前服务的凭据或账号权限后重试。";
const LEGACY_CODEX_AUTH_TURN_FAILURE =
  "Codex 登录已失效或当前账号无权限，请重新登录后重试。";
const CODEX_UPDATE_INTERRUPTION =
  "Codex 自动更新时连接中断，本轮未确认完成。请检查已有结果后继续。";
const CODEX_CONNECTION_INTERRUPTION =
  "与 Codex 的连接中断，本轮未确认完成。请检查已有结果后继续。";
const CODEX_USAGE_LIMIT_FAILURE =
  "本轮使用的 Codex 账号额度已用完。可切换账号、补充额度，或等待恢复后重试。";
const CODEX_USAGE_LIMIT_RETRY =
  /^官方提示可于 ((?!0000)[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2})（设备当地时间）重试。$/;
const CODEX_TRANSPORT_MESSAGES = new Map([
  ["Codex 已自动更新，当前回合在更新时中断；为避免重复执行工具，"
    + "本次任务未自动重试。请确认已有结果后重新发送。", CODEX_UPDATE_INTERRUPTION],
  ["Codex 共享通道意外断开；为避免重复执行工具，本次任务未自动重试。"
    + "请确认已有结果后重新发送。", CODEX_CONNECTION_INTERRUPTION],
]);
const SAFE_TURN_FAILURE_MESSAGES = new Set([
  // Keep the former network copy safe for replayed rows from an older wrapper.
  "网络异常，连接失败，请重新尝试。",
  "网络连接异常，请检查网络后重试。",
  PROVIDER_AUTH_TURN_FAILURE,
  "请求过于频繁或当前额度受限，请稍后重试。",
  "请求超时，请重新尝试。",
  "Codex 上游服务暂时不可用，请稍后重试。",
  "当前模型繁忙，请稍后重试或切换模型。",
  CODEX_UPDATE_INTERRUPTION,
  CODEX_CONNECTION_INTERRUPTION,
  "上游模型因安全策略拒绝了本次请求（cyber_policy）。"
    + "这不是本地权限或网络错误；请核实并说明任务背景与授权范围，"
    + "若属误判请向服务提供方反馈。",
]);

function isUsageLimitFailure(message: string): boolean {
  if (message === CODEX_USAGE_LIMIT_FAILURE) return true;
  if (!message.startsWith(CODEX_USAGE_LIMIT_FAILURE)) return false;
  const match = CODEX_USAGE_LIMIT_RETRY.exec(message.slice(CODEX_USAGE_LIMIT_FAILURE.length));
  if (!match) return false;
  // Validate without browser-local timezone conversion (including DST gaps).
  const iso = match[1].replace(" ", "T");
  const date = new Date(iso + "Z");
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 16) === iso;
}

function safeTurnFailureMessage(message: string): string | null {
  const trimmed = message.trim();
  if (trimmed === LEGACY_CODEX_AUTH_TURN_FAILURE) {
    return PROVIDER_AUTH_TURN_FAILURE;
  }
  const transportMessage = CODEX_TRANSPORT_MESSAGES.get(trimmed);
  if (transportMessage) return transportMessage;
  return SAFE_TURN_FAILURE_MESSAGES.has(trimmed) || isUsageLimitFailure(trimmed) ? trimmed : null;
}

/** Only reviewed, explicit transport causes may override an interruption label. */
export function codexTransportInterruption(message?: string): "update" | "connection" | null {
  const safe = message ? safeTurnFailureMessage(message) : null;
  if (safe === CODEX_UPDATE_INTERRUPTION) return "update";
  if (safe === CODEX_CONNECTION_INTERRUPTION) return "connection";
  return null;
}

export function presentTurnOutcome(
  outcome: "failed" | "interrupted", message?: string,
): string {
  const cause = codexTransportInterruption(message);
  if (cause === "update") return "Codex 自动升级，本轮中断";
  if (cause === "connection") return "连接中断，回复未完成";
  if (outcome === "failed" && message && isUsageLimitFailure(message.trim())) return "账号额度已用完";
  return outcome === "interrupted" ? "已打断" : "回复未完成";
}

function ownershipMessage(message: string): string | null {
  if (!OWNERSHIP_GUIDANCE.test(message)) return null;
  // Ownership rejections are deliberately authored user copy.  Still remove
  // protocol/implementation nouns that should never become product UI.
  return message
    .replace(/\b(?:error|exception|traceback|crash)\b/gi, "问题")
    .replace(/\bcc_[a-z_]+\b/gi, "")
    .trim();
}

export function presentTurnProblem(error: Pick<ErrorMsg, "code" | "message">): string {
  if (error.code === "busy") {
    return ownershipMessage(error.message)
      ?? "本次消息未发送，会话当前不可写，请稍后重试。";
  }
  if (error.code === "not_running") {
    return "本次消息未发送，会话暂时不可用，请重新进入后重试。";
  }
  if (error.code === "bad_prompt") {
    return "消息内容无法发送，请检查输入或附件后重试。";
  }
  if (error.code === "drain_timeout") {
    return "停止操作未及时完成，会话正在恢复。";
  }
  if (error.code === "cc_crash") {
    return safeTurnFailureMessage(error.message)
      ?? "本次回复未完成，请重试。";
  }
  return "本次回复未完成，请重试。";
}

export function presentCommandProblem(
  error: Pick<ErrorMsg, "code" | "message">,
): string {
  switch (error.code) {
    case "dsh_invalid_session":
      return "DSH 会话尚未选定或已失效，请重新选择会话。";
    case "dsh_session/not-found":
      return "DSH 会话不存在，请刷新会话列表。";
    case "dsh_not_paired":
    case "dsh_auth_required":
    case "dsh_auth_expired":
    case "dsh_invalid_connection":
      return "DSH 本机配对不可用，请重新配对。";
    case "dsh_disconnected":
    case "dsh_unavailable":
    case "dsh_closed":
      return "无法连接本机 DSH，请检查服务后重试。";
    case "dsh_history_bridge_required":
      return "DSH 尚未加载 cc-remote 的只读历史插件。";
    case "dsh_unsupported":
      return "DSH 暂不支持此操作。";
    case "dsh_gateway/not-found":
      return "DSH 接口不兼容，请检查 DSH 版本。";
    case "wrapper_offline":
      return "设备正在重新连接…";
    case "invalid_cwd":
      return "所选目录不存在或不可用，请重新选择。";
    case "busy":
      return ownershipMessage(error.message)
        ?? "当前操作暂时无法执行，请稍后重试。";
    case "not_running":
      if (error.message === INCOMPLETE_CLAUDE_SESSION) {
        return INCOMPLETE_CLAUDE_SESSION;
      }
      return "当前会话暂时不可用，请重新进入后重试。";
    case "bad_prompt":
      return "输入内容无效，请检查后重试。";
    case "auth":
      return "当前操作不适用于这个会话。";
    case "protocol":
      return "页面版本已更新，请刷新后重试。";
    case "fork_reconciling":
      return "正在确认派生结果，请稍候…";
    case "steer_outcome_unknown":
      return "引导已发出，Codex 尚未确认是否生效。请先查看后续结果。";
    default:
      return "操作未完成，请稍后重试。";
  }
}

export function presentHistoricalTurnProblem(message: string, continuing = false): string {
  if (continuing && message.trim() === "当前模型繁忙，请稍后重试或切换模型。") {
    return "当前模型繁忙，这次请求未完成。";
  }
  const normalized = message.trim().toLowerCase();
  if (!normalized || normalized === "error") return "该轮未正常结束";
  return safeTurnFailureMessage(message) ?? "该轮未正常结束";
}
