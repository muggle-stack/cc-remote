/** Maintenance is a device state, not a failed login or an ordinary busy turn. */
export const UPDATE_MAINTENANCE_MESSAGE = "设备正在更新，完成后会自动恢复会话。";
export function isUpdateMaintenance(error: { code: string; message: string }): boolean {
  return error.code === "update_in_progress"
    || (error.code === "busy" && error.message === "设备正在安装更新，请在重新连接后继续。");
}
export function allowedDuringUpdate(type: string, operation?: string): boolean {
  return (type === "collaboration" && ["list", "read", "catalog", "sessions", "reconcile", "stop", "answer"].includes(operation ?? ""))
    || /^(get_|list_|read_|browse_)/.test(type)
    || ["hello", "ping", "interrupt", "cancel_queued_query", "answer_question",
      "acknowledge_completion"].includes(type);
}
