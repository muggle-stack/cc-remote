import type { Notice } from "./protocol";

const OFFICIAL_NOTICE_PREFIX = "codex-notice-";

function cleanProductText(value: string): string {
  return value
    .replace(/\bcc[_ -]?crash\b/gi, "运行问题")
    .replace(/\b(?:crash|warning|error|exception|traceback)\b/gi, "问题")
    .replace(/see (?:the )?wrapper logs?/gi, "请稍后重试")
    .trim();
}

function isOfficialNotice(notice: Notice): boolean {
  return notice.notice_id.startsWith(OFFICIAL_NOTICE_PREFIX);
}

/** Notices that belong in the conversation surface.
 *
 * Official app-server diagnostics are retained in session state for the Codex
 * status sheet, but never interrupt the transcript. Routine compaction receipts
 * already have inline progress. Other local outcomes use product copy.
 */
export function conversationNotices(notices: Notice[]): Notice[] {
  return notices.filter((notice) => !isOfficialNotice(notice)
    && !(notice.notice_id.startsWith("compact-") && notice.severity === "info")).map((notice) => {
    if (notice.notice_id.startsWith("schedule-")) {
      if (notice.severity === "warning") {
        const retrying = notice.title.includes("重试");
        return {
          ...notice,
          title: retrying ? "定时任务稍后重试" : "定时任务未完成",
          message: retrying
            ? "任务暂未完成，系统会按计划再次尝试。"
            : "可在任务列表中检查状态后重试。",
          detail: null,
        };
      }
      return {
        ...notice,
        title: "定时任务已完成",
        message: cleanProductText(notice.message),
        detail: null,
      };
    }
    if (notice.notice_id.startsWith("checkpoint-")) {
      return {
        ...notice,
        title: "本轮仅支持对话回滚",
        message: "本轮文件状态无法安全记录；对话仍可正常回滚。",
        detail: null,
      };
    }
    if (notice.notice_id.startsWith("rollback-")) {
      if (notice.severity === "warning") {
        return {
          ...notice,
          title: "回滚未完全完成",
          message: "部分内容未恢复，请检查冲突后重试。",
          detail: notice.detail ? cleanProductText(notice.detail) : null,
        };
      }
      return {
        ...notice,
        title: "回滚完成",
        message: cleanProductText(notice.message),
        detail: notice.detail ? cleanProductText(notice.detail) : null,
      };
    }
    if (notice.notice_id.startsWith("compact-")) {
      return {
        ...notice,
        title: cleanProductText(notice.title),
        message: cleanProductText(notice.message),
        detail: null,
      };
    }
    return notice.severity === "warning" ? {
      ...notice,
      title: "操作需要处理",
      message: "这项操作未完成，请稍后重试。",
      detail: null,
    } : {
      ...notice,
      title: cleanProductText(notice.title),
      message: cleanProductText(notice.message),
      detail: null,
    };
  });
}

/** Safe, non-interrupting summaries for diagnostics retained by the status UI. */
export function statusNotices(notices: Notice[]): Notice[] {
  return notices.filter((notice) => isOfficialNotice(notice)
    && notice.category !== "rate_limit").map((notice) => {
    const copy = {
      runtime: ["运行状态", "Codex 报告了一项运行状态；如操作受影响，请在本机 Codex 中检查。"],
      guardian: ["安全守护", "Codex 的安全守护需要关注，请在本机 Codex 中检查。"],
      config: ["配置状态", "当前 Codex 配置需要调整，请在本机 Codex 中查看。"],
      deprecation: ["兼容性状态", "当前 Codex 配置中有即将变更的选项，请在本机 Codex 中查看。"],
      security: ["目录权限", "Codex 检测到目录权限需要检查，请在本机 Codex 中查看。"],
      rate_limit: ["使用限额", "当前使用限额需要关注。"],
    }[notice.category];
    return {
      ...notice,
      title: copy[0],
      message: copy[1],
      detail: null,
    };
  });
}
