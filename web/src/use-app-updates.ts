import { useCallback, useEffect, useRef, useState } from "react";
import { AppUpdateClient, parseAppUpdate, parseUpdateJob, parseUpdateReceipt, UpdateIntentStore, UPDATE_TERMINAL,
  type AppUpdateState, type UpdateIntent } from "./app-updates";

export function useAppUpdates(enabled: boolean, machine: string, open: boolean) {
  const [maintenance, setMaintenance] = useState(false);
  const [data, setData] = useState<AppUpdateState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [changing, setChanging] = useState(false);
  const [pending, setPending] = useState<UpdateIntent | null>(null);
  const [pendingStatus, setPendingStatus] = useState<"unconfirmed" | "missing" | "unsupported">("unconfirmed");
  const openRef = useRef(open); openRef.current = open;
  const actions = useRef({ check: async () => {}, start: async (_version: string) => {}, cancel: async () => {}, observeMaintenance: () => {} });
  useEffect(() => {
    setMaintenance(false); setData(null); setError(null); setChecking(false); setChanging(false); setPending(null);
    setPendingStatus("unconfirmed");
    if (!enabled || !machine) return;
    let active = true, probing = false, mutation = false, lastCheck = 0, guardRevision = 0;
    let probeChecksCatalog = false, recheck = false;
    let guardObserved = false;
    let mutationRevision = 0, lookupUnsupported = false;
    let snapshot: AppUpdateState | null = null;
    const client = new AppUpdateClient(machine);
    let storage: Storage | null = null;
    try { storage = window.localStorage; } catch { /* private storage unavailable */ }
    const ledger = new UpdateIntentStore(machine, storage);
    let intent = ledger.read(); setPending(intent);
    let operationError = intent?.reason ?? null;
    const failed = (reason: unknown) => {
      if (active) setError(reason instanceof Error ? reason.message : "network");
    };
    const clearIntent = () => {
      ledger.clear(); intent = null; operationError = null; setPending(null); setPendingStatus("unconfirmed");
    };
    const reconcile = (next: AppUpdateState) => {
      if (intent && next.job?.id === intent.id && next.job.version === intent.version) clearIntent();
      snapshot = next; setData(next);
    };
    const probe = async (check = false) => {
      if (!active) return;
      if (mutation) { recheck ||= check; return; }
      if (probing) {
        // Opening during a status poll still needs a catalog check. Coalesce
        // concurrent opens with an already-running catalog request.
        if (check && !probeChecksCatalog) { recheck = true; setChecking(true); }
        return;
      }
      probing = true; probeChecksCatalog = check;
      const observedRevision = guardRevision;
      const observedMutation = mutationRevision;
      if (check) setChecking(true);
      try {
        const raw = await client.call(check ? { op: "check" } : undefined);
        if (!active || observedMutation !== mutationRevision) return;
        reconcile(parseAppUpdate(raw));
        if (observedRevision === guardRevision) {
          guardObserved = snapshot?.maintenance === true;
          setMaintenance(guardObserved);
        }
        if (check) lastCheck = Date.now();
        if (intent && !lookupUnsupported) {
          const inspecting = intent;
          try {
            const rawReceipt = await client.call({ op: "lookup", job_id: inspecting.id });
            if (!active || observedMutation !== mutationRevision || intent?.id !== inspecting.id) return;
            const receipt = parseUpdateReceipt(rawReceipt, inspecting);
            if (receipt.status === "found") {
              clearIntent();
              // Do not replace a newer active update with an older terminal receipt.
              if (snapshot && (!snapshot.job || snapshot.job.id === receipt.job.id
                || (UPDATE_TERMINAL.has(snapshot.job.phase) && !UPDATE_TERMINAL.has(receipt.job.phase)))) {
                reconcile({ ...snapshot, job: receipt.job });
              }
            } else setPendingStatus("missing");
          } catch (reason) {
            if (!active || observedMutation !== mutationRevision) return;
            if (reason instanceof Error && ["invalid_request", "unsupported"].includes(reason.message)) {
              lookupUnsupported = true; setPendingStatus("unsupported");
              if (!operationError || operationError === "outcome_pending") operationError = "confirmation_unavailable";
            } else {
              setPendingStatus("unconfirmed");
              throw reason;
            }
          }
        }
        // A successful status poll is not an acknowledgement of a failed start.
        setError(operationError);
      } catch (reason) { if (observedMutation === mutationRevision) failed(reason); }
      finally {
        const checkNext = recheck; recheck = false;
        probing = false; probeChecksCatalog = false;
        if (active && check && !checkNext) setChecking(false);
        if (active && (checkNext || observedRevision !== guardRevision || observedMutation !== mutationRevision)) void probe(checkNext);
      }
    };
    const start = async (version: string) => {
      if (mutation || !active) return;
      mutation = true; mutationRevision++; operationError = null; setChanging(true); setError(null);
      // Persist BEFORE sending. An unknown acknowledgement keeps the same id
      // across page reloads; an explicit retry cannot launch a second OS job.
      intent ??= { id: crypto.randomUUID().replaceAll("-", ""), version };
      intent = { id: intent.id, version: intent.version };
      ledger.save(intent); setPending(intent);
      setPendingStatus(lookupUnsupported ? "unsupported" : "unconfirmed");
      const submitted = intent;
      try {
        const raw = await client.call({ op: "start", job_id: submitted.id, version: submitted.version });
        if (!active) return;
        const job = parseUpdateJob(raw.job);
        if (!job || job.id !== submitted.id || job.version !== submitted.version) throw new Error("outcome_pending");
        clearIntent();
        if (snapshot) reconcile({ ...snapshot, job });
      } catch (reason) {
        // These responses are produced before a durable launch is accepted.
        // Timeouts, disconnects and permission changes retain the original id.
        if (!active) return;
        if (reason instanceof Error && ["release_changed", "receipt_limit"].includes(reason.message)) {
          clearIntent();
        }
        operationError = reason instanceof Error ? reason.message : "network";
        if (intent) { intent = { ...intent, reason: operationError }; ledger.save(intent); setPending(intent); }
        failed(reason);
      }
      finally {
        mutation = false;
        if (active) { setChanging(false); void probe(recheck); }
      }
    };
    const cancel = async () => {
      const job = snapshot?.job;
      if (mutation || !active || !job) return;
      mutation = true; mutationRevision++; operationError = null; setChanging(true);
      try {
        const raw = await client.call({ op: "cancel", job_id: job.id });
        const next = parseUpdateJob(raw.job);
        if (!next || next.id !== job.id || next.version !== job.version) throw new Error("outcome_pending");
        if (active && snapshot) reconcile({ ...snapshot, job: next });
      } catch (reason) {
        if (active) operationError = reason instanceof Error ? reason.message : "network";
        failed(reason);
      }
      finally { mutation = false; if (active) { setChanging(false); void probe(recheck); } }
    };
    actions.current = { check: () => probe(true), start, cancel, observeMaintenance: () => {
      if (!active) return;
      guardRevision++; guardObserved = true; setMaintenance(true); void probe();
    } };
    void probe(true);
    const poll = window.setInterval(() => {
      const running = snapshot?.job && !UPDATE_TERMINAL.has(snapshot.job.phase);
      if (openRef.current || running || intent || guardObserved) void probe();
      else if (document.visibilityState === "visible" && Date.now() - lastCheck > 15 * 60_000) void probe(true);
    }, 5000);
    const focus = () => { if (openRef.current || intent || snapshot?.job) void probe(); };
    window.addEventListener("online", focus);
    window.addEventListener("focus", focus);
    return () => {
      active = false; client.close(); window.clearInterval(poll);
      window.removeEventListener("online", focus); window.removeEventListener("focus", focus);
      actions.current = { check: async () => {}, start: async () => {}, cancel: async () => {}, observeMaintenance: () => {} };
    };
  }, [enabled, machine]);
  const check = useCallback(() => actions.current.check(), []);
  const start = useCallback((version: string) => actions.current.start(version), []);
  const cancel = useCallback(() => actions.current.cancel(), []);
  useEffect(() => { if (open) void check(); }, [open, check, machine]);
  const observeMaintenance = useCallback(() => actions.current.observeMaintenance(), []);
  return { data, error, checking, changing, pending, pendingStatus, maintenance, observeMaintenance, check, start, cancel };
}
export type AppUpdatesController = ReturnType<typeof useAppUpdates>;
