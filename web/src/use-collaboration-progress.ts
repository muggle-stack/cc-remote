import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import type { CollaborationMember, ServerEvent } from "./protocol";
import type { RelayWs } from "./ws";
import { createRuntime, initialState, reduce, type SessionRuntime } from "./reducer";
import { displayHistoryProjection } from "./history-recovery";
import {
  acceptsCachedNewerPage,
  appendNewerPage,
  cachedLatestRequiresLiveRuntime,
  canonicalTurnId,
  createHistoryBrowse,
  prependOlderPage,
  type HistoryBrowseProjection,
} from "./history-browse";
import { historyPageCacheScopeKey, type HistoryPageCache, type HistoryPageCacheScope } from "./history-page-cache";
import { HISTORY_INITIAL_PAGE, HISTORY_MORE_PAGE, HISTORY_LATEST_PAGE_KEY, historyPageKey, summaryHistoryTurns } from "./history-summary";
import {
  HISTORY_REQUEST_TIMEOUT_MS,
  HistoryDetailRequestCoordinator,
  type HistoryDetailRequestContext,
} from "./history-requests";

interface OlderRequest {
  scope: HistoryPageCacheScope;
  browse: HistoryBrowseProjection;
  before: string;
  anchorTurnId?: string;
  timer: ReturnType<typeof setTimeout>;
}

export function useCollaborationProgress({ machineId, member, ws, listen, initialRuntime, cache }: {
  machineId: string;
  member: CollaborationMember;
  ws: RelayWs | null;
  listen: (handler: ((event: ServerEvent) => void) | null) => void;
  initialRuntime?: SessionRuntime;
  cache: HistoryPageCache;
}) {
  const sid = member.sid!;
  const [state, dispatch] = useReducer(reduce, initialRuntime, (seed): typeof initialState => ({
    ...initialState,
    focusedSid: sid,
    sessions: [{
      session_id: sid, native_session_id: member.native_sid,
      engine: member.spec.engine, space: "code", cwd: member.cwd || member.spec.cwd || "",
      claude_profile_id: member.spec.engine === "claude" ? member.spec.profile_id : undefined,
      codex_profile_id: member.spec.engine === "codex" ? member.spec.profile_id : undefined,
    }],
    runtimes: { [sid]: seed && !seed.historyInvalidated ? {
      ...seed,
      turns: seed.turns.map(turn => turn.detailLoading
        ? { ...turn, detailLoading: false } : turn),
    } : createRuntime() },
  }));
  const stateRef = useRef(state);
  stateRef.current = state;
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const deadline = useRef<ReturnType<typeof setTimeout> | null>(null);
  const olderRequest = useRef<OlderRequest | null>(null);
  const [detailRequests] = useState(() => new HistoryDetailRequestCoordinator(context => {
    dispatch({ type: "history_detail_cancelled", context });
  }));
  const alive = useRef(true);
  const runtime = state.runtimes[sid] ?? createRuntime();
  const view = displayHistoryProjection(state.historyRecovery, sid, runtime,
    state.historyBrowse, state.retainedHistoryBrowse);

  const request = useCallback(() => {
    if (deadline.current) clearTimeout(deadline.current);
    if (olderRequest.current) clearTimeout(olderRequest.current.timer);
    olderRequest.current = null;
    for (const context of detailRequests.clear()) dispatch({ type: "history_detail_cancelled", context });
    dispatch({ type: "return_to_latest", sid });
    setError(null);
    setLoaded(false);
    if (!ws?.sendGetHistory(sid, null, HISTORY_INITIAL_PAGE, member.cwd)) {
      setError("设备连接恢复后即可读取进度。");
      setLoaded(true);
      return;
    }
    deadline.current = setTimeout(() => {
      setLoaded(true);
      setError("原生过程尚未返回，请重试。");
    }, HISTORY_REQUEST_TIMEOUT_MS);
  }, [detailRequests, sid, member.cwd, ws]);

  useEffect(() => {
    alive.current = true;
    listen(event => {
      const eventSid = event.type === "history" || event.type === "turn_detail"
        ? event.session_id : event.sid;
      if (eventSid !== sid) return;
      if (event.type === "history" && event.before) {
        const pending = olderRequest.current;
        if (!pending || pending.before !== event.before) return;
        clearTimeout(pending.timer);
        olderRequest.current = null;
        const identity = {
          sid, scopeKey: pending.browse.scopeKey, revision: pending.browse.revision,
          generation: pending.browse.generation, viewId: pending.browse.viewId,
          windowEpoch: pending.browse.windowEpoch, before: pending.before,
        };
        if (event.error || event.authoritative === false
            || event.revision !== identity.revision
            || (event.generation != null && event.generation !== identity.generation)) {
          dispatch({ type: "history_browse_page_failed", ...identity });
          setError(event.error || "历史已更新，请重试。");
          return;
        }
        let turns = summaryHistoryTurns(event);
        if (!turns) {
          // Compatibility pages use the native decoder in a disposable runtime.
          // Their lifecycle/control fields never enter the active session.
          const decoded = reduce({
            ...initialState, sessions: stateRef.current.sessions,
            runtimes: { [sid]: createRuntime() },
          }, { type: "event", event: { ...event, before: null, control: null, in_progress: false } });
          turns = decoded.runtimes[sid]?.turns ?? [];
        }
        const page = {
          pageKey: historyPageKey(event.before), turns, hasOlder: event.has_more,
          olderCursor: event.oldest_id ?? (turns[0] ? canonicalTurnId(turns[0]) : null),
          hasNewer: true, newerPageKey: pending.browse.oldestPageKey, isLatest: false,
        };
        const browse = stateRef.current.historyBrowse ?? pending.browse;
        const protectedTurnIds = pending.anchorTurnId ? [pending.anchorTurnId] : [];
        const mutation = prependOlderPage(browse, page, {
          expectedScopeKey: identity.scopeKey, expectedViewId: identity.viewId,
          expectedWindowEpoch: identity.windowEpoch, expectedOlderCursor: identity.before,
          protectedTurnIds,
        });
        void cache.putPage(pending.scope, page);
        for (const evicted of mutation.evictedPages) void cache.putPage(pending.scope, evicted);
        dispatch({ type: "install_history_browse_page", ...identity, page, protectedTurnIds,
          prepared: { from: browse, to: mutation.projection } });
        return;
      }
      if (event.type === "turn_detail") {
        for (const context of detailRequests.completeAll(event)) {
          if (context.target === "runtime") dispatch({ type: "event", event });
          else dispatch({
            type: "history_browse_detail", sid, scopeKey: context.scopeKey,
            revision: context.revision, viewId: context.viewId, windowEpoch: context.windowEpoch,
            turnId: event.turn_id, events: event.events, error: event.error,
            resetRequired: event.reset_required, before: event.before,
            hasMore: event.has_more, oldestCursor: event.oldest_cursor,
            hasNewer: event.has_newer, newerCursor: event.newer_cursor,
          });
        }
        return;
      }
      dispatch({ type: "event", event });
      if (event.type === "replay_start" && (event.truncated || event.rebuild)) {
        request();
      } else if (event.type === "history") {
        if (deadline.current) clearTimeout(deadline.current);
        setLoaded(true);
        setError(event.error ?? null);
      }
    });
    request();
    return () => {
      alive.current = false;
      listen(null);
      if (deadline.current) clearTimeout(deadline.current);
      if (olderRequest.current) clearTimeout(olderRequest.current.timer);
      detailRequests.clear();
    };
  }, [cache, detailRequests, listen, request, sid]);

  useEffect(() => {
    // The shared reducer requires a newer build to confirm the first page
    // observed after a replay gap, which may already have been in flight.
    if (runtime.historyInvalidated && runtime.pendingHistoryCandidateBuildSeq != null) request();
  }, [request, runtime.historyInvalidated, runtime.pendingHistoryCandidateBuildSeq]);

  const loadOlder = (anchorTurnId?: string) => {
    const current = stateRef.current;
    const active = current.runtimes[sid];
    if (!loaded || error || olderRequest.current || !active?.historyRevision
        || active.historyInvalidated) return false;
    let browse = current.historyBrowse;
    const before = browse ? browse.olderCursor : active.oldestId;
    if (!(browse ? browse.hasOlder : active.hasMore) || !before) return false;
    const scope = { machineId, sid, engine: member.spec.engine, space: "code", revision: active.historyRevision };
    if (!browse) {
      const viewId = crypto.randomUUID();
      const basePageKey = `${HISTORY_LATEST_PAGE_KEY}:${viewId}`;
      const identity = { sid, scopeKey: historyPageCacheScopeKey(scope), revision: active.historyRevision,
        generation: active.historyGeneration, viewId, basePageKey };
      browse = createHistoryBrowse({ ...identity, baseTurns: active.turns,
        hasOlder: true, olderCursor: before }).projection;
      void cache.putPage(scope, { pageKey: basePageKey, turns: active.turns,
        hasOlder: true, olderCursor: before, isLatest: true });
      dispatch({ type: "begin_history_browse", ...identity });
    }
    const frozen = browse;
    const timer = setTimeout(() => {
      olderRequest.current = null;
      dispatch({ type: "history_browse_page_failed", sid, scopeKey: frozen.scopeKey,
        revision: frozen.revision, generation: frozen.generation,
        viewId: frozen.viewId, windowEpoch: frozen.windowEpoch, before });
      setError("加载更早历史超时，请重试。");
    }, HISTORY_REQUEST_TIMEOUT_MS);
    olderRequest.current = { scope, browse, before, anchorTurnId, timer };
    if (!ws?.sendGetHistory(sid, before, HISTORY_MORE_PAGE, member.cwd)) {
      clearTimeout(timer);
      olderRequest.current = null;
      return false;
    }
    return { accepted: true as const, viewId: browse.viewId, scopeKey: browse.scopeKey,
      generation: browse.generation };
  };

  const loadNewer = (anchorTurnId?: string) => {
    const browse = stateRef.current.historyBrowse;
    if (!browse?.hasNewer || !browse.newerPageKey) return false;
    const frozen = { sid, scopeKey: browse.scopeKey, revision: browse.revision,
      generation: browse.generation, viewId: browse.viewId, windowEpoch: browse.windowEpoch,
      pageKey: browse.newerPageKey };
    const scope = { machineId, sid, engine: member.spec.engine, space: "code", revision: browse.revision };
    void cache.getPage(scope, frozen.pageKey).then(page => {
      if (!alive.current) return;
      const current = stateRef.current.historyBrowse;
      if (!current || !acceptsCachedNewerPage(current, frozen)) return;
      if (cachedLatestRequiresLiveRuntime(current, page, `${HISTORY_LATEST_PAGE_KEY}:${frozen.viewId}`)) {
        dispatch({ type: "return_to_latest", sid });
        return;
      }
      if (!page) {
        dispatch({ type: "history_browse_newer_unavailable", ...frozen });
        return;
      }
      const protectedTurnIds = anchorTurnId ? [anchorTurnId] : [];
      const mutation = appendNewerPage(current, page, {
        expectedScopeKey: frozen.scopeKey, expectedViewId: frozen.viewId,
        expectedWindowEpoch: frozen.windowEpoch, expectedNewerPageKey: frozen.pageKey,
        protectedTurnIds,
      });
      for (const evicted of mutation.evictedPages) void cache.putPage(scope, evicted);
      dispatch({ type: "install_history_browse_newer", ...frozen, page, protectedTurnIds,
        prepared: { from: current, to: mutation.projection } });
    });
    return true;
  };

  const loadDetail = (turnId: string, before?: string | null, autoLoad?: boolean) => {
    const current = stateRef.current;
    const revision = current.runtimes[sid]?.historyRevision;
    if (!revision || current.runtimes[sid]?.historyInvalidated) return false;
    const browse = current.historyBrowse;
    const context: HistoryDetailRequestContext = browse ? {
      target: "browse", sid, scopeKey: browse.scopeKey, revision,
      viewId: browse.viewId, windowEpoch: browse.windowEpoch, turnId, before,
    } : {
      target: "runtime", sid,
      scopeKey: historyPageCacheScopeKey({ machineId, sid, engine: member.spec.engine, space: "code", revision }),
      revision, turnId, before, autoLoad,
    };
    const registration = detailRequests.register(context);
    if (!registration.accepted) return false;
    if (registration.send && !ws?.sendGetTurnDetail(sid, turnId, revision, before)) {
      detailRequests.cancel(context);
      return false;
    }
    if (browse) {
      dispatch({ type: "history_browse_detail_requested", sid, scopeKey: browse.scopeKey,
        revision, viewId: browse.viewId, windowEpoch: browse.windowEpoch, turnId, before });
    } else dispatch({ type: "turn_detail_requested", sid, turnId, before, autoLoad });
    return true;
  };

  return { runtime, view, loading: !loaded, error, request, loadOlder, loadNewer, loadDetail,
    returnToLatest: () => dispatch({ type: "return_to_latest", sid }) };
}
