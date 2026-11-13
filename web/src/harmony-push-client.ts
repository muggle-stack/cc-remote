import type { RemoteNotificationMode } from "./notification-mode";
import type { PushBindingResult } from "./push";
import { bridge, publish } from "./harmony-push.ts";
let cachedToken = "";
let cleanupPending = false;
const cleanupError = "推送订阅清理尚未确认，可能仍收到通知；请联网后重新打开应用重试。";
async function request(path: string, body?: object): Promise<Response> {
  return fetch(`/api/push/harmony/${path}`, {
    credentials: "same-origin", cache: "no-store", signal: AbortSignal.timeout(12000),
    ...(body ? { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body) } : {}),
  });
}
async function checkConfig(): Promise<void> {
  const response = await request("config");
  if (!response.ok || (await response.json()).enabled !== true) {
    throw new Error("服务端尚未配置鸿蒙推送，请先完成 Push Kit 配置。");
  }
}
function errorMessage(error: unknown): string {
  const message = error instanceof Error ? error.message : "";
  // Never surface provider exception objects/tokens from the native bridge.
  if (message === "push_not_configured") return "当前安装包尚未开通 Push Kit，请更新推送签名后安装。";
  if (message === "permission_denied") return "通知权限未开启，请在系统设置中允许 cc-remote 通知。";
  if (message.startsWith("服务端尚未配置")) return message;
  return "鸿蒙推送连接失败，请检查网络后重试。";
}
export async function requestHarmonyPermission(): Promise<boolean> {
  try {
    await checkConfig();
    const result = await bridge()?.request("enable");
    if (!result?.enabled) throw new Error("permission_denied");
    publish({ error: "" });
    return true;
  } catch (error) {
    publish({ error: errorMessage(error) });
    return false;
  }
}
export async function enableHarmonyPush(machineIds: string[], mode: RemoteNotificationMode): Promise<PushBindingResult> {
  // Do not issue another token/binding until an uncertain prior attempt is
  // revoked. The native shell also persists failed token deletion across reloads.
  let revoked = false;
  if (cleanupPending) {
    if (!await disableHarmonyPush()) {
      publish({ error: cleanupError });
      return false;
    }
    revoked = true;
  }
  try {
    await checkConfig();
    const result = await bridge()?.request("token");
    if (!result?.token) throw new Error("push_not_configured");
    // Re-read on every foreground/rebind so a provider token rotation is repaired.
    const previous = cachedToken;
    cachedToken = result.token;
    // A rejected fetch is not evidence that the Relay did not commit the POST.
    cleanupPending = true;
    const response = await request("subscribe", {
      machine_id: machineIds[0], machine_ids: machineIds,
      notification_mode: mode, token: cachedToken,
    });
    if (!response.ok) throw new Error("subscribe_failed");
    cleanupPending = false;
    if (previous && previous !== cachedToken) {
      await request("unsubscribe", { token: previous }).catch(() => {});
    }
    publish({ error: "" });
    return true;
  } catch (error) {
    if (cleanupPending) {
      if (!await disableHarmonyPush()) {
        publish({ error: cleanupError });
        return false;
      }
      revoked = true;
    }
    publish({ error: errorMessage(error) });
    return revoked ? null : false;
  }
}
export async function disableHarmonyPush(): Promise<boolean> {
  try {
    cleanupPending = true;
    const native = bridge();
    if (!native) throw new Error("unavailable");
    const removal = cachedToken
      ? request("unsubscribe", { token: cachedToken }).catch(() => {})
      : Promise.resolve();
    // Invalidate independently of a slow/unreachable Relay. This also prevents a
    // late subscribe commit from reviving a binding after unsubscribe completed.
    await Promise.all([removal, native.request("delete")]);
    cachedToken = "";
    cleanupPending = false;
    publish({ error: "" });
    return true;
  } catch (error) {
    publish({ error: errorMessage(error) });
    return false;
  }
}
