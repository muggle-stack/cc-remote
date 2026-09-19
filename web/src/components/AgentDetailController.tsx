import { useCallback, useEffect, useRef, useState } from "react";

import {
  acceptAgentDetail,
  emptyAgentRun,
  type AgentDetailPanelState,
} from "../agent-detail";
import type { AgentDetail, Engine, ProcessStatus } from "../protocol";
import type { RelayWs } from "../ws";
import { uuid } from "../util";
import { AgentDetailPanel } from "./AgentDetailPanel";
import { HISTORY_DETAIL_REQUEST_TIMEOUT_MS } from "../history-requests";

export interface AgentDetailSelection {
  sid: string;
  revision: string;
  runId: string;
  title: string;
  status?: ProcessStatus;
  engine?: Engine;
}

export function AgentDetailController({ selection, ws, onListen, onClose,
  onOpenFile }: {
  selection: AgentDetailSelection;
  ws: RelayWs | null;
  onListen: (listener: ((message: AgentDetail) => void) | null) => void;
  onClose: () => void;
  onOpenFile?: (path: string, line?: number) => void;
}) {
  const [panel, setPanel] = useState<AgentDetailPanelState>(() => ({
    sid: selection.sid,
    revision: selection.revision,
    stack: [selection.runId],
    runs: { [selection.runId]: { ...emptyAgentRun(
      selection.runId, selection.title), status: selection.status ?? "unknown" } },
  }));
  const panelRef = useRef(panel);
  panelRef.current = panel;
  const deadlines = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  const clearDeadline = useCallback((runId: string) => {
    clearTimeout(deadlines.current.get(runId));
    deadlines.current.delete(runId);
  }, []);
  useEffect(() => {
    const timers = deadlines.current;
    return () => { timers.forEach(clearTimeout); timers.clear(); };
  }, []);

  const request = useCallback((runId: string, title = "协作代理",
    before?: string | null) => {
    const current = panelRef.current;
    const run = current.runs[runId];
    const requestId = uuid();
    clearDeadline(runId);
    const sent = ws?.sendGetAgentDetail(
      current.sid, runId, before ? current.revision : undefined,
      before ? run?.detailRevision : undefined, before, 192, requestId,
    );
    setPanel((value) => {
      const currentRun = value.runs[runId] ?? emptyAgentRun(runId, title);
      return { ...value, runs: { ...value.runs, [runId]: {
        ...currentRun, title: title || currentRun.title,
        loading: !!sent, error: sent ? null : "连接未就绪，请重试读取协作代理", requestId: sent ? requestId : null,
      } } };
    });
    if (sent) deadlines.current.set(runId, setTimeout(() => {
      deadlines.current.delete(runId);
      setPanel((value) => {
        const pending = value.runs[runId];
        if (pending?.requestId !== requestId) return value;
        return { ...value, runs: { ...value.runs, [runId]: {
          ...pending, loading: false, requestId: null,
          error: "读取协作代理超时，请重试",
        } } };
      });
    }, HISTORY_DETAIL_REQUEST_TIMEOUT_MS));
    return !!sent;
  }, [clearDeadline, ws]);

  const receive = useCallback((message: AgentDetail) => {
    const current = panelRef.current;
    const run = current.runs[message.run_id];
    if (!run || message.session_id !== current.sid) return;
    if (!message.live && (!message.request_id
        || message.request_id !== run.requestId)) return;
    if (message.live && message.revision !== current.revision) return;
    if (!message.live) clearDeadline(message.run_id);
    setPanel((value) => {
      const currentRun = value.runs[message.run_id];
      if (!currentRun) return value;
      if (!message.live && message.request_id !== currentRun.requestId) {
        return value;
      }
      if (message.revision !== value.revision) {
        // A correlated stale-revision rejection is still a response. Keeping
        // the old waiter here used to leave this panel loading forever.
        const fresh = emptyAgentRun(message.run_id, currentRun.title);
        return { ...value, revision: message.revision, runs: {
          [message.run_id]: message.authoritative && !message.error && !message.before
            ? acceptAgentDetail(fresh, message)
            : { ...fresh, loading: false, error: "会话记录已更新，请重试读取协作代理" },
        }, stack: [message.run_id] };
      }
      return { ...value, runs: { ...value.runs,
        [message.run_id]: acceptAgentDetail(currentRun, message) } };
    });
  }, [clearDeadline]);

  useEffect(() => {
    onListen(receive);
    return () => onListen(null);
  }, [onListen, receive]);
  useEffect(() => {
    const current = panelRef.current;
    const id = current.stack.at(-1) ?? selection.runId;
    request(id, current.runs[id]?.title ?? selection.title);
  }, [request, selection.runId, selection.title, selection.revision]);

  const open = (runId: string, title?: string) => {
    if (!request(runId, title)) return;
    setPanel((value) => ({ ...value,
      stack: value.stack.at(-1) === runId
        ? value.stack : [...value.stack, runId] }));
  };
  const activeId = panel.stack.at(-1);
  const run = activeId ? panel.runs[activeId] : null;
  useEffect(() => {
    // Cold/native CLI agents have no resident SDK subscription. Refresh only
    // that visible active source; resident agents already push their updates.
    if (!run || run.loading || run.error || run.viewingOlder || !run.detailRevision
        || run.detailRevision.startsWith("live-")
        || !["running", "pending"].includes(run.status)) return;
    const timer = setTimeout(() => request(run.runId, run.title), 3000);
    return () => clearTimeout(timer);
  }, [request, run]);
  if (!run) return null;
  return <AgentDetailPanel run={run} engine={selection.engine} canGoBack={panel.stack.length > 1}
    onBack={() => setPanel((value) => ({
      ...value, stack: value.stack.slice(0, -1),
    }))}
    onClose={onClose}
    onRetry={() => request(run.runId, run.title)}
    onLoadEarlier={() => {
      if (run.oldestCursor && !run.loading) {
        request(run.runId, run.title, run.oldestCursor);
      }
    }}
    onOpenAgent={open}
    onOpenFile={onOpenFile} />;
}
