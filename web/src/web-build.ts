import { PROTOCOL_VERSION } from "./protocol.ts";

export interface WebBuild {
  build_id: string;
  version: string;
  protocol: number;
}

declare const __CC_REMOTE_WEB_BUILD__: WebBuild;
export const WEB_BUILD: WebBuild = typeof __CC_REMOTE_WEB_BUILD__ === "undefined"
  ? { build_id: "development", version: "development", protocol: PROTOCOL_VERSION }
  : __CC_REMOTE_WEB_BUILD__;

export function parseWebBuild(value: unknown): WebBuild | null {
  if (!value || typeof value !== "object") return null;
  const build = value as Partial<WebBuild>;
  return typeof build.build_id === "string" && /^[a-f0-9]{64}$/.test(build.build_id)
    && typeof build.version === "string" && /^\d+\.\d+\.\d+(?:[-+][\w.-]+)?$/.test(build.version)
    && Number.isSafeInteger(build.protocol) && build.protocol! > 0
    ? build as WebBuild : null;
}

/** A partially activated deployment is not ready to replace the loaded page. */
export async function availableWebBuild(
  fetcher: typeof fetch = fetch, current: WebBuild = WEB_BUILD,
): Promise<WebBuild | null> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 5_000);
  try {
    const stamp = Date.now();
    const options = { cache: "no-store", credentials: "same-origin", signal: controller.signal } as const;
    const [web, relay] = await Promise.all([
      fetcher(`/cc-remote-build.json?build_probe=${stamp}`, options),
      fetcher(`/healthz?build_probe=${stamp}`, options),
    ]);
    if (!web.ok || !relay.ok) return null;
    const build = parseWebBuild(await web.json());
    const health = await relay.json() as { version?: unknown; protocol?: unknown } | null;
    return build && health && build.build_id !== current.build_id
      && build.protocol === health.protocol && build.version === health.version ? build : null;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

/** Check both before and after I/O; a draft can appear while the probe runs. */
export async function applyWebUpdate(
  blocked: () => string | null,
  reload: () => void,
  probe: () => Promise<WebBuild | null> = availableWebBuild,
): Promise<string | null> {
  const reason = blocked();
  if (reason) return reason;
  const build = await probe();
  const nextReason = blocked();
  if (nextReason) return nextReason;
  if (!build) return "新版暂时不可用，请稍后重试。";
  reload();
  return null;
}
