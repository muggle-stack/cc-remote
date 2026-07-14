import { useEffect, useReducer, useRef, useState, type TouchEvent } from "react";
import { RelayWs } from "./ws";
import { reduce, initialState, createRuntime, type Turn } from "./reducer";
import { uuid } from "./util";
import { Icon, ClaudeMark } from "./icons";
import { ChatView } from "./components/ChatView";
import { Composer } from "./components/Composer";
import { ReconnectBanner } from "./components/ReconnectBanner";
import { NoticeStack } from "./components/NoticeStack";
import { LoginForm } from "./components/LoginForm";
import { SessionsSidebar } from "./components/SessionsSidebar";
import { DirPicker } from "./components/DirPicker";
import { NewChatView } from "./components/NewChatView";
import { ArtifactPanel } from "./components/ArtifactPanel";
import { BtwPanel } from "./components/BtwPanel";
import { QuestionSheet } from "./components/QuestionSheet";
import { GoalPanel } from "./components/GoalPanel";
import { StatusSheet } from "./components/StatusSheet";
import { ForkWorktreeSheet } from "./components/ForkWorktreeSheet";
import { parseGoalCommand } from "./goal-command";
import { permsFor } from "./data";
import { shouldAcceptSessionList } from "./session-list";
import { clearLegacyAuthMarkers, probeSession } from "./session-auth";
import { collectWaitingQueries, selectDrainCandidates } from "./runtime-drain";
import { MAX_RUNTIME_SESSIONS } from "./runtime-bounds";
import { isTerminalWorktreeForkError, matchesSessionForkRequest,
  matchesWorktreeForkRequest, type PendingSessionFork,
  type PendingWorktreeFork } from "./session-worktree";
import { classifyBtwOpened, consumeDiscardedBtwSnapshot, matchesBtwRequest,
  normalizeDiffTheme, normalizeEngine, type Snapshot, type QueryImg,
  type QueryFile, type SessionInfo, type CodexPermissionMode,
  type CodexServiceTier, type CollaborationModeName,
  type DiffTheme, type Engine } from "./protocol";

const THEME_KEY = "cc_remote_theme";
const ENGINE_KEY = "cc_remote_engine";  // which backend the NEXT new session uses
const HISTORY_PAGE = 60;  // turns fetched per GetHistory (initial load + each "load more")

// The sidebar is an overlay on mobile (<980px, matches index.css) but a
// persistent grid column on desktop. So auto-close it after picking a session
// ONLY on mobile; on desktop keep it open.
const isMobile = () => window.matchMedia("(max-width: 979px)").matches;

export default function App() {
  const [theme, setTheme] = useState<DiffTheme>(
    () => normalizeDiffTheme(localStorage.getItem(THEME_KEY)));
  const [engine, setEngine] = useState<Engine>(
    () => normalizeEngine(localStorage.getItem(ENGINE_KEY)));
  const [authed, setAuthed] = useState(false);
  const [authReady, setAuthReady] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [dirPickerOpen, setDirPickerOpen] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [editPrompt, setEditPrompt] = useState<string | null>(null);
  // right slot is shared by diff + /btw; rightView picks which shows.
  const [rightView, setRightView] = useState<"diff" | "btw">("diff");
  // true from the moment /btw is clicked until the fork's btw_opened arrives — so
  // the panel appears instantly (spinner) instead of waiting ~1s for the fork.
  const [btwOpening, setBtwOpening] = useState(false);
  // Goal is deliberately opt-in UI: no empty bar and no RPC until /goal runs.
  // Keep reveal/editor state per session so switching sessions never leaks it.
  const [goalUiBySid, setGoalUiBySid] = useState<Record<string, { revealed: boolean; open: boolean }>>({});
  const [statusOpenSid, setStatusOpenSid] = useState<string | null>(null);
  const [forkWorktreeSession, setForkWorktreeSession] = useState<SessionInfo | null>(null);
  const [forkWorktreeCreating, setForkWorktreeCreating] = useState(false);
  const [forkWorktreeError, setForkWorktreeError] = useState<string | null>(null);
  const [forkingPointId, setForkingPointId] = useState<string | null>(null);
  const [state, dispatch] = useReducer(reduce, initialState);
  const stateRef = useRef(state);
  stateRef.current = state;
  const wsRef = useRef<RelayWs | null>(null);
  const drainingRef = useRef<Set<string>>(new Set());
  const pendingCreateRef = useRef<string | null>(null);
  const pendingBtwRef = useRef<string | null>(null);
  const pendingSessionForkRef = useRef<PendingSessionFork | null>(null);
  const pendingWorktreeForkRef = useRef<PendingWorktreeFork | null>(null);
  const activeBtwRef = useRef<{ requestId: string; sid: string } | null>(null);
  // Retain recently cancelled ids so a late response can be identified and
  // discarded (and a late successful fork can be closed) without disturbing a
  // newer opening spinner. Bounded because a peer may disappear permanently.
  const btwRequestIdsRef = useRef<Set<string>>(new Set());
  const discardedBtwSidsRef = useRef<Set<string>>(new Set());
  const touchStartX = useRef(0);
  // guards the once-per-connection "land on the latest session" auto-focus below
  const didInitFocusRef = useRef(false);
  const shortcutRef = useRef<{
    artifact: typeof state.artifact;
    btwSid: string | null;
    rightView: "diff" | "btw";
    getDiff: (file: string) => void;
    openBtw: () => void;
    closeBtw: () => void;
  }>({ artifact: null, btwSid: null, rightView: "diff",
    getDiff: () => {}, openBtw: () => {}, closeBtw: () => {} });

  // The focused session's runtime (turns/state/model/perm/queue/...). Falls back
  // to an empty runtime before any session is focused.
  const focusedSid = state.focusedSid;
  const rt = state.runtimes[focusedSid ?? ""] ?? createRuntime();
  const focusedEngine = (state.sessions.find(
    (session) => session.session_id === focusedSid)?.engine ?? engine) as "claude" | "codex";
  const allQueued = collectWaitingQueries(state.runtimes);
  const replaceableQueued = collectWaitingQueries(state.runtimes, focusedSid);

  const goalUi = focusedSid ? goalUiBySid[focusedSid] : undefined;

  // HttpOnly cookies can't be inspected from JS. Ask the relay whether this
  // browser session is still registered before opening a WebSocket; this also
  // makes relay restarts (which intentionally revoke old sessions) fail closed.
  useEffect(() => {
    // Never retain credentials/markers from the pre-HttpOnly implementation.
    clearLegacyAuthMarkers(localStorage);
    let cancelled = false;
    let timer: number | null = null;
    let backoff = 1000;
    const check = async () => {
      const result = await probeSession();
      if (cancelled) return;
      if (result === "unavailable") {
        setAuthReady(false);
        timer = window.setTimeout(check, backoff);
        backoff = Math.min(backoff * 2, 5000);
        return;
      }
      if (result === "unauthorized") {
        clearLegacyAuthMarkers(localStorage);
        // Do not expose the login form until prior-session prompts and
        // attachments are gone. A fast login must not race cache hydration.
        try { await import("./cache").then((module) => module.clearCache()); }
        catch { /* best-effort local cleanup */ }
        if (cancelled) return;
      }
      setAuthed(result === "authenticated");
      setAuthReady(true);
    };
    void check();
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, []);

  // swipe right -> open sidebar, swipe left -> close (mobile)
  const onTouchStart = (e: TouchEvent) => { touchStartX.current = e.touches[0].clientX; };
  const onTouchEnd = (e: TouchEvent) => {
    const dx = e.changedTouches[0].clientX - touchStartX.current;
    if (dx > 50 && touchStartX.current < window.innerWidth / 3) setSidebarOpen(true);
    else if (dx < -50) setSidebarOpen(false);
  };

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem(THEME_KEY, theme);
  }, [theme]);
  const toggleTheme = () => setTheme((t) => (t === "dark" ? "light" : "dark"));

  // `engine` selects the backend (Claude Code / Codex): the whole UI re-skins via
  // data-engine, and the sidebar re-lists that engine's own sessions.
  const engineRef = useRef(engine);
  engineRef.current = engine;
  useEffect(() => {
    document.documentElement.setAttribute("data-engine", engine);
    localStorage.setItem(ENGINE_KEY, engine);
    wsRef.current?.sendListSessions(engine);  // codex sessions vs claude sessions
  }, [engine]);
  // Tapping the engine pill switches engine AND drops you into a fresh chat for
  // it — otherwise "switch to Codex" silently only affects the *next* new session,
  // which reads as "nothing happened". Existing sessions stay in the sidebar.
  const toggleEngine = () => {
    pendingCreateRef.current = null;
    setCreateError(null);
    setEngine((e) => (e === "codex" ? "claude" : "codex"));
    dispatch({ type: "enter_new_chat", cwd: "~" });  // fresh chat defaults to home
    if (isMobile()) setSidebarOpen(false);
  };

  // WebSocket lifecycle
  useEffect(() => {
    if (!authed) return;
    const draining = drainingRef.current;
    didInitFocusRef.current = false;  // re-arm initial-focus for this connection lifecycle

    let cancelled = false;

    // A snapshot announces a session (cc_session_id/state/cwd). We do NOT reset
    // the cursor here anymore — cursors are seeded from the IndexedDB cache before
    // connecting, so hello asks the wrapper only for the DELTA instead of a full
    // history replay of every resident session (that flood wedged reconnect).
    function handleSnapshot(e: Snapshot) {
      dispatch({ type: "event", event: e });
    }

    (async () => {
      let seeded = { cursors: {} as Record<string, number>,
        generations: {} as Record<string, string> };
      try { seeded = await import("./cache").then((m) => m.loadAllReplayState()); } catch { /* best-effort */ }
      if (cancelled) return;
      const ws = new RelayWs({
        onEvent: (msg) => {
          if (msg.type === "btw_opened") {
            const disposition = classifyBtwOpened(
              pendingBtwRef.current, activeBtwRef.current, msg);
            if (disposition === "duplicate") {
              return; // cached replay after a lost ACK; the fork is already open
            }
            if (disposition === "stale") {
              // The user cancelled, navigated, or started a newer request while
              // this fork was connecting. Never let the stale response open the
              // panel, and tear down the now-unowned ephemeral session.
              const discarded = discardedBtwSidsRef.current;
              discarded.add(msg.btw_sid);
              while (discarded.size > 64) {
                const oldest = discarded.values().next().value as string | undefined;
                if (!oldest) break;
                discarded.delete(oldest);
              }
              ws.sendCloseBtw(msg.btw_sid);
              return;
            }
            pendingBtwRef.current = null;
            activeBtwRef.current = {
              requestId: msg.request_id,
              sid: msg.btw_sid,
            };
            setBtwOpening(false);
          } else if (msg.type === "error" && msg.request_id
              && btwRequestIdsRef.current.has(msg.request_id)) {
            const matches = matchesBtwRequest(
              pendingBtwRef.current, msg.request_id);
            if (!matches) return; // obsolete /btw failure; keep any newer spinner
            pendingBtwRef.current = null;
            setBtwOpening(false);
          }
          if (msg.type === "session_forked") {
            const pendingMessageFork = pendingSessionForkRef.current;
            const matchesMessageFork = msg.target === "same_cwd"
              && matchesSessionForkRequest(
              pendingMessageFork, msg.request_id,
              msg.parent_session_id, msg.last_turn_id);
            const matchesWorktreeFork = msg.target === "worktree"
              && matchesWorktreeForkRequest(
              pendingWorktreeForkRef.current, msg.request_id,
              msg.parent_session_id);
            if (!matchesMessageFork && !matchesWorktreeFork) return;
            const targetEngine = matchesMessageFork
              ? pendingMessageFork!.engine
              : "codex";
            if (matchesMessageFork) {
              pendingSessionForkRef.current = null;
              setForkingPointId(null);
            }
            if (matchesWorktreeFork) {
              pendingWorktreeForkRef.current = null;
              setForkWorktreeCreating(false);
              setForkWorktreeError(null);
              setForkWorktreeSession(null);
            }
            setEngine(targetEngine);
            dispatch({ type: "exit_new_chat" });
            dispatch({ type: "focus_session", sid: msg.session_id });
            ws.setSessionEngines([{ session_id: msg.session_id, engine: targetEngine }]);
            ws.setFocusedSid(msg.session_id, targetEngine);
            ws.sendListSessions(targetEngine);
            ws.sendSwitchSession(msg.session_id, targetEngine);
            if (isMobile()) setSidebarOpen(false);
            return;
          }
          if (msg.type === "error" && isTerminalWorktreeForkError(msg.code)
              && matchesSessionForkRequest(
                pendingSessionForkRef.current, msg.request_id)) {
            pendingSessionForkRef.current = null;
            setForkingPointId(null);
            dispatch({ type: "command_error", detail: `${msg.code}: ${msg.message}` });
            return;
          }
          if (msg.type === "error" && isTerminalWorktreeForkError(msg.code)
              && matchesWorktreeForkRequest(
              pendingWorktreeForkRef.current, msg.request_id)) {
            pendingWorktreeForkRef.current = null;
            setForkWorktreeCreating(false);
            setForkWorktreeError(msg.message);
            return;
          }
          if (msg.type === "session_focus" && msg.request_id
              && msg.request_id === pendingCreateRef.current) {
            pendingCreateRef.current = null;
            setCreateError(null);
            dispatch({ type: "exit_new_chat" });
          } else if (msg.type === "error" && msg.code !== "wrapper_offline" && msg.request_id
              && msg.request_id === pendingCreateRef.current) {
            pendingCreateRef.current = null;
            setCreateError(msg.message);
          }
          if (msg.type === "snapshot") {
            if (consumeDiscardedBtwSnapshot(discardedBtwSidsRef.current, msg)) return;
            handleSnapshot(msg);
            return;
          }
          if (msg.type === "session_rekey") {
            setGoalUiBySid((current) => {
              const prior = current[msg.old_key];
              if (!prior) return current;
              const next = { ...current, [msg.session_id]: prior };
              delete next[msg.old_key];
              return next;
            });
          }
          if (msg.type === "session_list") ws.setSessionEngines(msg.sessions);
          if (msg.type === "session_list"
              && !shouldAcceptSessionList(engineRef.current, msg)) return;
          if ((msg.type === "turn_end"
              || (msg.type === "error" && msg.code !== "wrapper_offline"))
              && msg.sid) {
            draining.delete(msg.sid);
          }
          dispatch({ type: "event", event: msg });
          if (msg.type === "wrapper_reconnected") {
            ws.sendListSessions(engineRef.current);
            ws.sendGetModels("codex");
            const currentSid = stateRef.current.focusedSid;
            if (currentSid) ws.sendGetHistory(currentSid, undefined, HISTORY_PAGE);
          }
          // refresh the context ring after each turn (local SDK query, no model tokens)
          if (msg.type === "turn_end" && msg.sid) ws.sendGetContextTo(msg.sid);
        },
        onConnState: (s, detail) => {
          dispatch({ type: "conn", connState: s, detail });
          if (s === "connected") {
            ws.sendListSessions(engineRef.current);
            // Always fetch codex's catalog, not just when codex is the active engine:
            // the engine pill switches instantly and must render real models/efforts.
            // The wrapper caches it, so a refresh doesn't respawn an app-server.
            ws.sendGetModels("codex");
          }
        },
        onAuthFail: () => {
          setAuthReady(false);
          clearLegacyAuthMarkers(localStorage);
          pendingCreateRef.current = null;
          pendingBtwRef.current = null;
          pendingSessionForkRef.current = null;
          pendingWorktreeForkRef.current = null;
          activeBtwRef.current = null;
          btwRequestIdsRef.current.clear();
          discardedBtwSidsRef.current.clear();
          setBtwOpening(false);
          setForkingPointId(null);
          setForkWorktreeSession(null);
          setForkWorktreeCreating(false);
          setForkWorktreeError(null);
          setGoalUiBySid({});
          setStatusOpenSid(null);
          dispatch({ type: "reset" });
          setAuthed(false);
          void (async () => {
            try { await import("./cache").then((module) => module.clearCache()); }
            catch { /* best-effort local cleanup */ }
            setAuthReady(true);
          })();
        },
        onCommandError: (detail) => dispatch({ type: "command_error", detail }),
        onOutboxChanged: (protectedSids) => {
          dispatch({ type: "prune_runtimes", protectedSids });
        },
        onWrapperGenerationChanged: () => {
          discardedBtwSidsRef.current.clear();
          if (stateRef.current.btwSid || pendingBtwRef.current
              || activeBtwRef.current) {
            pendingBtwRef.current = null;
            activeBtwRef.current = null;
            setBtwOpening(false);
            if (stateRef.current.btwSid) dispatch({ type: "clear_btw" });
            dispatch({ type: "command_error",
              detail: "wrapper 已重启，临时 /btw 会话已关闭，请重新打开。" });
          }
        },
      });
      ws.seedReplayState(seeded.cursors, seeded.generations);
      wsRef.current = ws;
      ws.start();
    })();

    return () => {
      cancelled = true;
      wsRef.current?.stop();
      wsRef.current = null;
      draining.clear();
    };
  }, [authed]);

  // Land on the MOST-RECENTLY-ACTIVE session on load. The wrapper's hello sends a
  // snapshot per resident session but no focus, so the reducer defaults to the
  // first one (the bootstrap session) — not where you last worked. Once the
  // session list arrives, switch to the latest by last_modified (same field the
  // sidebar sorts by). Guarded to fire once per connection: reconnects keep the
  // current view; only a fresh page load / re-login re-arms it.
  useEffect(() => {
    if (didInitFocusRef.current || !wsRef.current) return;
    if (state.sessions.length === 0) return;  // wait for the list
    const latest = [...state.sessions]
      .filter((s) => s.tag !== "archived")
      .sort((a, b) => (parseInt(b.last_modified || "0", 10) || 0) - (parseInt(a.last_modified || "0", 10) || 0))[0]
      ?? state.sessions[0];
    didInitFocusRef.current = true;
    if (latest && latest.session_id !== state.focusedSid) {
      dispatch({ type: "focus_session", sid: latest.session_id });
      wsRef.current.setFocusedSid(latest.session_id);
      wsRef.current.sendSwitchSession(latest.session_id, (latest.engine as "claude" | "codex") || engineRef.current);
    }
  }, [state.sessions, state.focusedSid]);

  // Drain every resident session, not just the one currently visible. A queued
  // background turn must resume when that runtime becomes idle even if the user
  // has switched elsewhere. Never remove work merely because the socket or
  // wrapper is offline; accepted commands are retained by RelayWs's outbox.
  useEffect(() => {
    const draining = drainingRef.current;
    for (const sid of draining) {
      const runtime = state.runtimes[sid];
      if (!runtime || runtime.state !== "idle") draining.delete(sid);
    }

    const ws = wsRef.current;
    if (!ws) return;
    const candidates = selectDrainCandidates(
      state.runtimes,
      draining,
      state.connState === "connected",
      state.wrapperOnline,
    );
    for (const { sid, source, query } of candidates) {
      const msg_id = uuid();
      if (!ws.sendQueryTo(sid, query.prompt, msg_id, query.images, query.files)) continue;
      draining.add(sid);
      dispatch({ type: "query_sent", sid, prompt: query.prompt, msg_id,
        images: query.images, files: query.files, ts: Date.now() });
      if (source === "pending") dispatch({ type: "clear_pending", sid });
      else dispatch({ type: "dequeue_at", sid, i: 0 });
    }
  }, [state.runtimes, state.connState, state.wrapperOnline]);

  // Keep a long-lived tab bounded without evicting anything that can still be
  // acted on. ACK callbacks run the same prune when an outbox target becomes
  // reclaimable; otherwise an idle runtime protected during retry would linger.
  useEffect(() => {
    if (Object.keys(state.runtimes).length <= MAX_RUNTIME_SESSIONS) return;
    dispatch({
      type: "prune_runtimes",
      protectedSids: wsRef.current?.pendingSessionIds() ?? [],
    });
  }, [state.runtimes, focusedSid, state.btwSid, state.artifact?.sid]);

  // Persist the focused session's turns to IndexedDB (Phase-2 will write through
  // background sessions too). Coalesced in cache.ts.
  useEffect(() => {
    const sid = rt.ccSessionId;
    if (!sid || rt.turns.length === 0) return;
    import("./cache").then(({ saveSession }) => {
      const live = wsRef.current?.lastSeqFor(sid) || 0;
      saveSession(sid, rt.turns, live, wsRef.current?.generationFor(sid));
    });
  }, [focusedSid, rt.turns, rt.ccSessionId]);

  // Hydrate the focused session's turns from IndexedDB for an INSTANT render on
  // switch — the wrapper's replay (for non-resident sessions) then reconciles.
  // This completes the previously write-only cache: switching no longer shows an
  // empty view while waiting on a cold wrapper round-trip. A 6s fallback clears
  // the spinner if a session has no cache and the wrapper stays silent.
  useEffect(() => {
    const sid = focusedSid;
    if (!sid) return;
    let cancelled = false;
    import("./cache").then(({ loadSession }) => loadSession(sid)).then((cached) => {
      if (!cancelled && cached && Array.isArray(cached.turns) && cached.turns.length) {
        dispatch({ type: "hydrate_cache", sid, turns: cached.turns as Turn[] });
      }
    });
    const t = window.setTimeout(() => dispatch({ type: "hydrate_cache", sid, turns: [] }), 6000);
    return () => { cancelled = true; window.clearTimeout(t); };
  }, [focusedSid]);

  // Fetch authoritative history for the focused session — bulk, on-demand, read
  // from the transcript (like a web chat's GET /conversation). Fires on focus
  // change AND on (re)connect, so a reconnect re-syncs any turns that completed
  // while we were away. The `history` event reconciles over the instant cache paint.
  useEffect(() => {
    if (!focusedSid || state.connState !== "connected") return;
    wsRef.current?.sendGetHistory(focusedSid, undefined, HISTORY_PAGE);
  }, [focusedSid, state.connState]);

  // Cmd/Ctrl+B => toggle sidebar; Cmd/Ctrl+Shift+B => open latest turn's diff
  useEffect(() => {
    if (!authed) return;
    const onKey = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey)) return;
      const k = e.key.toLowerCase();
      if (k === "b" && e.shiftKey) {           // diff (shared right slot)
        e.preventDefault();
        const latest = shortcutRef.current;
        if (latest.artifact && latest.rightView === "diff") dispatch({ type: "clear_artifact" });
        else latest.getDiff("");
      } else if (k === "b") {                    // toggle sidebar
        e.preventDefault();
        setSidebarOpen((v) => !v);
      } else if (k === "k" && e.shiftKey) {      // /btw side panel (shared right slot)
        e.preventDefault();
        const latest = shortcutRef.current;
        if (latest.btwSid && latest.rightView === "btw") latest.closeBtw();
        else latest.openBtw();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [authed]);

  // Shift+Tab follows each engine's real mode control: Claude cycles permission
  // modes; Codex toggles collaboration mode without touching approvalPolicy.
  useEffect(() => {
    if (!authed) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Tab" && e.shiftKey) {
        e.preventDefault();
        if (focusedEngine === "codex") {
          setCollaborationMode(
            rt.collaborationMode === "plan" ? "default" : "plan");
          return;
        }
        const modes = permsFor(focusedEngine).map((p) => p.id);
        const current = modes.indexOf(rt.perm);
        setPerm(modes[current < 0 ? 0 : (current + 1) % modes.length]);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [authed, focusedSid, rt.perm, rt.collaborationMode, focusedEngine]);

  // ---- /btw effects ----
  // These MUST stay ABOVE the `!authed` early return below. Hooks have to run
  // unconditionally and in the same order on every render; putting them after the
  // return meant logging out (authed -> false) rendered fewer hooks than the
  // previous render, and React blew up with #300 ("rendered fewer hooks than
  // expected"). Logging back in tripped the mirror image. Refreshing "fixed" it
  // only because a fresh mount has no previous render to disagree with.
  //
  // A /btw fork belongs to the session it was forked from. When you switch session
  // or toggle engine, discard it — else a codex btw would linger while you view a
  // cc session ("cc shows codex btw"). Read via ref so opening btw (no focus
  // change) doesn't trip this, and it only fires on actual navigation.
  const btwSidRef = useRef<string | null>(null);
  btwSidRef.current = state.btwSid;
  useEffect(() => {
    pendingBtwRef.current = null;
    activeBtwRef.current = null;
    setBtwOpening(false);
    const s = btwSidRef.current;
    if (s) { wsRef.current?.sendCloseBtw(s); dispatch({ type: "clear_btw" }); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusedSid, engine]);

  if (!authReady) {
    return <div className="login" aria-busy="true">正在连接中继…</div>;
  }

  if (!authed) {
    return <LoginForm onLogin={() => { dispatch({ type: "reset" }); setAuthed(true); }} theme={theme} onToggleTheme={toggleTheme} />;
  }

  const sendQuery = (prompt: string, images?: QueryImg[], files?: QueryFile[]): boolean => {
    if (!wsRef.current || !focusedSid) return false;
    const msg_id = uuid();
    if (!wsRef.current.sendQuery(prompt, msg_id, images, files)) return false;
    dispatch({ type: "query_sent", sid: focusedSid, prompt, msg_id, images, files, ts: Date.now() });
    return true;
  };
  // One command creates the session and starts its first query atomically. The
  // wrapper targets the new temp-keyed ctx directly; no later focus event is used
  // to route or trigger this message.
  const sendFirstMessage = (prompt: string, images?: QueryImg[], files?: QueryFile[],
                            collaborationMode?: CollaborationModeName,
                            permissionMode?: CodexPermissionMode,
                            serviceTier?: CodexServiceTier): boolean => {
    if (!wsRef.current || !state.newChat) return false;
    const { cwd, model, effort } = state.newChat;
    // Null is meaningful: let the local CLI/app-server use its configured defaults.
    // Only explicit user choices cross the wire; otherwise a stale fallback catalog
    // could silently override the machine's real model or reasoning configuration.
    const msg_id = uuid();
    const queued = wsRef.current.sendNewSession(
      cwd, engine, model, effort, { prompt, msg_id, images, files },
      engine === "codex" ? collaborationMode : undefined,
      engine === "codex" ? permissionMode : undefined,
      engine === "codex" ? serviceTier : undefined);
    if (queued) {
      pendingCreateRef.current = msg_id;
      setCreateError(null);
    }
    return queued;
  };
  const interrupt = () => wsRef.current?.sendInterrupt();
  const setModel = (model: string) => {
    wsRef.current?.sendSetModel(model);
  };
  const setEffort = (effort: string) => {
    wsRef.current?.sendSetEffort(effort);
  };
  // Codex Fast mode is persisted by app-server per thread. The runtime's Fast
  // event owns the chip state; here we only forward the requested transition.
  const setServiceTier = (tier: string) => {
    wsRef.current?.sendSetServiceTier(tier);
  };
  const setPerm = (perm: string) => {
    wsRef.current?.sendSetPerm(perm);
  };
  const setCollaborationMode = (mode: CollaborationModeName) => {
    wsRef.current?.sendSetCollaborationMode(mode);
  };
  const setGoalUi = (patch: Partial<{ revealed: boolean; open: boolean }>) => {
    if (!focusedSid) return;
    setGoalUiBySid((current) => {
      const previous = current[focusedSid] ?? { revealed: false, open: false };
      return { ...current, [focusedSid]: { ...previous, ...patch } };
    });
  };
  const runGoal = (args: string) => {
    if (!focusedSid) return;
    const command = parseGoalCommand(args);
    if (command.kind === "clear") {
      wsRef.current?.sendClearGoal();
      setGoalUi({ revealed: false, open: false });
      return;
    }
    setGoalUi({ revealed: true, open: true });
    if (command.kind === "show") wsRef.current?.sendGetGoal();
    else wsRef.current?.sendSetGoal(command.objective, "active", null);
  };
  const openStatus = () => {
    if (!focusedSid) return;
    setStatusOpenSid(focusedSid);
    wsRef.current?.sendGetStatus();
  };
  const forkFromTurn = (forkPointId: string) => {
    if (!focusedSid
        || pendingSessionForkRef.current || pendingWorktreeForkRef.current) return;
    const requestId = wsRef.current?.sendForkSession(
      focusedSid, forkPointId) ?? null;
    if (!requestId) {
      dispatch({ type: "command_error",
        detail: "派生请求未发送，请等待连接恢复后重试。" });
      return;
    }
    pendingSessionForkRef.current = {
      requestId,
      parentSessionId: focusedSid,
      forkPointId,
      engine: focusedEngine,
    };
    setForkingPointId(forkPointId);
  };
  const openForkWorktree = (session: SessionInfo) => {
    if (pendingSessionForkRef.current || pendingWorktreeForkRef.current) return;
    setForkWorktreeError(null);
    setForkWorktreeSession(session);
  };
  const submitForkWorktree = (name: string) => {
    const source = forkWorktreeSession;
    if (!source || pendingWorktreeForkRef.current) return;
    setForkWorktreeError(null);
    const requestId = wsRef.current?.sendForkSessionWorktree(source.session_id, name) ?? null;
    if (!requestId) {
      setForkWorktreeError("请求未发送，请等待连接恢复后重试。");
      return;
    }
    pendingWorktreeForkRef.current = {
      requestId,
      parentSessionId: source.session_id,
    };
    setForkWorktreeCreating(true);
  };
  const closeForkWorktree = () => {
    if (pendingWorktreeForkRef.current) return;
    setForkWorktreeSession(null);
    setForkWorktreeError(null);
  };
  const getDiff = (file: string) => {
    setRightView("diff");
    dispatch({ type: "open_artifact_loading", file, sid: focusedSid });
    wsRef.current?.sendGetDiff(file, theme);
  };
  // /btw: fork the focused session into an ephemeral side panel (wrapper replies
  // BtwOpened → reducer opens the panel). Send/close target the fork by its sid.
  const openBtw = () => {
    setRightView("btw");
    if (!focusedSid || state.btwSid || pendingBtwRef.current) return;
    const requestId = wsRef.current?.sendOpenBtw(focusedSid) ?? null;
    if (!requestId) { setBtwOpening(false); return; }
    pendingBtwRef.current = requestId;
    const requestIds = btwRequestIdsRef.current;
    requestIds.add(requestId);
    while (requestIds.size > 64) {
      const oldest = requestIds.values().next().value as string | undefined;
      if (!oldest) break;
      requestIds.delete(oldest);
    }
    setBtwOpening(true);
  };
  const sendBtw = (prompt: string) => { if (state.btwSid) wsRef.current?.sendQueryTo(state.btwSid, prompt, uuid()); };
  const closeBtw = () => {
    pendingBtwRef.current = null;
    activeBtwRef.current = null;
    setBtwOpening(false);
    if (state.btwSid) {
      wsRef.current?.sendCloseBtw(state.btwSid);
      dispatch({ type: "clear_btw" });
    }
  };
  // Header tab switch between the two right-slot views (opening the target lazily).
  const switchRight = (v: "diff" | "btw") => { if (v === "diff") getDiff(""); else openBtw(); };
  shortcutRef.current = {
    artifact: state.artifact, btwSid: state.btwSid, rightView,
    getDiff, openBtw, closeBtw,
  };
  const logout = async () => {
    try {
      const response = await fetch("/api/logout", {
        method: "POST", credentials: "same-origin", cache: "no-store",
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      await import("./cache").then((module) => module.clearCache());
      wsRef.current?.stop();
      pendingCreateRef.current = null;
      pendingBtwRef.current = null;
      pendingSessionForkRef.current = null;
      pendingWorktreeForkRef.current = null;
      activeBtwRef.current = null;
      btwRequestIdsRef.current.clear();
      discardedBtwSidsRef.current.clear();
      setCreateError(null);
      setForkingPointId(null);
      setForkWorktreeSession(null);
      setForkWorktreeCreating(false);
      setForkWorktreeError(null);
      dispatch({ type: "reset" });
      setAuthed(false);
    } catch {
      dispatch({ type: "command_error", detail: "退出失败：服务暂不可用，请稍后重试" });
    }
  };

  return (
    <div className={"shell" + (sidebarOpen ? " sidebar-open" : "") + ((state.artifact || state.btwSid || btwOpening) ? " panel-open" : "")} onTouchStart={onTouchStart} onTouchEnd={onTouchEnd}>
      <SessionsSidebar
        open={sidebarOpen}
        sessions={state.sessions}
        liveStates={Object.fromEntries(Object.entries(state.runtimes).map(([sid, r]) => [sid, r.state]))}
        activeSessionId={focusedSid}
        onSelect={(id) => { pendingCreateRef.current = null; setCreateError(null); dispatch({ type: "exit_new_chat" }); dispatch({ type: "focus_session", sid: id }); wsRef.current?.setFocusedSid(id); wsRef.current?.sendSwitchSession(id, (state.sessions.find((s) => s.session_id === id)?.engine as "claude" | "codex") || engine); if (isMobile()) setSidebarOpen(false); }}
        onNew={() => { pendingCreateRef.current = null; setCreateError(null); dispatch({ type: "enter_new_chat", cwd: "~" }); if (isMobile()) setSidebarOpen(false); }}
        onNewInDir={(cwd) => { pendingCreateRef.current = null; setCreateError(null); dispatch({ type: "enter_new_chat", cwd }); if (isMobile()) setSidebarOpen(false); }}
        onClose={() => setSidebarOpen(false)}
        onRename={(id, title) => wsRef.current?.sendRenameSession(id, title)}
        onArchive={(id, archived) => { wsRef.current?.sendArchiveSession(id, archived); }}
        onForkWorktree={openForkWorktree}
      />
      <DirPicker
        open={dirPickerOpen}
        path={state.dirPicker?.path ?? null}
        parent={state.dirPicker?.parent ?? null}
        dirs={state.dirPicker?.dirs ?? []}
        onBrowse={(p) => wsRef.current?.sendListDir(p)}
        onConfirm={(cwd) => { if (state.newChat) dispatch({ type: "set_new_chat_cwd", cwd }); setDirPickerOpen(false); }}
        onClose={() => setDirPickerOpen(false)}
      />
      <section className="pane">
        <header className="c-head">
          <div className="titlewrap">
            <div className="ttl">
              <span className="brand" onClick={() => setSidebarOpen(true)} style={{ cursor: "pointer" }}>
                <span className="brand-mark"><ClaudeMark size={18} /></span>
                <span className="name serif"><b>cc</b><span>·remote</span></span>
              </span>
            </div>
            <div className="sub">{rt.ccSessionId ? `session ${rt.ccSessionId.slice(0, 8)}` : "connected"}</div>
          </div>
          <span className={`hstat ${rt.state}`}><span className="sd" />{rt.state}</span>
          <button className="engine-toggle" onClick={toggleEngine} aria-label="切换新会话引擎"
            title="新建会话使用的引擎">{engine === "codex" ? "◇ Codex" : "✳ Claude"}</button>
          <button className="iconbtn" onClick={toggleTheme} aria-label="切换主题">
            <Icon name={theme === "dark" ? "sun" : "moon"} />
          </button>
          <button className="iconbtn" onClick={() => void logout()} aria-label="退出"><Icon name="dots" /></button>
        </header>

        <ReconnectBanner banner={state.banner} replaying={rt.replaying} truncated={rt.truncated} />
        <NoticeStack notices={rt.notices}
          onDismiss={(noticeId) => {
            if (focusedSid) dispatch({ type: "dismiss_notice", sid: focusedSid, noticeId });
          }} />

        {state.newChat ? (
          <NewChatView cwd={state.newChat.cwd}
            createError={createError}
            engine={engine}
            onPickCwd={() => setDirPickerOpen(true)}
            onSend={sendFirstMessage} />
        ) : (
          <>
            <ChatView sid={focusedSid} turns={rt.turns} loading={!!rt.loading}
              engine={focusedEngine} forkingPointId={forkingPointId}
              hasMore={!!rt.hasMore}
              onLoadMore={() => { if (focusedSid) wsRef.current?.sendGetHistory(focusedSid, rt.oldestId, HISTORY_PAGE); }}
              onEdit={(prompt) => setEditPrompt(prompt)} onGetDiff={getDiff}
              onFork={forkFromTurn} />

            <GoalPanel engine={engine} goal={rt.goal}
              revealed={!!goalUi?.revealed} open={!!goalUi?.open}
              onOpen={() => { wsRef.current?.sendGetGoal(); setGoalUi({ revealed: true, open: true }); }}
              onClose={() => setGoalUi({ open: false })}
              onDismiss={() => setGoalUi({ revealed: false, open: false })}
              onSave={(objective, status, budget) => {
                wsRef.current?.sendSetGoal(objective, status, engine === "codex" ? budget : null);
                setGoalUi({ revealed: true, open: false });
              }}
              onClear={() => {
                wsRef.current?.sendClearGoal();
                setGoalUi({ revealed: false, open: false });
              }} />

            <Composer
          state={rt.state}
          catalog={state.catalog}
          connState={state.connState}
          wrapperOnline={state.wrapperOnline}
          sendMode={state.sendMode}
          setSendMode={(m) => dispatch({ type: "set_send_mode", mode: m })}
          queue={rt.queue}
          allQueued={allQueued}
          replaceableQueued={replaceableQueued}
          model={rt.model}
          effort={rt.effort}
          perm={rt.perm}
          collaborationMode={rt.collaborationMode}
          fast={rt.fast}
          external={rt.external}
          takeoverPending={rt.takeoverPending}
          takeoverMessage={rt.takeoverMessage}
          onTakeover={() => { if (focusedSid) wsRef.current?.sendTakeover(focusedSid); }}
          engine={focusedEngine}
          editPrompt={editPrompt}
          onEditConsumed={() => setEditPrompt(null)}
          onSendQuery={sendQuery}
          onInterrupt={interrupt}
          onEnqueue={(query) => dispatch({ type: "enqueue", query })}
          onSetPending={(query) => dispatch({ type: "set_pending", query })}
          onDequeue={(i) => { if (focusedSid) dispatch({ type: "dequeue_at", sid: focusedSid, i }); }}
          onSetModel={setModel}
          onSetEffort={setEffort}
          onSetServiceTier={setServiceTier}
          onSetPerm={setPerm}
          onSetCollaborationMode={setCollaborationMode}
          onClear={() => dispatch({ type: "enter_new_chat", cwd: state.currentCwd })}
          onContext={() => wsRef.current?.sendGetContext()}
          onOpenBtw={openBtw}
          onGoal={runGoal}
          onStatus={openStatus}
          contextReport={rt.contextReport}
        />
          </>
        )}
        {/* context usage now lives in the composer's ring popover (see Composer) */}
      </section>
      {/* Shared right slot: diff and /btw take turns; header tabs switch. */}
      {(() => {
        const btwShowing = !!state.btwSid || btwOpening;
        const view = rightView === "btw" && btwShowing ? "btw"
          : state.artifact ? "diff" : btwShowing ? "btw" : null;
        if (view === "btw")
          return <BtwPanel sid={state.btwSid ?? undefined} rt={state.btwSid ? state.runtimes[state.btwSid] : undefined}
            engine={state.btwEngine} opening={btwOpening && !state.btwSid}
            active="btw" hasDiff={!!state.artifact} onTab={switchRight}
            onSend={sendBtw} onClose={closeBtw}
            onDismissNotice={(noticeId) => {
              if (state.btwSid) dispatch({ type: "dismiss_notice", sid: state.btwSid, noticeId });
            }} />;
        if (view === "diff" && state.artifact)
          return <ArtifactPanel artifact={state.artifact} active="diff" hasBtw={!!state.btwSid}
            onTab={switchRight} onClose={() => dispatch({ type: "clear_artifact" })} />;
        return null;
      })()}
      {rt.pendingQuestion && (
        <QuestionSheet
          header={rt.pendingQuestion.header}
          question={rt.pendingQuestion.question}
          options={rt.pendingQuestion.options}
          allowText={rt.pendingQuestion.allow_text}
          secret={rt.pendingQuestion.secret}
          onAnswer={(answer) => {
            wsRef.current?.sendAnswerQuestion(rt.pendingQuestion!.ask_id, answer);
            dispatch({ type: "answer_question" });
          }}
        />
      )}
      <StatusSheet open={statusOpenSid === focusedSid} report={rt.statusReport}
        onClose={() => setStatusOpenSid(null)}
        onRefresh={() => wsRef.current?.sendGetStatus()} />
      <ForkWorktreeSheet open={forkWorktreeSession !== null} session={forkWorktreeSession}
        creating={forkWorktreeCreating} error={forkWorktreeError}
        onConfirm={submitForkWorktree} onClose={closeForkWorktree} />
    </div>
  );
}
