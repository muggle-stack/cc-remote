import { useCallback, useEffect, useRef, useState, type RefObject } from "react";
import type { AntigravityQuota, ServerEvent } from "./protocol";
import type { RelayWs } from "./ws";

export type AntigravityQuotaReading = Pick<AntigravityQuota, "groups" | "plan" | "observed_at" | "error">;

export interface AntigravityQuotaView {
  reading: AntigravityQuotaReading | null;
  loading: boolean;
}

const EMPTY_VIEW: AntigravityQuotaView = { reading: null, loading: false };
const REFRESH_MS = 60_000;
const REPLY_TIMEOUT_MS = 25_000;

interface PendingRead {
  socket: RelayWs;
  requestId: string;
  timer: number;
}

export function useAntigravityQuota(machineId: string, wsRef: RefObject<RelayWs | null>) {
  const [view, setView] = useState<(AntigravityQuotaView & { machineId: string }) | null>(null);
  const pending = useRef<PendingRead | null>(null);
  const lastRead = useRef<{ socket: RelayWs; time: number } | null>(null);

  const cancel = useCallback(() => {
    if (pending.current) window.clearTimeout(pending.current.timer);
    pending.current = null;
    lastRead.current = null;
  }, []);

  useEffect(() => cancel, [cancel, machineId]);

  const reset = useCallback(() => {
    cancel();
    setView(null);
  }, [cancel]);

  const settle = useCallback((reading: AntigravityQuotaReading | null) => {
    if (pending.current) window.clearTimeout(pending.current.timer);
    pending.current = null;
    setView(current => {
      const previous = current?.machineId === machineId ? current.reading : null;
      return {
        machineId, loading: false,
        reading: reading ?? { groups: [], ...previous, error: "refresh_failed" },
      };
    });
  }, [machineId]);

  const request = useCallback((socket = wsRef.current) => {
    if (!socket || socket !== wsRef.current || pending.current?.socket === socket) return;
    const now = Date.now();
    if (lastRead.current?.socket === socket && now - lastRead.current.time < REFRESH_MS) return;
    const requestId = socket.sendGetAntigravityQuota();
    if (!requestId) return;
    if (pending.current) window.clearTimeout(pending.current.timer);
    pending.current = {
      socket, requestId, timer: window.setTimeout(() => {
        if (pending.current?.socket === socket && pending.current.requestId === requestId) {
          settle(null);
        }
      }, REPLY_TIMEOUT_MS),
    };
    lastRead.current = { socket, time: now };
    setView(current => ({
      machineId, loading: true, reading: current?.machineId === machineId ? current.reading : null,
    }));
  }, [machineId, settle, wsRef]);

  const accept = useCallback((socket: RelayWs, msg: ServerEvent) => {
    if (msg.type !== "antigravity_quota" && msg.type !== "error") return false;
    const read = pending.current;
    if (read?.socket !== socket || msg.request_id !== read.requestId) return msg.type === "antigravity_quota";
    settle(msg.type === "antigravity_quota" ? msg : null);
    return true;
  }, [settle]);

  return { view: view?.machineId === machineId ? view : EMPTY_VIEW, request, reset, accept };
}
