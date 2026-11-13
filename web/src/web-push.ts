import type { RemoteNotificationMode } from "./notification-mode";
type PushConfig = { enabled: boolean; public_key: string };

function applicationServerKey(value: string): Uint8Array<ArrayBuffer> {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/")
    + "=".repeat((4 - value.length % 4) % 4);
  const raw = atob(padded);
  return Uint8Array.from(raw, (character) => character.charCodeAt(0));
}

async function config(): Promise<PushConfig | null> {
  try {
    const response = await fetch("/api/push-config", {
      credentials: "same-origin", cache: "no-store",
    });
    if (!response.ok) return null;
    const payload = await response.json() as Partial<PushConfig>;
    return {
      enabled: payload.enabled === true,
      public_key: typeof payload.public_key === "string" ? payload.public_key : "",
    };
  } catch {
    return null;
  }
}

export async function enableRemotePush(
  machineIds: string[],
  mode: RemoteNotificationMode = "generic",
): Promise<boolean> {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) return false;
  const pushConfig = await config();
  if (!pushConfig?.enabled || !pushConfig.public_key) return false;
  try {
    const registration = await navigator.serviceWorker.ready;
    let subscription = await registration.pushManager.getSubscription();
    subscription ??= await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: applicationServerKey(pushConfig.public_key),
    });
    const serialized = subscription.toJSON();
    if (!serialized.endpoint || !serialized.keys?.p256dh || !serialized.keys.auth) {
      return false;
    }
    const response = await fetch("/api/push/subscribe", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        machine_id: machineIds[0],
        machine_ids: machineIds,
        notification_mode: mode,
        endpoint: serialized.endpoint,
        keys: serialized.keys,
      }),
    });
    return response.ok;
  } catch {
    return false;
  }
}

export async function disableRemotePush(): Promise<boolean> {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) return true;
  let subscription: PushSubscription | null;
  try {
    const registration = await navigator.serviceWorker.ready;
    subscription = await registration.pushManager.getSubscription();
  } catch {
    return false;
  }
  if (!subscription) return true;
  let relayRemoved = false;
  let browserRemoved = false;
  try {
    const response = await fetch("/api/push/unsubscribe", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ endpoint: subscription.endpoint }),
    });
    relayRemoved = response.ok;
  } catch {
    // Still invalidate the browser endpoint below. The push service will then
    // return 404/410 to a stale relay row, which prunes it durably.
  }
  try {
    browserRemoved = await subscription.unsubscribe();
  } catch {
    // A successful relay removal is already sufficient to stop delivery.
  }
  return relayRemoved || browserRemoved;
}
