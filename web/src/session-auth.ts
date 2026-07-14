export type SessionAuthResult = "authenticated" | "unauthorized" | "unavailable";

export interface SessionResponse {
  readonly ok: boolean;
  readonly status: number;
}

export type SessionFetch = (
  input: string,
  init: {
    credentials: "same-origin";
    cache: "no-store";
    signal: AbortSignal;
  },
) => Promise<SessionResponse>;

const SESSION_CHECK_TIMEOUT_MS = 5_000;
const LEGACY_AUTH_KEYS = ["cc_remote_session", "cc_remote_authenticated"] as const;

/**
 * HttpOnly cookies are deliberately opaque to JavaScript.  Probe the relay's
 * session endpoint so both page startup and an opaque WebSocket handshake
 * failure use the same source of truth.
 *
 * Only an explicit 401 means "log in again".  Other HTTP failures, network
 * errors and timeouts are transient: reconnecting must remain possible while
 * the relay or network is down.
 */
export async function probeSession(
  fetchSession: SessionFetch = (input, init) => fetch(input, init),
  timeoutMs = SESSION_CHECK_TIMEOUT_MS,
): Promise<SessionAuthResult> {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(new Error("session check timed out"));
    }, timeoutMs);
  });

  try {
    const response = await Promise.race([
      fetchSession("/api/session", {
        credentials: "same-origin",
        cache: "no-store",
        signal: controller.signal,
      }),
      timeout,
    ]);
    if (response.status === 401) return "unauthorized";
    return response.ok ? "authenticated" : "unavailable";
  } catch {
    return "unavailable";
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

export function shouldReconnectAfterSessionProbe(result: SessionAuthResult): boolean {
  return result !== "unauthorized";
}

export function clearLegacyAuthMarkers(
  storage: Pick<Storage, "removeItem">,
): void {
  for (const key of LEGACY_AUTH_KEYS) storage.removeItem(key);
}
