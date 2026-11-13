import type { RemoteNotificationMode } from "./notification-mode";
import type { PushBindingResult } from "./push";

// Installed by the signed shell in the configured top-level document only.
// Ordinary Web/PWA clients never enter this channel or call its endpoints.
interface HarmonyBridge {
  request(method: "status" | "enable" | "token" | "delete"): Promise<{
    enabled?: boolean; token?: string;
  }>;
}
declare global {
  interface Window {
    ccRemoteHarmonyPush?: HarmonyBridge;
    ccRemoteHarmonyNotification?: unknown;
  }
}
export interface HarmonyPushState {
  available: boolean;
  granted: boolean;
  revision: number;
  error: string;
}
let state: HarmonyPushState = { available: false, granted: false, revision: 0, error: "" };
const listeners = new Set<() => void>();

export function bridge(): HarmonyBridge | undefined {
  return typeof window !== "undefined" && window.top === window
    ? window.ccRemoteHarmonyPush : undefined;
}
export function hasHarmonyPush(): boolean { return !!bridge(); }
export function harmonyPushSnapshot(): HarmonyPushState { return state; }
export function publish(update: Partial<HarmonyPushState>): void {
  state = { ...state, ...update };
  for (const listener of listeners) listener();
}
export function subscribeHarmonyPush(listener: () => void): () => void {
  listeners.add(listener);
  listener();
  return () => { listeners.delete(listener); };
}

async function refresh(): Promise<void> {
  const native = bridge();
  if (!native) return;
  try {
    const result = await native.request("status");
    publish({ available: true, granted: result.enabled === true, revision: state.revision + 1 });
  } catch {
    publish({ available: true, granted: false, error: "无法读取鸿蒙通知权限，请重新打开应用。" });
  }
}

export function confirmHarmonyPermission(): void {
  publish({ available: true, granted: true, error: "" });
}
function unavailable(): false {
  // Chunk loading can fail before the client handles provider/network errors.
  publish({ error: "鸿蒙通知暂不可用，请检查网络或刷新后重试。" });
  return false;
}
export async function requestHarmonyPermission(): Promise<boolean> {
  try {
    return await (await import("./harmony-push-client.ts")).requestHarmonyPermission();
  } catch {
    return unavailable();
  }
}
export async function enableHarmonyPush(machineIds: string[], mode: RemoteNotificationMode): Promise<PushBindingResult> {
  try {
    return await (await import("./harmony-push-client.ts")).enableHarmonyPush(machineIds, mode);
  } catch {
    return unavailable();
  }
}
export async function disableHarmonyPush(): Promise<boolean> {
  try {
    return await (await import("./harmony-push-client.ts")).disableHarmonyPush();
  } catch {
    return unavailable();
  }
}

if (typeof window !== "undefined") {
  window.addEventListener("cc-remote-harmony-ready", () => { void refresh(); });
  window.addEventListener("cc-remote-harmony-refresh", () => { void refresh(); });
  if (hasHarmonyPush()) void refresh();
}
