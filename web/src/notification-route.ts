import type { Engine, Space } from "./protocol";

export interface NotificationRoute {
  machine_id: string;
  session_id: string;
  engine: Engine;
  space: Space;
}

export const NOTIFICATION_TARGET_KEY = "cc_remote_notification_target";
const ROUTE_KEYS = new Set(["machine_id", "session_id", "engine", "space"]);
const WIRE_ID = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$/;

function safeSessionId(value: unknown): value is string {
  return typeof value === "string" && WIRE_ID.test(value);
}

export function parseNotificationRoute(value: unknown): NotificationRoute | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const row = value as Record<string, unknown>;
  const keys = Object.keys(row);
  if (keys.length !== ROUTE_KEYS.size || keys.some((key) => !ROUTE_KEYS.has(key))) {
    return null;
  }
  if (typeof row.machine_id !== "string" || !WIRE_ID.test(row.machine_id)
      || !safeSessionId(row.session_id)
      || (row.engine !== "claude" && row.engine !== "codex" && row.engine !== "dsh")
      || (row.space !== "code" && row.space !== "work")) {
    return null;
  }
  return {
    machine_id: row.machine_id,
    session_id: row.session_id,
    engine: row.engine,
    space: row.space,
  };
}

export function encodeNotificationRoute(route: NotificationRoute): string {
  const safe = parseNotificationRoute(route);
  if (!safe) return "/";
  return `/#notification=${encodeURIComponent(JSON.stringify(safe))}`;
}

export function parseNotificationFragment(fragment: string): NotificationRoute | null {
  const prefix = "#notification=";
  if (!fragment.startsWith(prefix) || fragment.length > 8192) return null;
  try {
    return parseNotificationRoute(
      JSON.parse(decodeURIComponent(fragment.slice(prefix.length))),
    );
  } catch {
    return null;
  }
}

export function captureNotificationFragment(
  location: Pick<Location, "hash" | "pathname" | "search">,
  storage: Pick<Storage, "setItem">,
  browserHistory: Pick<History, "replaceState">,
): NotificationRoute | null {
  if (!location.hash.startsWith("#notification=")) return null;
  const target = parseNotificationFragment(location.hash);
  if (target) {
    try {
      storage.setItem(NOTIFICATION_TARGET_KEY, JSON.stringify(target));
    } catch {
      // The caller can still keep the returned target in memory when private
      // browsing or storage quota blocks sessionStorage.
    }
  }
  // Clear even malformed payloads immediately so secrets or forged routes do
  // not linger in screenshots, copied URLs, or subsequent login attempts.
  try {
    browserHistory.replaceState(
      null, "", `${location.pathname}${location.search}`);
  } catch {
    // Navigation remains usable even when an embedded browser blocks History.
  }
  return target;
}

export function consumeNotificationTarget(
  storage: Pick<Storage, "getItem" | "removeItem">,
): NotificationRoute | null {
  const raw = storage.getItem(NOTIFICATION_TARGET_KEY);
  storage.removeItem(NOTIFICATION_TARGET_KEY);
  if (!raw || raw.length > 8192) return null;
  try {
    return parseNotificationRoute(JSON.parse(raw));
  } catch {
    return null;
  }
}

export function storeNotificationTarget(
  storage: Pick<Storage, "setItem">,
  target: NotificationRoute,
): boolean {
  const safe = parseNotificationRoute(target);
  if (!safe) return false;
  storage.setItem(NOTIFICATION_TARGET_KEY, JSON.stringify(safe));
  return true;
}
