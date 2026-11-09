import { useCallback, useEffect, useRef, useState } from "react";
import { AppUpdateClient, parseAppUpdate, parseUpdateJob, UpdateIntentStore, UPDATE_TERMINAL,
  type AppUpdateState, type UpdateIntent } from "./app-updates";

export function useAppUpdates(enabled: boolean, machine: string, open: boolean) {
  const [data, setData] = useState<AppUpdateState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [changing, setChanging] = useState(false);
  const [pending, setPending] = useState<UpdateIntent | null>(null);
  const openRef = useRef(open); openRef.current = open;
  const actions = useRef({ check: async () => {}, start: async (_version: string) => {}, cancel: async () => {} });
  useEffect(() => {
    setData(null); setError(null); setChecking(false); setChanging(false); setPending(null);
    if (!enabled || !machine) return;
    let active = true, probing = false, mutation = false, lastCheck = 0;
    let snapshot: AppUpdateState | null = null;
    const client = new AppUpdateClient(machine);
    let storage: Storage | null = null;
    try { storage = window.localStorage; } catch { /* private storage unavailable */ }
    const ledger = new UpdateIntentStore(machine, storage);
    let intent = ledger.read(); setPending(intent);
    const failed = (reason: unknown) => {
      if (active) setError(reason instanceof Error ? reason.message : "network");
    };
    const reconcile = (next: AppUpdateState) => {
      if (intent && next.job?.id === intent.id) {
        ledger.clear(); intent = null; setPending(null);
      }
      snapshot = next; setData(next);
    };
    const probe = async (check = false) => {
      if (probing || !active) return;
      probing = true;
      if (check) setChecking(true);
      try {
        const raw = await client.call(check ? { op: "check" } : undefined);
        if (!active) return;
        reconcile(parseAppUpdate(raw)); setError(null);
        if (check) lastCheck = Date.now();
      } catch (reason) { failed(reason); }
      finally { probing = false; if (active && check) setChecking(false); }
    };
    const start = async (version: string) => {
      if (mutation || !active) return;
      mutation = true; setChanging(true); setError(null);
      // Persist BEFORE sending. An unknown acknowledgement keeps the same id
      // across page reloads; an explicit retry cannot launch a second OS job.
      intent ??= { id: crypto.randomUUID().replaceAll("-", ""), version };
      ledger.save(intent); setPending(intent);
      try {
        const raw = await client.call({ op: "start", job_id: intent.id, version: intent.version });
        if (!active) return;
        const job = parseUpdateJob(raw.job);
        if (job && snapshot) reconcile({ ...snapshot, job });
        await probe();
      } catch (reason) {
        // These responses are produced before a durable launch is accepted.
        // Timeouts, disconnects and permission changes retain the original id.
        if (active && reason instanceof Error && ["release_changed", "receipt_limit"].includes(reason.message)) {
          ledger.clear(); intent = null; setPending(null);
        }
        failed(reason);
      }
      finally { mutation = false; if (active) setChanging(false); }
    };
    const cancel = async () => {
      const job = snapshot?.job;
      if (mutation || !active || !job) return;
      mutation = true; setChanging(true);
      try {
        const raw = await client.call({ op: "cancel", job_id: job.id });
        const next = parseUpdateJob(raw.job);
        if (!next) throw new Error("outcome_pending");
        if (active && snapshot) reconcile({ ...snapshot, job: next });
      } catch (reason) { failed(reason); }
      finally { mutation = false; if (active) setChanging(false); }
    };
    actions.current = { check: () => probe(true), start, cancel };
    void probe(true);
    const poll = window.setInterval(() => {
      const running = snapshot?.job && !UPDATE_TERMINAL.has(snapshot.job.phase);
      if (openRef.current || running || intent) void probe();
      else if (document.visibilityState === "visible" && Date.now() - lastCheck > 15 * 60_000) void probe(true);
    }, 5000);
    const focus = () => { if (openRef.current || intent || snapshot?.job) void probe(); };
    window.addEventListener("online", focus);
    window.addEventListener("focus", focus);
    return () => {
      active = false; client.close(); window.clearInterval(poll);
      window.removeEventListener("online", focus); window.removeEventListener("focus", focus);
      actions.current = { check: async () => {}, start: async () => {}, cancel: async () => {} };
    };
  }, [enabled, machine]);
  const check = useCallback(() => actions.current.check(), []);
  const start = useCallback((version: string) => actions.current.start(version), []);
  const cancel = useCallback(() => actions.current.cancel(), []);
  useEffect(() => { if (open) void check(); }, [open, check, machine]);
  return { data, error, checking, changing, pending, check, start, cancel };
}
export type AppUpdatesController = ReturnType<typeof useAppUpdates>;
