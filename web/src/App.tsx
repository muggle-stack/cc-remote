import { useCallback, useEffect, useReducer, useRef, useState, type TouchEvent } from "react";
import { RelayWs, sessionScopeKey, type EventOwnership } from "./ws";
import {
  createRuntime,
  deferredQueueCapacity,
  initialState,
  reduce,
  type PendingQuery,
} from "./reducer";
import type { Turn } from "./domain/conversation";
import { uuid } from "./util";
import { Icon } from "./icons";
import { ChatView } from "./components/ChatView";
import { Composer } from "./components/Composer";
import {
  QueuedQueryDialog,
  type QueuedQueryEditor,
} from "./components/QueuedQueryDialog";
import { ReconnectBanner } from "./components/ReconnectBanner";
import { NoticeStack } from "./components/NoticeStack";
import { presentCommandProblem } from "./problem-presentation";
import { LoginForm } from "./components/LoginForm";
import { SessionsSidebar } from "./components/SessionsSidebar";
import { DirPicker } from "./components/DirPicker";
import {
  compatibleNewChatEffort,
  newChatCatalogRequest,
  NewChatView,
  reconcileNewChatSelection,
  resolveNewChatLocalDefaults,
} from "./components/NewChatView";
import { ArtifactPanel } from "./components/ArtifactPanel";
import { BtwPanel } from "./components/BtwPanel";
import { QuestionSheet } from "./components/QuestionSheet";
import { GoalPanel } from "./components/GoalPanel";
import { StatusSheet } from "./components/StatusSheet";
import { ForkWorktreeSheet } from "./components/ForkWorktreeSheet";
import { WorkDashboardSheet } from "./components/WorkDashboardSheet";
import { WorkArtifactsSheet } from "./components/WorkArtifactsSheet";
import { CapabilitiesSheet, type HookDraft, type SkillDraft } from "./components/CapabilitiesSheet";
import { TerminalControl } from "./components/TerminalControl";
import { DeviceSheet, type PairingState, type RemoteDevice } from "./components/DeviceSheet";
import { HeaderMenu } from "./components/HeaderMenu";
import { parseGoalCommand } from "./goal-command";
import { shouldOpenCodexStatus } from "./status-capabilities";
import { permsFor } from "./data";
import {
  shouldAcceptSessionList,
  updateScopedSessionLifecycle,
} from "./session-list";
import { clearLegacyAuthMarkers, probeSession } from "./session-auth";
import { nextAutoLoadDetailTurn } from "./history-detail-projection";
import {
  canEnqueueQuery,
  collectUnconfirmedQueries,
} from "./runtime-drain";
import type { SendMode } from "./composer-submit";
import { MAX_RUNTIME_SESSIONS } from "./runtime-bounds";
import {
  isTerminalSessionMigrationError,
  isTerminalWorktreeForkError,
  matchesSessionForkRequest,
  matchesSessionMigrationRequest,
  matchesWorktreeForkRequest,
  reconcileOpenMigrationSession,
  type PendingSessionFork,
  type PendingSessionMigration,
  type PendingWorktreeFork,
} from "./session-worktree";
import { classifyBtwOpened, consumeDiscardedBtwSnapshot, matchesBtwRequest,
  normalizeDiffTheme, normalizeEngine, type Snapshot, type QueryImg,
  type QueryFile, type SessionInfo, type CodexPermissionMode,
  type CodexWebSearchMode, type PermissionProfileInfo,
  type CodexServiceTier, type CollaborationModeName,
  type DiffTheme, type Engine, type Space,
  type SessionControl, type History, sessionControlLocksInput } from "./protocol";
import type { EngineCapabilities, EngineCapabilityItem, EngineCapabilityKind, WorkArtifactInfo, WorkDashboard } from "./protocol";
import { isMarkdownPath } from "./preview-path";
import { parseGitDiff } from "./diff";
import { resolveSidebarSwipe } from "./responsive-layout";
import {
  bumpSessionActivity,
  compareSessionsByActivity,
  mergeSessionActivityState,
  sessionCommandTarget,
  setSessionPinned,
} from "./session-order";
import {
  disableRemotePush,
  enableRemotePush,
  PushBindingCoordinator,
  type PushBindingSnapshot,
} from "./push";
import {
  protectedHistoryTurnIds,
  type TextSelectionGuard,
} from "./history-selection-guard";
import {
  readNotificationMode,
  writeNotificationMode,
  type NotificationMode,
} from "./notification-mode";
import {
  captureNotificationFragment,
  consumeNotificationTarget,
  encodeNotificationRoute,
  NOTIFICATION_TARGET_KEY,
  parseNotificationRoute,
  storeNotificationTarget,
  type NotificationRoute,
} from "./notification-route";
import {
  resolveNotificationNavigation,
  type NotificationOrigin,
} from "./notification-navigation";
import {
  turnNotificationPresentation,
  turnNotificationTag,
} from "./turn-notification";
import {
  HistoryRequestCoordinator,
  HistoryDetailRequestCoordinator,
  resolveHistoryCwdHint,
  type HistoryBrowseRequestContext,
  type HistoryDetailRequestContext,
} from "./history-requests";
import { RecoverableReadCoordinator } from "./recoverable-read";
import { InlineImageAssetCache } from "./inline-image-assets";
import { HistoryImageAssetCache } from "./history-image-assets";
import {
  displayHistoryProjection,
  historyConfirmsRecovery,
  historyConfirmsRuntimeRecovery,
  historyNeedsConfirmationRequest,
  isHistoryRecoveryPending,
  isRuntimeHistoryRecoveryPending,
} from "./history-recovery";
import {
  acceptsCachedNewerPage,
  appendNewerPage,
  canonicalTurnId,
  prependOlderPage,
  type HistoryBrowsePage,
} from "./history-browse";
import {
  HistoryPageCache,
  historyPageCacheScopeKey,
  type HistoryPageCacheScope,
  type HistoryPageCacheSessionScope,
} from "./history-page-cache";
import {
  HISTORY_INITIAL_PAGE,
  HISTORY_LATEST_PAGE_KEY,
  HISTORY_MORE_PAGE,
  HISTORY_PROVISIONAL_WATCHDOG_MS,
  historyPageKey,
  summaryHistoryTurns,
} from "./history-summary";
import { ComposerDraftStore, composerDraftKey } from "./composer-drafts";
import {
  acknowledgeCompletion,
  completionBadgeKind,
  discardBtwCompletionReceipts,
  markCompletionUnread,
  rekeyCompletionReceipts,
  type CompletionBadgeKind,
  type CompletionReceipts,
} from "./completion-badges";
import {
  cacheSkillCatalog,
  SkillCatalogRequestCoordinator,
  skillCatalogFresh,
  skillCatalogKey,
  skillCatalogRefreshSucceeded,
  type SkillCatalogCacheEntry,
  type SkillCatalogRequest,
} from "./skill-catalog-cache";

const THEME_KEY = "cc_remote_theme";
const ENGINE_KEY = "cc_remote_engine";  // which backend the NEXT new session uses
const SPACE_KEY = "cc_remote_space";
const MACHINE_KEY = "cc_remote_machine";

interface QueuedQueryEditorState extends QueuedQueryEditor {
  detailRequestId: string | null;
  updateRequestId: string | null;
  pendingPrompt: string | null;
}

// The sidebar is an overlay on mobile (<980px, matches index.css) but a
// persistent grid column on desktop. So auto-close it after picking a session
// ONLY on mobile; on desktop keep it open.
const isMobile = () => window.matchMedia("(max-width: 979px)").matches;

export default function App() {
  const [theme, setTheme] = useState<DiffTheme>(
    () => normalizeDiffTheme(localStorage.getItem(THEME_KEY)));
  const [engine, setEngine] = useState<Engine>(
    () => normalizeEngine(localStorage.getItem(ENGINE_KEY)));
  const [space, setSpace] = useState<Space>(
    () => localStorage.getItem(SPACE_KEY) === "work" ? "work" : "code");
  const [authed, setAuthed] = useState(false);
  const [authReady, setAuthReady] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [dirPickerOpen, setDirPickerOpen] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [newChatAutoFocus, setNewChatAutoFocus] = useState(true);
  const [editPrompt, setEditPrompt] = useState<string | null>(null);
  const [queuedQueryEditor, setQueuedQueryEditor] =
    useState<QueuedQueryEditorState | null>(null);
  // right slot is shared by diff + /btw; rightView picks which shows.
  const [rightView, setRightView] = useState<"diff" | "btw">("diff");
  // true from the moment /btw is clicked until the fork's btw_opened arrives — so
  // the panel appears instantly (spinner) instead of waiting ~1s for the fork.
  const [btwOpeningByParentSid, setBtwOpeningByParentSid] = useState<Record<string, boolean>>({});
  // Goal is deliberately opt-in UI: no empty bar and no RPC until /goal runs.
  // Keep reveal/editor state per session so switching sessions never leaks it.
  const [goalUiBySid, setGoalUiBySid] = useState<Record<string, { revealed: boolean; open: boolean }>>({});
  const [statusOpenSid, setStatusOpenSid] = useState<string | null>(null);
  const [forkWorktreeSession, setForkWorktreeSession] = useState<SessionInfo | null>(null);
  const [forkWorktreeCreating, setForkWorktreeCreating] = useState(false);
  const [forkWorktreeError, setForkWorktreeError] = useState<string | null>(null);
  const [migrateSession, setMigrateSession] = useState<SessionInfo | null>(null);
  const [migrateCreating, setMigrateCreating] = useState(false);
  const [migrateError, setMigrateError] = useState<string | null>(null);
  const [forkingPointId, setForkingPointId] = useState<string | null>(null);
  const [workManagerOpen, setWorkManagerOpen] = useState(false);
  const [workArtifactsOpen, setWorkArtifactsOpen] = useState(false);
  const [capabilitiesOpen, setCapabilitiesOpen] = useState(false);
  const [capabilitiesKind, setCapabilitiesKind] = useState<EngineCapabilityKind | "all">("all");
  const [capabilitiesLoading, setCapabilitiesLoading] = useState(false);
  const [deviceSheetOpen, setDeviceSheetOpen] = useState(false);
  const [capabilitiesByScope, setCapabilitiesByScope] =
    useState<Record<string, EngineCapabilities>>({});
  const [skillCatalogs, setSkillCatalogs] =
    useState<Record<string, SkillCatalogCacheEntry>>({});
  const [notificationMode, setNotificationMode] = useState<NotificationMode>(
    () => readNotificationMode(localStorage));
  const [pushBinding, setPushBinding] = useState<PushBindingSnapshot>({
    state: "off", target: null, bound: null,
  });
  const [machineId, setMachineId] = useState(
    () => localStorage.getItem(MACHINE_KEY) || "default");
  const [remoteDevices, setRemoteDevices] = useState<RemoteDevice[]>([]);
  const [devicesLoadState, setDevicesLoadState] = useState<
    "idle" | "loading" | "ready" | "error"
  >("idle");
  const [pendingNotificationTarget, setPendingNotificationTarget] = useState<
    NotificationRoute | null
  >(() => {
    let captured: NotificationRoute | null = null;
    try {
      captured = captureNotificationFragment(
        window.location, sessionStorage, window.history);
    } catch {
      // Keep trying the in-memory capture below.
    }
    try {
      return consumeNotificationTarget(sessionStorage) ?? captured;
    } catch {
      return captured;
    }
  });
  const [notificationListRevision, bumpNotificationListRevision] = useReducer(
    (value: number) => value + 1, 0);
  const [devicePairing, setDevicePairing] = useState<PairingState>({
    enabled: false, expires_at: null,
  });
  const [workProjectId, setWorkProjectId] = useState<string | null>(null);
  const [workDashboards, setWorkDashboards] = useState<Partial<Record<Engine, WorkDashboard>>>({});
  const [workArtifactsBySid, setWorkArtifactsBySid] = useState<Record<string, WorkArtifactInfo[]>>({});
  const [completionReceipts, setCompletionReceipts] = useState<CompletionReceipts>({});
  const [btwSendModeBySid, setBtwSendModeBySid] = useState<
    Record<string, SendMode>
  >({});
  const [newChatPermissionCatalog, setNewChatPermissionCatalog] = useState<{
    machineId: string;
    cwd: string;
    profiles: PermissionProfileInfo[];
  } | null>(null);
  const [state, dispatch] = useReducer(reduce, initialState);
  const inlineImageAssetsRef = useRef(new InlineImageAssetCache());
  const [, bumpInlineImageRevision] = useReducer((value: number) => value + 1, 0);
  const historyImageAssetsRef = useRef(new HistoryImageAssetCache());
  const composerDraftsRef = useRef(new ComposerDraftStore());
  const btwDraftsRef = useRef(new ComposerDraftStore());
  const [, bumpHistoryImageRevision] = useReducer((value: number) => value + 1, 0);
  const dismissBanner = useCallback((banner: string) => {
    dispatch({ type: "dismiss_banner", banner });
  }, []);
  const pushCoordinatorRef = useRef<PushBindingCoordinator | null>(null);
  if (pushCoordinatorRef.current === null) {
    pushCoordinatorRef.current = new PushBindingCoordinator(async (target) => (
      target
        ? enableRemotePush(target.machineId, target.mode)
        : disableRemotePush()
    ));
  }
  const stateRef = useRef(state);
  stateRef.current = state;
  const rightViewRef = useRef(rightView);
  rightViewRef.current = rightView;
  const wsRef = useRef<RelayWs | null>(null);
  const skillCatalogsRef =
    useRef<Record<string, SkillCatalogCacheEntry>>({});
  const skillCatalogRequestsRef =
    useRef<SkillCatalogRequestCoordinator | null>(null);
  if (skillCatalogRequestsRef.current === null) {
    skillCatalogRequestsRef.current = new SkillCatalogRequestCoordinator(
      (request) => wsRef.current?.sendGetEngineCapabilities(
        request.engine,
        request.space,
        request.cwd,
        request.skillsOnly,
      ) ?? null,
    );
  }
  const focusedSkillScopeRef = useRef<{
    key: string;
    engine: Engine;
    space: Space;
    cwd: string;
    skillsOnly: boolean;
  } | null>(null);
  const historyRequestsRef = useRef(new HistoryRequestCoordinator());
  const historyDetailRequestsRef = useRef(new HistoryDetailRequestCoordinator(
    (context) => {
      dispatch({ type: "history_detail_cancelled", context });
    },
  ));
  const clearHistoryDetailRequests = useCallback(() => {
    for (const context of historyDetailRequestsRef.current.clear()) {
      dispatch({ type: "history_detail_cancelled", context });
    }
  }, []);
  const historyPageCacheRef = useRef(new HistoryPageCache());
  const historyPageScopesRef =
    useRef(new Map<string, HistoryPageCacheScope>());
  const textSelectionGuardRef = useRef<TextSelectionGuard | null>(null);
  const updateTextSelectionGuard = useCallback(
    (guard: TextSelectionGuard | null) => {
      textSelectionGuardRef.current = guard;
    },
    [],
  );
  const pendingCreateRef = useRef<string | null>(null);
  const createRequestsRef = useRef<Map<string, {
    scopeKey: string;
    cwdSource: "default" | "inherited" | "explicit";
  }>>(new Map());
  const pendingBtwByParentRef = useRef<Map<string, string>>(new Map());
  const pendingSessionForkRef = useRef<PendingSessionFork | null>(null);
  const pendingWorktreeForkRef = useRef<PendingWorktreeFork | null>(null);
  const pendingSessionMigrationRef =
    useRef<PendingSessionMigration | null>(null);
  const sessionListsBySurfaceRef = useRef<Record<string, SessionInfo[]>>({});
  const historySessionListsRef = useRef<Record<string, SessionInfo[]>>({});
  // Cached lists are paint-only during a surface switch. A surface may choose
  // its remembered/latest focus only after a fresh wrapper list is accepted.
  const authoritativeSurfaceListsRef = useRef<Set<string>>(new Set());
  const sessionActivityPendingRef = useRef<Set<string>>(new Set());
  const deferredStatusRefreshRef = useRef<Set<string>>(new Set());
  const prefetchedSurfacesRef = useRef<Set<string>>(new Set());
  const lastFocusBySurfaceRef = useRef<Record<string, string>>({});
  const preferredSurfaceFocusRef = useRef<{ key: string; sid: string } | null>(null);
  const activeBtwByParentRef = useRef<Map<
    string, { requestId: string; sid: string }
  >>(new Map());
  // Retain recently cancelled ids so a late response can be identified and
  // discarded (and a late successful fork can be closed) without disturbing a
  // newer opening spinner. Bounded because a peer may disappear permanently.
  const btwRequestParentsRef = useRef<Map<string, string>>(new Map());
  const discardedBtwSidsRef = useRef<Set<string>>(new Set());
  // A marker may arrive while its session is in the background and while an
  // IndexedDB read is already in flight. The set blocks new cache use; the
  // epoch rejects reads that started before the destructive mutation.
  // sid -> exact revision required by a rollback marker. null means a replay
  // gap hid the revision, so the next authoritative first page may satisfy it.
  const historyInvalidationsRef = useRef<Map<string, string | null>>(new Map());
  const historyInvalidationGenerationsRef =
    useRef<Map<string, string | null>>(new Map());
  const historyCacheEpochRef = useRef<Map<string, number>>(new Map());
  const previousMachineRef = useRef(machineId);
  const notificationListRequestRef = useRef<string | null>(null);
  const notificationOriginRef = useRef<NotificationOrigin | null>(null);
  const pendingNotificationErrorRef = useRef<string | null>(null);
  const touchStartX = useRef(0);
  const touchStartY = useRef(0);
  const touchSwipeLocked = useRef(false);
  const artifactDirtyRef = useRef(false);
  const setArtifactDirty = useCallback((dirty: boolean) => {
    artifactDirtyRef.current = dirty;
  }, []);
  const confirmArtifactDiscard = useCallback(() => {
    if (!artifactDirtyRef.current) return true;
    if (!window.confirm("Markdown 有未保存的修改，确定放弃吗？")) return false;
    artifactDirtyRef.current = false;
    return true;
  }, []);
  const setBtwOpeningFor = useCallback((parentSid: string, opening: boolean) => {
    setBtwOpeningByParentSid((current) => {
      if (opening) {
        return current[parentSid] ? current : { ...current, [parentSid]: true };
      }
      if (!current[parentSid]) return current;
      const next = { ...current };
      delete next[parentSid];
      return next;
    });
  }, []);
  const requestHistory = useCallback((
    sid: string,
    before: string | null | undefined,
    limit: number,
    generation?: string | null,
    revision?: string | null,
    browse?: HistoryBrowseRequestContext,
  ) => {
    const ws = wsRef.current;
    if (!ws) return false;
    return historyRequestsRef.current.request({
      sid, before, limit,
      generation: generation ?? ws.generationFor(sid),
      revision,
      browse,
    }, () => ws.sendGetHistory(
      sid,
      before,
      limit,
      resolveHistoryCwdHint(historySessionListsRef.current, sid),
    ));
  }, []);
  const cancelPendingNotificationTarget = useCallback(() => {
    setPendingNotificationTarget(null);
    notificationListRequestRef.current = null;
    notificationOriginRef.current = null;
    try { sessionStorage.removeItem(NOTIFICATION_TARGET_KEY); }
    catch { /* private browsing storage may be unavailable */ }
  }, []);
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

  useEffect(() => {
    const current = reconcileOpenMigrationSession(
      migrateSession,
      state.sessions,
      migrateCreating,
    );
    if (current === migrateSession) return;
    setMigrateSession(current);
    setMigrateError(null);
  }, [migrateCreating, migrateSession, state.sessions]);

  useEffect(() => {
    const previous = previousMachineRef.current;
    if (previous === machineId) return;
    previousMachineRef.current = machineId;
    localStorage.setItem(MACHINE_KEY, machineId);
    pendingCreateRef.current = null;
    createRequestsRef.current.clear();
    pendingSessionMigrationRef.current = null;
    setMigrateSession(null);
    setMigrateCreating(false);
    setMigrateError(null);
    pendingBtwByParentRef.current.clear();
    activeBtwByParentRef.current.clear();
    btwRequestParentsRef.current.clear();
    discardedBtwSidsRef.current.clear();
    setBtwOpeningByParentSid({});
    setBtwSendModeBySid({});
    setQueuedQueryEditor(null);
    btwDraftsRef.current.clear();
    setCompletionReceipts({});
    sessionListsBySurfaceRef.current = {};
    historySessionListsRef.current = {};
    authoritativeSurfaceListsRef.current.clear();
    notificationListRequestRef.current = null;
    sessionActivityPendingRef.current.clear();
    skillCatalogsRef.current = {};
    skillCatalogRequestsRef.current?.reset();
    setSkillCatalogs({});
    setCapabilitiesByScope({});
    deferredStatusRefreshRef.current.clear();
    historyRequestsRef.current.clear();
    clearHistoryDetailRequests();
    historyPageScopesRef.current.clear();
    prefetchedSurfacesRef.current.clear();
    historyInvalidationsRef.current.clear();
    historyInvalidationGenerationsRef.current.clear();
    historyCacheEpochRef.current.clear();
    inlineImageAssetsRef.current.clear();
    historyImageAssetsRef.current.clear();
    bumpInlineImageRevision();
    bumpHistoryImageRevision();
    dispatch({ type: "reset" });
    if (pendingNotificationErrorRef.current) {
      dispatch({
        type: "command_error",
        detail: pendingNotificationErrorRef.current,
      });
      pendingNotificationErrorRef.current = null;
    }
    void import("./cache").then((module) => module.clearCache());
    void historyPageCacheRef.current.clear();
  }, [clearHistoryDetailRequests, machineId]);

  // The focused session's runtime (turns/state/model/perm/queue/...). Falls back
  // to an empty runtime before any session is focused.
  const focusedSid = state.focusedSid;
  const visibleParentSid = state.newChat ? null : focusedSid;
  const activeBtw = visibleParentSid ? state.btwByParentSid[visibleParentSid] : undefined;
  const activeBtwSid = activeBtw?.sid ?? null;
  const btwOpening = visibleParentSid
    ? !!btwOpeningByParentSid[visibleParentSid] : false;
  const completionBadges = Object.fromEntries(
    Object.entries(completionReceipts).flatMap(([sid, receipt]) => {
      const kind = completionBadgeKind(receipt);
      return kind ? [[sid, kind]] : [];
    }),
  ) as Record<string, CompletionBadgeKind>;
  const activeScopeKey = sessionScopeKey(machineId, engine, space);
  const currentCwd = state.cwdByScope[activeScopeKey] ?? "";
  const newChatCwd = state.newChat?.cwd ?? null;
  const newChatDefaults = resolveNewChatLocalDefaults(
    engine,
    space,
    newChatCwd ?? "",
    state.catalogDefault,
    state.catalogDefaultEffort,
    state.catalogDefaultCwd,
  );
  const rt = state.runtimes[focusedSid ?? ""] ?? createRuntime();
  const historyView = displayHistoryProjection(
    state.historyRecovery, focusedSid, rt, state.historyBrowse);
  const focusedSession = state.sessions.find(
    (session) => session.session_id === focusedSid);
  const focusedEngine = (focusedSession?.engine ?? engine) as "claude" | "codex";
  const capabilityCwd = focusedSession?.cwd || currentCwd;
  const focusedComposerDraftKey = composerDraftKey(
    machineId, space, focusedEngine, focusedSid ?? "",
  );
  const focusedSkillCatalogKey = skillCatalogKey(
    machineId, focusedEngine, space, capabilityCwd);
  focusedSkillScopeRef.current = {
    key: focusedSkillCatalogKey,
    engine: focusedEngine,
    space,
    cwd: capabilityCwd,
    skillsOnly: true,
  };
  const activeBtwDraftKey = composerDraftKey(
    machineId, space,
    (activeBtw?.engine === "codex" ? "codex" : "claude"),
    `btw:${activeBtwSid ?? visibleParentSid ?? "opening"}`,
  );
  const inlineImageAssets = focusedSid
    ? inlineImageAssetsRef.current.forSession(focusedSid) : {};
  const historyImageAssets = focusedSid
    ? historyImageAssetsRef.current.forSession(focusedSid) : {};
  const currentWorkArtifacts = focusedSid ? (workArtifactsBySid[focusedSid] ?? []) : [];
  const unconfirmedQueued = collectUnconfirmedQueries(state.runtimes);
  const unconfirmedReplaceable = collectUnconfirmedQueries(
    state.runtimes, focusedSid);
  const btwUnconfirmedReplaceable = collectUnconfirmedQueries(
    state.runtimes, activeBtwSid);
  const queueCapacity = deferredQueueCapacity(state);
  const replaceQueueCapacity = deferredQueueCapacity(state, focusedSid);
  const btwReplaceQueueCapacity = deferredQueueCapacity(
    state, activeBtwSid);
  const activeBtwSendMode = activeBtwSid
    ? btwSendModeBySid[activeBtwSid] ?? "steer" : "steer";

  useEffect(() => {
    setQueuedQueryEditor((current) => (
      current
      && current.sid !== focusedSid
      && current.sid !== activeBtwSid
        ? null
        : current
    ));
  }, [activeBtwSid, focusedSid]);

  useEffect(() => {
    const acknowledgeVisible = () => {
      if (document.hidden) return;
      const current = stateRef.current;
      const parentSid = current.newChat ? null : current.focusedSid;
      if (!parentSid) return;
      const binding = current.btwByParentSid[parentSid];
      setCompletionReceipts((receipts) => {
        let next = acknowledgeCompletion(
          receipts, parentSid, { main: true });
        if (binding
            && (rightViewRef.current === "btw" || !current.artifact)) {
          next = acknowledgeCompletion(
            next, parentSid, { btwSid: binding.sid });
        }
        return next;
      });
    };
    acknowledgeVisible();
    document.addEventListener("visibilitychange", acknowledgeVisible);
    return () => document.removeEventListener(
      "visibilitychange", acknowledgeVisible);
  }, [visibleParentSid, activeBtwSid, rightView, state.artifact]);

  const goalUi = focusedSid ? goalUiBySid[focusedSid] : undefined;
  const storeSkillCatalog = useCallback((
    key: string,
    items: EngineCapabilityItem[],
  ) => {
    const next = cacheSkillCatalog(
      skillCatalogsRef.current, key, items);
    skillCatalogsRef.current = next;
    setSkillCatalogs(next);
  }, []);
  const requestSkillCatalog = useCallback((
    request: SkillCatalogRequest,
    force = false,
  ) => {
    const cached = skillCatalogsRef.current[request.key];
    if (request.skillsOnly && !force && skillCatalogFresh(cached)) return false;
    return skillCatalogRequestsRef.current?.request(request) ?? false;
  }, []);
  const acceptSkillCatalog = useCallback((msg: EngineCapabilities) => {
    const coordinator = skillCatalogRequestsRef.current;
    if (!coordinator) return;
    const accepted = coordinator.accept(msg);
    if (!accepted) return;
    const matchedScope = accepted.request;
    if (!accepted.superseded && skillCatalogRefreshSucceeded(msg)) {
      storeSkillCatalog(
        matchedScope.key,
        msg.items.filter((item) => item.kind === "skill"));
    }
    if (!accepted.superseded && !msg.skills_only) {
      setCapabilitiesByScope((current) => ({
        ...current,
        [matchedScope.key]: msg,
      }));
    }
    if (focusedSkillScopeRef.current?.key === matchedScope.key
        && !coordinator.hasPendingMutation(matchedScope.key)
        && !coordinator.hasPendingRead(matchedScope.key, false)) {
      setCapabilitiesLoading(false);
    }
  }, [storeSkillCatalog]);
  const trackCapabilityMutation = useCallback((
    requestId: string | null | undefined,
    request: SkillCatalogRequest,
  ) => {
    if (!skillCatalogRequestsRef.current?.trackMutation(requestId, request)) {
      setCapabilitiesLoading(false);
    }
  }, []);
  const loadMessageImage = useCallback((sid: string, path: string): boolean => {
    const ws = wsRef.current;
    if (!ws || stateRef.current.focusedSid !== sid) return false;
    const cache = inlineImageAssetsRef.current;
    if (cache.has(sid, path)) return true;
    const previewId = uuid();
    const requestId = uuid();
    if (!cache.begin({ sid, path, previewId, requestId })) return false;
    if (!ws.sendGetPreviewAsset(path, previewId, requestId)) {
      cache.cancel(requestId);
      return false;
    }
    bumpInlineImageRevision();
    return true;
  }, []);
  const loadFocusedMessageImage = useCallback((path: string) => (
    focusedSid ? loadMessageImage(focusedSid, path) : false
  ), [focusedSid, loadMessageImage]);
  const loadHistoryImage = useCallback((
    turnId: string,
    imageId: string,
    variant: "thumbnail" | "full",
  ): boolean => {
    const sid = stateRef.current.focusedSid;
    const ws = wsRef.current;
    if (!sid || !ws) return false;
    const revision = stateRef.current.runtimes[sid]?.historyRevision;
    if (!revision) return false;
    const cache = historyImageAssetsRef.current;
    if (cache.has(sid, turnId, imageId, variant)) return true;
    const requestId = uuid();
    if (!cache.begin({
      sid, turnId, imageId, variant, requestId, revision,
    })) return false;
    if (!ws.sendGetHistoryImage(
      sid, turnId, imageId, variant, requestId, revision,
    )) {
      cache.cancel(requestId);
      return false;
    }
    bumpHistoryImageRevision();
    return true;
  }, []);

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

  useEffect(() => {
    if (!authed) {
      setRemoteDevices([]);
      setDevicesLoadState("idle");
      return;
    }
    setDevicesLoadState("loading");
    let cancelled = false;
    void fetch("/api/devices", {
      credentials: "same-origin", cache: "no-store",
    }).then(async (response) => response.ok ? response.json() : null)
      .then((payload) => {
        if (cancelled) return;
        if (!payload || !Array.isArray(payload.devices)) {
          setDevicesLoadState("error");
          return;
        }
        const validDevices: RemoteDevice[] = (payload.devices as unknown[]).filter((value: unknown): value is RemoteDevice => {
          if (!value || typeof value !== "object") return false;
          const item = value as Partial<RemoteDevice>;
          return typeof item.machine_id === "string"
            && /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$/.test(item.machine_id)
            && typeof item.label === "string" && typeof item.online === "boolean";
        });
        const available = validDevices.map((device) => device.machine_id);
        setRemoteDevices(validDevices);
        setDevicesLoadState("ready");
        setDevicePairing(payload.pairing ?? { enabled: false, expires_at: null });
        if (available.length && !available.includes(machineId)) {
          setMachineId(available[0]);
        }
      }).catch(() => {
        if (!cancelled) setDevicesLoadState("error");
      });
    return () => { cancelled = true; };
  }, [authed, machineId]);

  useEffect(() => {
    if (!("serviceWorker" in navigator)) return;
    const onMessage = (event: MessageEvent) => {
      if (event.data?.type !== "cc-remote-notification") return;
      const target = parseNotificationRoute(event.data.route);
      if (!target) return;
      notificationListRequestRef.current = null;
      notificationOriginRef.current = null;
      try { storeNotificationTarget(sessionStorage, target); }
      catch { /* retain the in-memory route below */ }
      setPendingNotificationTarget(target);
    };
    navigator.serviceWorker.addEventListener("message", onMessage);
    return () => navigator.serviceWorker.removeEventListener("message", onMessage);
  }, []);

  useEffect(() => pushCoordinatorRef.current?.subscribe(setPushBinding), []);

  useEffect(() => {
    const enabled = notificationMode !== "off"
      && typeof Notification !== "undefined"
      && Notification.permission === "granted";
    void pushCoordinatorRef.current?.setTarget(
      authed && enabled
        ? { machineId, mode: notificationMode }
        : null,
    );
  }, [authed, machineId, notificationMode]);

  // Swipe right -> open sidebar, swipe left -> close (mobile). Interactive
  // vertical scrollers opt out so a diagonal scroll never becomes navigation.
  const onTouchStart = (e: TouchEvent) => {
    const touch = e.touches[0];
    touchStartX.current = touch.clientX;
    touchStartY.current = touch.clientY;
    touchSwipeLocked.current = e.target instanceof Element
      && !!e.target.closest("[data-lock-horizontal-swipe]");
  };
  const onTouchEnd = (e: TouchEvent) => {
    const touch = e.changedTouches[0];
    const action = resolveSidebarSwipe(
      touchStartX.current,
      touchStartY.current,
      touch.clientX,
      touch.clientY,
      window.innerWidth,
      touchSwipeLocked.current,
    );
    if (action === "open") setSidebarOpen(true);
    else if (action === "close") setSidebarOpen(false);
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
  const spaceRef = useRef(space);
  spaceRef.current = space;
  const historyPageScopeFor = useCallback((
    sid: string,
    revision: string,
    fallbackEngine: Engine = engineRef.current,
    fallbackSpace: Space = spaceRef.current,
  ): HistoryPageCacheScope => {
    const listed = stateRef.current.sessions.find(
      (session) => session.session_id === sid)
      ?? Object.values(historySessionListsRef.current)
        .flat()
        .find((session) => session.session_id === sid);
    const scope: HistoryPageCacheScope = {
      machineId,
      engine: listed?.engine === "codex" || listed?.engine === "claude"
        ? listed.engine : fallbackEngine,
      space: listed?.space === "work" || listed?.space === "code"
        ? listed.space : fallbackSpace,
      sid,
      revision,
    };
    historyPageScopesRef.current.set(historyPageCacheScopeKey(scope), scope);
    return scope;
  }, [machineId]);
  const invalidateHistoryPageScopes = useCallback((sid: string) => {
    const scopes = new Map<string, HistoryPageCacheSessionScope>();
    for (const scope of historyPageScopesRef.current.values()) {
      if (scope.machineId !== machineId || scope.sid !== sid) continue;
      const sessionScope: HistoryPageCacheSessionScope = {
        machineId: scope.machineId,
        engine: scope.engine,
        space: scope.space,
        sid: scope.sid,
      };
      scopes.set(JSON.stringify(sessionScope), sessionScope);
    }
    for (const [surfaceKey, sessions] of Object.entries(
      historySessionListsRef.current)) {
      if (!sessions.some((session) => session.session_id === sid)) continue;
      const [listedSpace, listedEngine] = surfaceKey.split(":");
      if ((listedEngine !== "claude" && listedEngine !== "codex")
          || (listedSpace !== "code" && listedSpace !== "work")) continue;
      const sessionScope: HistoryPageCacheSessionScope = {
        machineId,
        engine: listedEngine,
        space: listedSpace,
        sid,
      };
      scopes.set(JSON.stringify(sessionScope), sessionScope);
    }
    if (stateRef.current.focusedSid === sid) {
      const sessionScope: HistoryPageCacheSessionScope = {
        machineId,
        engine: engineRef.current,
        space: spaceRef.current,
        sid,
      };
      scopes.set(JSON.stringify(sessionScope), sessionScope);
    }
    for (const scope of scopes.values()) {
      void historyPageCacheRef.current.invalidateScope(scope);
    }
    for (const [key, scope] of historyPageScopesRef.current) {
      if (scope.machineId === machineId && scope.sid === sid) {
        historyPageScopesRef.current.delete(key);
      }
    }
  }, [machineId]);
  const installBrowseHistoryPage = useCallback((
    history: History,
    waiters: readonly HistoryBrowseRequestContext[],
  ) => {
    const turns = summaryHistoryTurns(history);
    if (!history.before || !turns) return;
    for (const waiter of waiters) {
      const browse = stateRef.current.historyBrowse;
      if (!browse
          || stateRef.current.focusedSid !== history.session_id
          || browse.sid !== history.session_id
          || browse.scopeKey !== waiter.scopeKey
          || browse.revision !== history.revision
          || browse.viewId !== waiter.viewId
          || browse.windowEpoch !== waiter.windowEpoch
          || browse.olderCursor !== waiter.pendingBefore
          || waiter.pendingBefore !== history.before
          || (history.generation != null
            && browse.generation !== history.generation)) continue;
      const scope = historyPageScopesRef.current.get(waiter.scopeKey);
      if (!scope) continue;
      const page: HistoryBrowsePage = {
        pageKey: historyPageKey(history.before),
        turns,
        hasOlder: history.has_more,
        olderCursor: history.oldest_id
          ?? (turns[0] ? canonicalTurnId(turns[0]) : null),
        hasNewer: !!waiter.sourcePageKey,
        newerPageKey: waiter.sourcePageKey,
        isLatest: false,
      };
      const protectedTurnIds = protectedHistoryTurnIds(
        waiter.anchorTurnId,
        textSelectionGuardRef.current,
        {
          sid: history.session_id,
          revision: history.revision,
          viewId: waiter.viewId,
          scopeKey: waiter.scopeKey,
        },
      );
      const mutation = prependOlderPage(browse, page, {
        expectedScopeKey: waiter.scopeKey,
        expectedViewId: waiter.viewId,
        expectedWindowEpoch: waiter.windowEpoch,
        expectedOlderCursor: waiter.pendingBefore,
        protectedTurnIds,
      });
      if (mutation.projection === browse) continue;
      // Paint the received page immediately. Page-cache writes are best-effort
      // acceleration and must never hold the visible history response behind
      // IndexedDB quota/LRU work. A later cache miss keeps this readable window
      // mounted and degrades the downward affordance to "return to latest".
      void historyPageCacheRef.current.putPage(scope, page);
      for (const evicted of mutation.evictedPages) {
        void historyPageCacheRef.current.putPage(scope, evicted);
      }
      dispatch({
        type: "install_history_browse_page",
        sid: history.session_id,
        scopeKey: waiter.scopeKey,
        revision: history.revision,
        generation: history.generation,
        viewId: waiter.viewId,
        windowEpoch: waiter.windowEpoch,
        before: waiter.pendingBefore,
        page,
        protectedTurnIds,
        prepared: {
          from: browse,
          to: mutation.projection,
        },
      });
    }
  }, []);
  useEffect(() => {
    document.documentElement.setAttribute("data-engine", engine);
    localStorage.setItem(ENGINE_KEY, engine);
    wsRef.current?.setSurface(engine, space);
    wsRef.current?.sendListSessions(engine, space);
    if (space === "work") wsRef.current?.sendGetWorkDashboard(engine);
  }, [engine, space]);
  useEffect(() => {
    document.documentElement.setAttribute("data-space", space);
    localStorage.setItem(SPACE_KEY, space);
  }, [space]);
  useEffect(() => {
    if (newChatCwd === null || state.connState !== "connected"
        || !state.wrapperOnline) return;
    const request = newChatCatalogRequest(
      engine, space, newChatCwd);
    if (!request) return;
    wsRef.current?.sendGetModels(request.engine, request.cwd);
  }, [
    engine,
    newChatCwd,
    space,
    state.connState,
    state.wrapperOnline,
  ]);
  useEffect(() => {
    if (!state.newChat) return;
    const reconciled = reconcileNewChatSelection(
      engine,
      state.newChat.model,
      state.newChat.effort,
      state.catalog,
      newChatDefaults.model,
    );
    if (reconciled.model === state.newChat.model
        && reconciled.effort === state.newChat.effort) return;
    dispatch({ type: "set_new_chat_selection", ...reconciled });
  }, [
    engine,
    newChatDefaults.model,
    state.catalog,
    state.newChat,
  ]);
  useEffect(() => {
    // TurnDetail has no request id. Any view navigation revokes the frozen
    // runtime/browse target so a delayed response cannot cross sessions.
    clearHistoryDetailRequests();
  }, [
    clearHistoryDetailRequests,
    machineId, engine, space, focusedSid, state.newChat,
  ]);
  const rememberSurfaceFocus = useCallback((
    currentEngine: Engine,
    currentSpace: Space,
  ) => {
    if (focusedSid && !state.newChat) {
      lastFocusBySurfaceRef.current[`${currentSpace}:${currentEngine}`] = focusedSid;
    }
  }, [focusedSid, state.newChat]);

  const prepareSurfaceSwitch = useCallback((
    nextEngine: Engine,
    nextSpace: Space,
    preserveAuthority = false,
  ) => {
    rememberSurfaceFocus(engine, space);
    const surfaceKey = `${nextSpace}:${nextEngine}`;
    if (!preserveAuthority) {
      authoritativeSurfaceListsRef.current.delete(surfaceKey);
    }
    dispatch({
      type: "restore_session_list",
      sessions: sessionListsBySurfaceRef.current[surfaceKey] ?? [],
    });
    const remembered = lastFocusBySurfaceRef.current[surfaceKey];
    preferredSurfaceFocusRef.current = preserveAuthority
      ? null
      : remembered ? { key: surfaceKey, sid: remembered } : null;
    didInitFocusRef.current = preserveAuthority;
    wsRef.current?.setSurface(nextEngine, nextSpace);
    wsRef.current?.setFocusedSid(null);
    // Keep the previous surface's transcript out of view while its accepted
    // list is restored. The focus effect below exits this temporary new page as
    // soon as the remembered (or latest valid) session is available.
    dispatch({ type: "enter_new_chat", cwd: "~", cwdSource: "default" });
    setNewChatAutoFocus(false);
  }, [engine, rememberSurfaceFocus, space]);

  const focusListedSession = useCallback((selected: SessionInfo) => {
    const selectedEngine: Engine = selected.engine === "codex"
      || selected.engine === "claude"
      ? selected.engine : engineRef.current;
    const selectedSpace: Space = selected.space === "work"
      || selected.space === "code"
      ? selected.space : spaceRef.current;
    const id = selected.session_id;
    pendingCreateRef.current = null;
    setCreateError(null);
    setStatusOpenSid(null);
    setWorkArtifactsOpen(false);
    dispatch({ type: "exit_new_chat" });
    dispatch({ type: "focus_session", sid: id });
    wsRef.current?.setFocusedSid(id, selectedEngine, selectedSpace);
    requestHistory(id, undefined, HISTORY_INITIAL_PAGE);
    wsRef.current?.sendSwitchSession(id, selectedEngine, selectedSpace);
    if (selectedSpace === "work") {
      wsRef.current?.sendGetWorkArtifacts(selectedEngine, id);
    }
    if (isMobile()) setSidebarOpen(false);
  }, [requestHistory]);

  useEffect(() => {
    const target = pendingNotificationTarget;
    if (!target || !authed) return;
    const surfaceKey = `${target.space}:${target.engine}`;
    if (notificationOriginRef.current === null) {
      notificationOriginRef.current = {
        machineId,
        engine,
        space,
        sid: state.focusedSid,
      };
      // A cached/preloaded catalog is paint-only here. Resolve every click
      // against a list requested after that click so a deleted or revoked
      // target cannot move focus.
      authoritativeSurfaceListsRef.current.delete(surfaceKey);
      notificationListRequestRef.current = null;
    }
    const navigation = resolveNotificationNavigation({
      target,
      origin: notificationOriginRef.current,
      deviceState: devicesLoadState,
      authorizedMachineIds: remoteDevices.map((device) => device.machine_id),
      machineId,
      engine,
      space,
      authoritativeSessions: authoritativeSurfaceListsRef.current.has(surfaceKey)
        ? sessionListsBySurfaceRef.current[surfaceKey] ?? []
        : null,
    });
    if (navigation.kind === "wait") return;
    if (navigation.kind === "fail") {
      const detail = navigation.reason === "devices_unavailable"
        ? "无法验证通知对应的设备，当前会话没有切换。"
        : navigation.reason === "device_missing"
          ? "通知对应的设备不存在或当前账号无权访问。"
          : "通知对应的会话已删除或不属于当前设备。";
      if (navigation.restore) {
        const origin = navigation.restore;
        pendingNotificationErrorRef.current = detail;
        setPendingNotificationTarget(null);
        notificationListRequestRef.current = null;
        notificationOriginRef.current = null;
        try { sessionStorage.removeItem(NOTIFICATION_TARGET_KEY); }
        catch { /* best-effort stale route cleanup */ }
        preferredSurfaceFocusRef.current = origin.sid
          ? { key: `${origin.space}:${origin.engine}`, sid: origin.sid }
          : null;
        didInitFocusRef.current = false;
        setEngine(origin.engine);
        setSpace(origin.space);
        setMachineId(origin.machineId);
      } else {
        dispatch({ type: "command_error", detail });
        cancelPendingNotificationTarget();
      }
      return;
    }
    if (navigation.kind === "switch_machine") {
      setMachineId(navigation.machineId);
      return;
    }
    if (navigation.kind === "request_list") {
      const ws = wsRef.current;
      if (!ws) return;
      const requestKey = `${machineId}:${surfaceKey}`;
      if (notificationListRequestRef.current !== requestKey) {
        if (ws.sendListSessions(navigation.engine, navigation.space)) {
          notificationListRequestRef.current = requestKey;
        }
      }
      return;
    }
    if (navigation.kind === "switch_surface") {
      pendingCreateRef.current = null;
      setCreateError(null);
      prepareSurfaceSwitch(navigation.engine, navigation.space, true);
      setEngine(navigation.engine);
      setSpace(navigation.space);
      return;
    }
    didInitFocusRef.current = true;
    focusListedSession(navigation.session);
    cancelPendingNotificationTarget();
  }, [
    authed,
    cancelPendingNotificationTarget,
    devicesLoadState,
    engine,
    focusListedSession,
    machineId,
    notificationListRevision,
    pendingNotificationTarget,
    prepareSurfaceSwitch,
    remoteDevices,
    space,
    state.focusedSid,
  ]);

  // Engine and Work/Code switches are navigation. Each surface restores the
  // session that was last open there instead of silently starting a new one.
  const toggleEngine = () => {
    cancelPendingNotificationTarget();
    const nextEngine: Engine = engine === "codex" ? "claude" : "codex";
    pendingCreateRef.current = null;
    setCreateError(null);
    setStatusOpenSid(null);
    setWorkArtifactsOpen(false);
    setWorkProjectId(null);
    prepareSurfaceSwitch(nextEngine, space);
    setEngine(nextEngine);
    if (isMobile()) setSidebarOpen(false);
  };

  const switchSpace = (next: Space) => {
    if (next === space || !confirmArtifactDiscard()) return;
    cancelPendingNotificationTarget();
    pendingCreateRef.current = null;
    setCreateError(null);
    setStatusOpenSid(null);
    setForkWorktreeSession(null);
    setForkWorktreeError(null);
    setWorkArtifactsOpen(false);
    prepareSurfaceSwitch(engine, next);
    setSpace(next);
  };

  // WebSocket lifecycle
  useEffect(() => {
    if (!authed) return;
    const historyRequests = historyRequestsRef.current;
    didInitFocusRef.current = false;  // re-arm initial-focus for this connection lifecycle
    authoritativeSurfaceListsRef.current.delete(`${spaceRef.current}:${engineRef.current}`);

    let cancelled = false;
    const recoverableReads = new RecoverableReadCoordinator(
      (callback, delayMs) => window.setTimeout(callback, delayMs),
      (timer) => window.clearTimeout(timer),
    );

    // A snapshot announces a session (cc_session_id/state/cwd). We do NOT reset
    // the cursor here anymore — cursors are seeded from the IndexedDB cache before
    // connecting, so hello asks the wrapper only for the DELTA instead of a full
    // history replay of every resident session (that flood wedged reconnect).
    function handleSnapshot(e: Snapshot, ownership?: EventOwnership) {
      dispatch({ type: "event", event: e, ownership });
    }

    (async () => {
      let seeded = { cursors: {} as Record<string, number>,
        generations: {} as Record<string, string>,
        controls: {} as Record<string, SessionControl> };
      try { seeded = await import("./cache").then((m) => m.loadAllReplayState()); } catch { /* best-effort */ }
      if (cancelled) return;
      const ws = new RelayWs({
        onEvent: (msg, ownership) => {
          if (msg.type === "preview_asset"
              && inlineImageAssetsRef.current.accept(msg)) {
            bumpInlineImageRevision();
          }
          if (msg.type === "history_image"
              && historyImageAssetsRef.current.accept(msg)) {
            bumpHistoryImageRevision();
          }
          if (msg.type === "queued_query_detail") {
            setQueuedQueryEditor((current) => (
              current
              && current.sid === msg.sid
              && current.msgId === msg.msg_id
              && current.detailRequestId === msg.request_id
                ? {
                    ...current,
                    prompt: msg.prompt ?? null,
                    kind: msg.kind ?? null,
                    imageCount: msg.image_count,
                    fileCount: msg.file_count,
                    loading: false,
                    error: msg.error ?? null,
                    detailRequestId: null,
                  }
                : current
            ));
            return;
          }
          if (msg.type === "queued_query_updated") {
            setQueuedQueryEditor((current) => {
              if (!current
                  || current.sid !== msg.sid
                  || current.msgId !== msg.msg_id
                  || current.updateRequestId !== msg.request_id) {
                return current;
              }
              if (!msg.updated) {
                return {
                  ...current,
                  saving: false,
                  error: msg.error ?? "排队消息修改失败。",
                  updateRequestId: null,
                  pendingPrompt: null,
                };
              }
              const prompt = current.pendingPrompt ?? current.prompt ?? "";
              return {
                ...current,
                preview: prompt.slice(0, 512),
                prompt,
                saving: false,
                error: null,
                updateRequestId: null,
                pendingPrompt: null,
              };
            });
            return;
          }
          if (msg.type === "query_queue" && msg.sid) {
            setQueuedQueryEditor((current) => {
              if (!current || current.sid !== msg.sid) return current;
              return msg.items.some(
                (item) => item.msg_id === current.msgId) ? current : null;
            });
          }
          if (msg.type === "session_rekey") {
            setQueuedQueryEditor((current) => (
              current?.sid === msg.old_key
                ? { ...current, sid: msg.session_id }
                : current
            ));
          }
          if (msg.type === "turn_end" && msg.sid && !msg.result.is_error) {
            const current = stateRef.current;
            const btwOwner = Object.entries(current.btwByParentSid).find(
              ([, binding]) => binding.sid === msg.sid,
            );
            const isBtw = !!btwOwner;
            // A closed/stale fork can still drain one final terminal frame.
            // Without an owner it must not appear as a fake top-level session.
            if (!msg.sid.startsWith("btw-") || isBtw) {
              const parentSid = btwOwner?.[0] ?? msg.sid;
              const sameVisibleParent = !document.hidden
                && !current.newChat
                && current.focusedSid === parentSid;
              const btwPanelVisible = isBtw
                && sameVisibleParent
                && (rightViewRef.current === "btw" || !current.artifact);
              const alreadySeen = isBtw ? btwPanelVisible : sameVisibleParent;
              if (!alreadySeen) {
                setCompletionReceipts((receipts) => markCompletionUnread(
                  receipts,
                  parentSid,
                  msg.sid!,
                  isBtw ? "btw" : "main",
                ));
              }
            }
          }
          if ((msg.type === "user_msg" || msg.type === "turn_steered"
                || msg.type === "turn_end") && msg.sid) {
            const activityMs = Math.round(msg.ts * 1000);
            let changed = false;
            for (const [key, listed] of Object.entries(
              sessionListsBySurfaceRef.current)) {
              const updated = bumpSessionActivity(listed, msg.sid, activityMs);
              if (updated !== listed) {
                sessionListsBySurfaceRef.current[key] = updated;
                changed = true;
              }
            }
            if ((msg.type === "user_msg" || msg.type === "turn_steered")
                && changed) {
              sessionActivityPendingRef.current.add(msg.sid);
            }
          }
          if (msg.type === "history_invalidated") {
            const sid = msg.session_id;
            clearHistoryDetailRequests();
            invalidateHistoryPageScopes(sid);
            if (inlineImageAssetsRef.current.dropSession(sid)) {
              bumpInlineImageRevision();
            }
            if (historyImageAssetsRef.current.dropSession(sid)) {
              bumpHistoryImageRevision();
            }
            if (historyInvalidationsRef.current.get(sid) !== msg.revision) {
              historyInvalidationsRef.current.set(sid, msg.revision);
              if (!historyInvalidationGenerationsRef.current.has(sid)) {
                historyInvalidationGenerationsRef.current.set(sid, null);
              }
              historyCacheEpochRef.current.set(
                sid, (historyCacheEpochRef.current.get(sid) ?? 0) + 1);
              void import("./cache").then((module) =>
                module.invalidateSessionCache(sid));
            }
            // The marker is deliberately tiny/replayable; the full replacement
            // is one-shot and may have been dropped by a disconnect or frame
            // size bound. Fetch immediately when visible; a background session
            // gets the same authoritative request when it is later focused.
            if (stateRef.current.focusedSid === sid) {
              requestHistory(
                sid, undefined, HISTORY_INITIAL_PAGE, undefined, msg.revision);
            }
          } else if (msg.type === "artifact_invalidated") {
            const sid = msg.session_id;
            setWorkArtifactsBySid((current) => {
              if (!(sid in current)) return current;
              const next = { ...current };
              delete next[sid];
              return next;
            });
            const session = stateRef.current.sessions.find(
              (candidate) => candidate.session_id === sid);
            if (session?.space === "work"
                || (spaceRef.current === "work"
                  && stateRef.current.focusedSid === sid)) {
              ws.sendGetWorkArtifacts(
                (session?.engine as Engine | undefined) ?? engineRef.current,
                sid,
              );
            }
          } else if (msg.type === "replay_start" && msg.sid
              && (msg.truncated || msg.rebuild)) {
            const sid = msg.sid;
            clearHistoryDetailRequests();
            invalidateHistoryPageScopes(sid);
            historyInvalidationGenerationsRef.current.set(
              sid, msg.generation ?? null);
            // If a marker is still retained inside this replay it will follow
            // ReplayStart and replace null with its exact revision.
            if (!historyInvalidationsRef.current.has(sid)) {
              historyInvalidationsRef.current.set(sid, null);
              historyCacheEpochRef.current.set(
                sid, (historyCacheEpochRef.current.get(sid) ?? 0) + 1);
              void import("./cache").then((module) =>
                module.invalidateSessionCache(sid));
            }
            setWorkArtifactsBySid((current) => {
              if (!(sid in current)) return current;
              const next = { ...current };
              delete next[sid];
              return next;
            });
            if (stateRef.current.focusedSid === sid) {
              requestHistory(
                sid, undefined, HISTORY_INITIAL_PAGE, msg.generation);
            }
          } else if (msg.type === "history" && msg.authoritative !== false && !msg.before
              && historyInvalidationsRef.current.has(msg.session_id)) {
            const expected = historyInvalidationsRef.current.get(msg.session_id);
            const expectedGeneration =
              historyInvalidationGenerationsRef.current.get(msg.session_id) ?? null;
            const recovery = stateRef.current.historyRecovery;
            const currentRuntime =
              stateRef.current.runtimes[msg.session_id];
            const displayRecoveryMatches = !isHistoryRecoveryPending(
              recovery, msg.session_id)
              || historyConfirmsRecovery(recovery, msg);
            const runtimeRecoveryMatches =
              !isRuntimeHistoryRecoveryPending(currentRuntime)
              || historyConfirmsRuntimeRecovery(currentRuntime, msg);
            const sameBuildGeneration = msg.generation != null
              ? currentRuntime?.historyGeneration === msg.generation
              : currentRuntime?.historyGeneration == null
                && currentRuntime?.historyRevision === msg.revision;
            const buildIsCurrent = msg.build_seq == null
              || !sameBuildGeneration
              || msg.build_seq >= (currentRuntime?.historyBuildSeq ?? 0);
            if ((expected === null || expected === msg.revision)
                && (expectedGeneration === null
                  || expectedGeneration === msg.generation)
                && displayRecoveryMatches
                && runtimeRecoveryMatches
                && buildIsCurrent) {
              // A late first page from an older revision must not re-enable the
              // cache behind a newer destructive marker.
              historyInvalidationsRef.current.delete(msg.session_id);
              historyInvalidationGenerationsRef.current.delete(msg.session_id);
              void import("./cache").then((module) =>
                module.allowSessionCache(msg.session_id));
            }
          }
          if (msg.type === "history") {
            const browseWaiters =
              historyRequestsRef.current.complete(msg);
            const retryKey = ["history", msg.session_id, msg.before ?? "",
              msg.revision ?? ""].join("\u0000");
            let retryScheduled = false;
            if (msg.authoritative === false) {
              const retryDelay = msg.error == null && !msg.before
                ? HISTORY_PROVISIONAL_WATCHDOG_MS
                : undefined;
              retryScheduled = recoverableReads.retry(retryKey, () => {
                if (cancelled) return;
                if (stateRef.current.focusedSid !== msg.session_id) return;
                if (msg.before) {
                  for (const browse of browseWaiters) {
                    requestHistory(
                      msg.session_id, msg.before, HISTORY_MORE_PAGE,
                      msg.generation, msg.revision, browse);
                  }
                } else {
                  requestHistory(
                    msg.session_id, undefined, HISTORY_INITIAL_PAGE,
                    msg.generation, msg.revision);
                }
              }, retryDelay);
            } else {
              recoverableReads.complete(retryKey);
            }
            if (msg.before) {
              if (msg.authoritative !== false) {
                void installBrowseHistoryPage(msg, browseWaiters);
              } else if (!retryScheduled) {
                for (const browse of browseWaiters) {
                  dispatch({
                    type: "history_browse_page_failed",
                    sid: msg.session_id,
                    scopeKey: browse.scopeKey,
                    revision: msg.revision,
                    generation: msg.generation,
                    viewId: browse.viewId,
                    windowEpoch: browse.windowEpoch,
                    before: browse.pendingBefore,
                  });
                }
              }
              // History.before is display-only and requester-correlated. It
              // must never fall through to the generic runtime reducer.
              return;
            }
            const needsRecoveryConfirmation =
              historyNeedsConfirmationRequest(
                stateRef.current.runtimes[msg.session_id], msg);
            if (needsRecoveryConfirmation
                && stateRef.current.focusedSid === msg.session_id
                && msg.generation) {
              requestHistory(
                msg.session_id, undefined, HISTORY_INITIAL_PAGE,
                msg.generation);
            }
          }
          if (msg.type === "turn_detail") {
            const detailTarget =
              historyDetailRequestsRef.current.complete(msg);
            if (!detailTarget) return;
            const retryKey = [
              "detail", msg.session_id, msg.revision, msg.turn_id,
              msg.before ?? "",
            ].join("\u0000");
            if (msg.authoritative === false) {
              if (detailTarget.target === "browse") {
                dispatch({
                  type: "history_browse_detail",
                  sid: detailTarget.sid,
                  scopeKey: detailTarget.scopeKey,
                  revision: detailTarget.revision,
                  viewId: detailTarget.viewId,
                  windowEpoch: detailTarget.windowEpoch,
                  turnId: detailTarget.turnId,
                  events: [],
                  before: detailTarget.before,
                });
              } else {
                dispatch({ type: "event", event: msg, ownership });
              }
              recoverableReads.retry(retryKey, () => {
                if (cancelled) return;
                if (stateRef.current.focusedSid !== msg.session_id) return;
                const current = stateRef.current;
                if (detailTarget.target === "browse") {
                  const browse = current.historyBrowse;
                  if (!browse
                      || browse.scopeKey !== detailTarget.scopeKey
                      || browse.viewId !== detailTarget.viewId
                      || browse.revision !== detailTarget.revision
                      || !browse.turns.some((turn) =>
                        canonicalTurnId(turn) === detailTarget.turnId
                        || turn.id === detailTarget.turnId)) return;
                } else {
                  const runtime = current.runtimes[msg.session_id];
                  const turn = runtime?.turns.find(
                    (item) => canonicalTurnId(item) === msg.turn_id
                      || item.id === msg.turn_id);
                  if (!turn || (!detailTarget.before && turn.detailLoaded)
                      || runtime.historyRevision !== detailTarget.revision) return;
                }
                if (!historyDetailRequestsRef.current.begin(detailTarget)) return;
                const sent = ws.sendGetTurnDetail(
                  msg.session_id, msg.turn_id, detailTarget.revision,
                  detailTarget.before);
                if (!sent) {
                  historyDetailRequestsRef.current.cancel(detailTarget);
                  return;
                }
                if (detailTarget.target === "browse") {
                  dispatch({
                    type: "history_browse_detail_requested",
                    sid: detailTarget.sid,
                    scopeKey: detailTarget.scopeKey,
                    revision: detailTarget.revision,
                    viewId: detailTarget.viewId,
                    windowEpoch: detailTarget.windowEpoch,
                    turnId: detailTarget.turnId,
                    before: detailTarget.before,
                  });
                } else {
                  dispatch({
                    type: "turn_detail_requested", sid: detailTarget.sid,
                    turnId: detailTarget.turnId,
                    before: detailTarget.before,
                    autoLoad: detailTarget.autoLoad,
                  });
                }
              });
              return;
            } else {
              recoverableReads.complete(retryKey);
            }
            if (detailTarget.target === "browse") {
              dispatch({
                type: "history_browse_detail",
                sid: detailTarget.sid,
                scopeKey: detailTarget.scopeKey,
                revision: detailTarget.revision,
                viewId: detailTarget.viewId,
                windowEpoch: detailTarget.windowEpoch,
                turnId: detailTarget.turnId,
                events: msg.events,
                before: msg.before,
                hasMore: msg.has_more,
                oldestCursor: msg.oldest_cursor,
                hasNewer: msg.has_newer,
                newerCursor: msg.newer_cursor,
              });
              return;
            }
          }
          if (msg.type === "rollback_result" && msg.files === "succeeded"
              && stateRef.current.artifact?.sid === msg.session_id) {
            // Diff/file previews are snapshots of bytes that have just changed.
            // Close them instead of leaving a convincing but stale panel open.
            dispatch({ type: "clear_artifact" });
          }
          if (msg.type === "rollback_result" && msg.prefill_text
              && stateRef.current.focusedSid === msg.session_id) {
            setEditPrompt(msg.prefill_text);
          }
          if (msg.type === "btw_opened") {
            const requestedParent = btwRequestParentsRef.current.get(
              msg.request_id) ?? null;
            const pendingRequestId = requestedParent
              ? pendingBtwByParentRef.current.get(requestedParent) ?? null
              : null;
            const activeRequest = activeBtwByParentRef.current.get(
              msg.parent_sid)
              ?? (requestedParent
                ? activeBtwByParentRef.current.get(requestedParent) : undefined)
              ?? null;
            const disposition = classifyBtwOpened(
              pendingRequestId, activeRequest, msg);
            if (disposition === "duplicate") {
              btwRequestParentsRef.current.delete(msg.request_id);
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
              btwRequestParentsRef.current.delete(msg.request_id);
              ws.sendCloseBtw(msg.btw_sid);
              return;
            }
            if (requestedParent) {
              pendingBtwByParentRef.current.delete(requestedParent);
              activeBtwByParentRef.current.delete(requestedParent);
              setBtwOpeningFor(requestedParent, false);
            }
            btwRequestParentsRef.current.delete(msg.request_id);
            activeBtwByParentRef.current.set(msg.parent_sid, {
              requestId: msg.request_id,
              sid: msg.btw_sid,
            });
            setBtwOpeningFor(msg.parent_sid, false);
          } else if (msg.type === "error" && msg.request_id
              && btwRequestParentsRef.current.has(msg.request_id)) {
            const parentSid = btwRequestParentsRef.current.get(msg.request_id)!;
            const matches = matchesBtwRequest(
              pendingBtwByParentRef.current.get(parentSid) ?? null,
              msg.request_id);
            if (!matches) return; // obsolete /btw failure; keep any newer spinner
            pendingBtwByParentRef.current.delete(parentSid);
            btwRequestParentsRef.current.delete(msg.request_id);
            setBtwOpeningFor(parentSid, false);
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
            setSpace("code");
            dispatch({ type: "exit_new_chat" });
            dispatch({ type: "focus_session", sid: msg.session_id });
            ws.setSessionEngines([{ session_id: msg.session_id, engine: targetEngine, space: "code" }]);
            ws.setFocusedSid(msg.session_id, targetEngine, "code");
            ws.sendListSessions(targetEngine, "code");
            requestHistory(
              msg.session_id, undefined, HISTORY_INITIAL_PAGE);
            ws.sendSwitchSession(msg.session_id, targetEngine, "code");
            if (isMobile()) setSidebarOpen(false);
            return;
          }
          if (msg.type === "session_migrated"
              && matchesSessionMigrationRequest(
                pendingSessionMigrationRef.current,
                msg.request_id,
                msg.session_id,
              )) {
            pendingSessionMigrationRef.current = null;
            setMigrateCreating(false);
            setMigrateError(null);
            setMigrateSession(null);
          }
          if (msg.type === "error"
              && isTerminalSessionMigrationError(msg.code)
              && matchesSessionMigrationRequest(
                pendingSessionMigrationRef.current,
                msg.request_id,
              )) {
            pendingSessionMigrationRef.current = null;
            setMigrateCreating(false);
            setMigrateError(presentCommandProblem(msg));
            return;
          }
          if (msg.type === "error" && isTerminalWorktreeForkError(msg.code)
              && matchesSessionForkRequest(
                pendingSessionForkRef.current, msg.request_id)) {
            pendingSessionForkRef.current = null;
            setForkingPointId(null);
            dispatch({ type: "command_error", detail: presentCommandProblem(msg) });
            return;
          }
          if (msg.type === "error" && isTerminalWorktreeForkError(msg.code)
              && matchesWorktreeForkRequest(
              pendingWorktreeForkRef.current, msg.request_id)) {
            pendingWorktreeForkRef.current = null;
            setForkWorktreeCreating(false);
            setForkWorktreeError(presentCommandProblem(msg));
            return;
          }
          const createResponseRequestId = (msg.type === "session_focus"
              || msg.type === "error") ? msg.request_id : null;
          const createRequest = createResponseRequestId
            ? createRequestsRef.current.get(createResponseRequestId) : undefined;
          if (createRequest && (msg.type === "session_focus"
              || (msg.type === "error" && msg.code !== "wrapper_offline"))) {
            createRequestsRef.current.delete(createResponseRequestId!);
            if (createResponseRequestId !== pendingCreateRef.current) return;
            pendingCreateRef.current = null;
            const currentScopeKey = sessionScopeKey(
              machineId, engineRef.current, spaceRef.current);
            if (createRequest.scopeKey !== currentScopeKey) return;
            if (msg.type === "session_focus") {
              setCreateError(null);
              dispatch({ type: "exit_new_chat" });
            } else {
              if (msg.code === "invalid_cwd"
                  && createRequest.cwdSource === "inherited") {
                dispatch({
                  type: "clear_scope_cwd",
                  scopeKey: createRequest.scopeKey,
                });
                dispatch({
                  type: "set_new_chat_cwd",
                  cwd: "~",
                  cwdSource: "default",
                });
              }
              setCreateError(presentCommandProblem(msg));
              return;
            }
          }
          if (msg.type === "snapshot") {
            if (consumeDiscardedBtwSnapshot(discardedBtwSidsRef.current, msg)) return;
            handleSnapshot(msg, ownership);
            return;
          }
          if (msg.type === "permission_profiles" && !msg.sid && msg.cwd) {
            setNewChatPermissionCatalog({
              machineId,
              cwd: msg.cwd,
              profiles: msg.profiles,
            });
            return;
          }
          if (msg.type === "session_rekey") {
            setCompletionReceipts((current) => rekeyCompletionReceipts(
              current, msg.old_key, msg.session_id));
            const pendingBtwRequest = pendingBtwByParentRef.current.get(
              msg.old_key);
            if (pendingBtwRequest) {
              pendingBtwByParentRef.current.delete(msg.old_key);
              if (!pendingBtwByParentRef.current.has(msg.session_id)) {
                pendingBtwByParentRef.current.set(
                  msg.session_id, pendingBtwRequest);
                btwRequestParentsRef.current.set(
                  pendingBtwRequest, msg.session_id);
              }
              setBtwOpeningByParentSid((current) => {
                if (!current[msg.old_key]) return current;
                const next = { ...current };
                if (!next[msg.session_id]) next[msg.session_id] = true;
                delete next[msg.old_key];
                return next;
              });
            }
            const activeParentBtw = activeBtwByParentRef.current.get(
              msg.old_key);
            if (activeParentBtw) {
              activeBtwByParentRef.current.delete(msg.old_key);
              const targetParentBtw = activeBtwByParentRef.current.get(
                msg.session_id);
              if (!targetParentBtw) {
                activeBtwByParentRef.current.set(
                  msg.session_id, activeParentBtw);
              } else if (targetParentBtw.sid !== activeParentBtw.sid) {
                ws.sendCloseBtw(activeParentBtw.sid);
              }
            }
            if (ownership) {
              composerDraftsRef.current.rekey(
                composerDraftKey(
                  ownership.machineId, ownership.space, ownership.engine,
                  msg.old_key,
                ),
                composerDraftKey(
                  ownership.machineId, ownership.space, ownership.engine,
                  msg.session_id,
                ),
              );
            }
            setWorkArtifactsBySid((current) => {
              const prior = current[msg.old_key];
              if (!prior) return current;
              const next = { ...current, [msg.session_id]: prior };
              delete next[msg.old_key];
              return next;
            });
            if (stateRef.current.focusedSid === msg.old_key
                && ownership?.engine === engineRef.current
                && ownership.space === spaceRef.current) {
              // The reducer has already got enough correlated metadata to paint
              // a temp sidebar row. Rekey is the durability boundary: refresh
              // the active surface so its title/status comes from the native
              // catalog without making the user toggle or reload the page.
              ws.sendListSessions(engineRef.current, spaceRef.current);
              if (spaceRef.current === "work") {
                ws.sendGetWorkArtifacts(engineRef.current, msg.session_id);
              }
            }
            setGoalUiBySid((current) => {
              const prior = current[msg.old_key];
              if (!prior) return current;
              const next = { ...current, [msg.session_id]: prior };
              delete next[msg.old_key];
              return next;
            });
          }
          if (msg.type === "session_list" && ownership) {
            const listedSpace = msg.space ?? "code";
            historySessionListsRef.current[
              `${listedSpace}:${msg.engine}`
            ] = msg.sessions;
          }
          if (msg.type === "session_list") {
            ws.setSessionEngines(msg.sessions);
            const listedSpace = msg.space ?? "code";
            const surfaceKey = `${listedSpace}:${msg.engine}`;
            sessionListsBySurfaceRef.current[surfaceKey] = msg.sessions;
            authoritativeSurfaceListsRef.current.add(surfaceKey);
            bumpNotificationListRevision();
            prefetchedSurfacesRef.current.add(surfaceKey);
            // Warm the sibling Work/Code surface once per page lifetime. Codex
            // reuses the just-read native catalog in the wrapper, so this does
            // not start a second app-server and the user's first toggle is fast.
            const siblingSpace: Space = listedSpace === "work" ? "code" : "work";
            const siblingKey = `${siblingSpace}:${msg.engine}`;
            if (!prefetchedSurfacesRef.current.has(siblingKey)) {
              prefetchedSurfacesRef.current.add(siblingKey);
              ws.sendListSessions(msg.engine, siblingSpace);
            }
          }
          if (msg.type === "session_activity") {
            for (const [surfaceKey, listed] of Object.entries(
              sessionListsBySurfaceRef.current,
            )) {
              if (!surfaceKey.endsWith(`:${msg.engine}`)) continue;
              sessionListsBySurfaceRef.current[surfaceKey] = listed.map(
                (session) => session.session_id === msg.session_id
                  ? { ...session, state: msg.state }
                  : session,
              );
            }
          }
          if (msg.type === "state" && msg.sid && ownership) {
            sessionListsBySurfaceRef.current = updateScopedSessionLifecycle(
              sessionListsBySurfaceRef.current,
              ownership.engine,
              ownership.space,
              msg.sid,
              msg.state,
            );
          }
          if (msg.type === "work_dashboard") {
            setWorkDashboards((current) => ({ ...current, [msg.engine]: msg }));
            setWorkProjectId((current) => current && msg.projects.some(
              (project) => project.project_id === current) ? current : null);
          }
          if (msg.type === "work_artifacts") {
            setWorkArtifactsBySid((current) => ({
              ...current, [msg.session_id]: msg.artifacts,
            }));
          }
          if (msg.type === "engine_capabilities") {
            acceptSkillCatalog(msg);
          }
          if (msg.type === "turn_end" && msg.sid && msg.notification_context
              && document.hidden
              && typeof Notification !== "undefined"
              && Notification.permission === "granted") {
            const mode = readNotificationMode(localStorage);
            const remoteWillNotify = !msg.to
              && !!pushCoordinatorRef.current?.isRemoteActive(machineId);
            if (mode === "off" || remoteWillNotify) {
              // Web Push owns broadcast completions while /btw's owner-routed
              // TurnEnd remains a local-only notification.
            } else {
              const presentation = turnNotificationPresentation(msg, mode);
              const route = presentation.sessionId
                && presentation.engine && presentation.space
                ? {
                    machine_id: machineId,
                    session_id: presentation.sessionId,
                    engine: presentation.engine,
                    space: presentation.space,
                  }
                : null;
              const url = route ? encodeNotificationRoute(route) : "/";
              void navigator.serviceWorker?.ready.then((registration) =>
                registration.showNotification(presentation.title, {
                  body: presentation.body,
                  icon: "/icon-192.png", badge: "/favicon.svg",
                  tag: turnNotificationTag(msg), data: { url, route },
                })).catch(() => undefined);
            }
          }
          if (msg.type === "session_focus" && spaceRef.current === "work"
              && !msg.session_id.startsWith("tmp-")) {
            ws.sendGetWorkArtifacts(engineRef.current, msg.session_id);
          }
          if (msg.type === "session_list"
              && !shouldAcceptSessionList(engineRef.current, spaceRef.current, msg)) return;
          if (msg.type === "session_list") {
            const currentSid = stateRef.current.focusedSid;
            if (currentSid && !currentSid.startsWith("tmp-")
                && !msg.sessions.some((session) => session.session_id === currentSid)) {
              didInitFocusRef.current = false;
              preferredSurfaceFocusRef.current = null;
            }
          }
          const statusRuntimeBeforeEvent = msg.sid
            ? stateRef.current.runtimes[msg.sid] : undefined;
          const completesStatusRequest = !!statusRuntimeBeforeEvent?.statusRequestId
            && (
              (msg.type === "status_report"
                && msg.request_id === statusRuntimeBeforeEvent.statusRequestId)
              || (msg.type === "error"
                && msg.request_id === statusRuntimeBeforeEvent.statusRequestId)
            );
          dispatch({ type: "event", event: msg, ownership });
          if (msg.sid && completesStatusRequest
              && deferredStatusRefreshRef.current.delete(msg.sid)) {
            const requestId = ws.sendGetStatusTo(msg.sid);
            if (requestId) {
              dispatch({
                type: "begin_status_request",
                sid: msg.sid,
                requestId,
              });
            }
          }
          // Account quota belongs to the Codex daemon generation, not the
          // transcript. Refresh after the authoritative idle boundary so an
          // account-switch restart has finished before we read the new limits.
          if (msg.type === "state" && msg.state === "idle" && msg.sid) {
            const session = stateRef.current.sessions.find(
              (candidate) => candidate.session_id === msg.sid);
            const eventEngine = session?.engine
              ?? (stateRef.current.focusedSid === msg.sid
                ? engineRef.current : undefined);
            if (eventEngine === "codex") {
              if (stateRef.current.runtimes[msg.sid]?.statusRequestId) {
                deferredStatusRefreshRef.current.add(msg.sid);
              } else {
                const requestId = ws.sendGetStatusTo(msg.sid);
                if (requestId) {
                  dispatch({
                    type: "begin_status_request",
                    sid: msg.sid,
                    requestId,
                  });
                }
              }
            }
          }
          if (msg.type === "wrapper_reconnected") {
            skillCatalogsRef.current = {};
            setSkillCatalogs({});
            skillCatalogRequestsRef.current?.resetReads();
            const focusedSkills = focusedSkillScopeRef.current;
            if (focusedSkills?.engine === "codex" && focusedSkills.cwd) {
              requestSkillCatalog(focusedSkills, true);
            }
            ws.sendListSessions(engineRef.current, spaceRef.current);
            if (spaceRef.current === "work") {
              ws.sendGetWorkDashboard(engineRef.current);
            }
            ws.sendGetModels("codex");
            const currentSid = stateRef.current.focusedSid;
            if (currentSid) requestHistory(
              currentSid, undefined, HISTORY_INITIAL_PAGE, msg.generation);
          }
          // refresh the context ring after each turn (local SDK query, no model tokens)
          if (msg.type === "turn_end" && msg.sid) {
            ws.sendGetContextTo(msg.sid);
            if (sessionActivityPendingRef.current.delete(msg.sid)) {
              const listed = Object.values(sessionListsBySurfaceRef.current)
                .flat().find((session) => session.session_id === msg.sid);
              ws.sendListSessions(
                (listed?.engine as Engine | undefined) ?? engineRef.current,
                listed?.space ?? spaceRef.current,
              );
            }
            const session = stateRef.current.sessions.find(
              (candidate) => candidate.session_id === msg.sid);
            if (session?.space === "work"
                || (spaceRef.current === "work" && stateRef.current.focusedSid === msg.sid)) {
              ws.sendGetWorkArtifacts(
                (session?.engine as Engine | undefined) ?? engineRef.current, msg.sid);
            }
          }
        },
        onConnState: (s, detail) => {
          dispatch({ type: "conn", connState: s, detail });
          if (s !== "connected") {
            skillCatalogRequestsRef.current?.resetReads();
          }
          if (s === "connected") {
            recoverableReads.clear();
            historyRequestsRef.current.beginConnection();
            clearHistoryDetailRequests();
            notificationListRequestRef.current = null;
            bumpNotificationListRevision();
            ws.sendListSessions(engineRef.current, spaceRef.current);
            if (spaceRef.current === "work") {
              ws.sendGetWorkDashboard(engineRef.current);
              const currentSid = stateRef.current.focusedSid;
              if (currentSid) ws.sendGetWorkArtifacts(engineRef.current, currentSid);
            }
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
          createRequestsRef.current.clear();
          pendingBtwByParentRef.current.clear();
          pendingSessionForkRef.current = null;
          pendingWorktreeForkRef.current = null;
          pendingSessionMigrationRef.current = null;
          setMigrateSession(null);
          setMigrateCreating(false);
          setMigrateError(null);
          activeBtwByParentRef.current.clear();
          btwRequestParentsRef.current.clear();
          discardedBtwSidsRef.current.clear();
          historyInvalidationsRef.current.clear();
          historyInvalidationGenerationsRef.current.clear();
          historyCacheEpochRef.current.clear();
          historySessionListsRef.current = {};
          skillCatalogsRef.current = {};
          skillCatalogRequestsRef.current?.reset();
          setSkillCatalogs({});
          setCapabilitiesByScope({});
          inlineImageAssetsRef.current.clear();
          historyImageAssetsRef.current.clear();
          bumpInlineImageRevision();
          bumpHistoryImageRevision();
          historyRequestsRef.current.clear();
          clearHistoryDetailRequests();
          historyPageScopesRef.current.clear();
          void historyPageCacheRef.current.clear();
          setBtwOpeningByParentSid({});
          setBtwSendModeBySid({});
          btwDraftsRef.current.clear();
          setForkingPointId(null);
          setForkWorktreeSession(null);
          setForkWorktreeCreating(false);
          setForkWorktreeError(null);
          setGoalUiBySid({});
          setStatusOpenSid(null);
          setWorkArtifactsOpen(false);
          setWorkArtifactsBySid({});
          setCompletionReceipts({});
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
          clearHistoryDetailRequests();
          inlineImageAssetsRef.current.clear();
          historyImageAssetsRef.current.clear();
          bumpInlineImageRevision();
          bumpHistoryImageRevision();
          discardedBtwSidsRef.current.clear();
          setCompletionReceipts(discardBtwCompletionReceipts);
          setBtwSendModeBySid({});
          btwDraftsRef.current.clear();
          if (Object.keys(stateRef.current.btwByParentSid).length > 0
              || pendingBtwByParentRef.current.size > 0
              || activeBtwByParentRef.current.size > 0
              || btwRequestParentsRef.current.size > 0) {
            pendingBtwByParentRef.current.clear();
            activeBtwByParentRef.current.clear();
            btwRequestParentsRef.current.clear();
            setBtwOpeningByParentSid({});
            dispatch({ type: "clear_all_btw" });
            dispatch({ type: "command_error",
              detail: "服务已重新连接，临时 /btw 会话已关闭，请重新打开。" });
          }
        },
      }, machineId);
      ws.setSurface(engineRef.current, spaceRef.current);
      // Seed both transport and reducer watermarks before Hello. This prevents
      // an older replay/snapshot from reviving a lock already superseded in the
      // last authoritative control snapshot.
      ws.seedReplayState(seeded.cursors, seeded.generations, seeded.controls);
      for (const [sid, control] of Object.entries(seeded.controls)) {
        dispatch({
          type: "hydrate_cache", sid, turns: [], revision: null,
          generation: seeded.generations[sid] ?? control.generation, control,
        });
      }
      wsRef.current = ws;
      ws.start();
    })();

    return () => {
      cancelled = true;
      wsRef.current?.stop();
      wsRef.current = null;
      historyRequests.clear();
      clearHistoryDetailRequests();
      recoverableReads.clear();
    };
  }, [
    acceptSkillCatalog,
    authed,
    clearHistoryDetailRequests,
    installBrowseHistoryPage,
    invalidateHistoryPageScopes,
    machineId,
    requestHistory,
    requestSkillCatalog,
    setBtwOpeningFor,
  ]);

  // Land on the preferred/recent session only after an accepted list for the
  // active engine+space arrives. Background snapshots never pick focus.
  useEffect(() => {
    if (pendingNotificationTarget) return;
    if (didInitFocusRef.current || !wsRef.current) return;
    const surfaceKey = `${spaceRef.current}:${engineRef.current}`;
    if (!authoritativeSurfaceListsRef.current.has(surfaceKey)) return;
    if (state.sessions.length === 0) {
      preferredSurfaceFocusRef.current = null;
      didInitFocusRef.current = true;
      return;
    }
    const preferred = preferredSurfaceFocusRef.current?.key === surfaceKey
      ? state.sessions.find((session) => (
          session.session_id === preferredSurfaceFocusRef.current?.sid
          && (session.space ?? "code") === spaceRef.current
          && (session.engine ?? "claude") === engineRef.current
        ))
      : undefined;
    preferredSurfaceFocusRef.current = null;
    const latest = preferred ?? [...state.sessions]
      .filter((s) => s.tag !== "archived")
      .sort(compareSessionsByActivity)[0]
      ?? state.sessions[0];
    didInitFocusRef.current = true;
    if (latest && latest.session_id !== state.focusedSid) {
      dispatch({ type: "exit_new_chat" });
      dispatch({ type: "focus_session", sid: latest.session_id });
      const latestEngine = (latest.engine as "claude" | "codex") || engineRef.current;
      wsRef.current.setFocusedSid(latest.session_id, latestEngine, spaceRef.current);
      requestHistory(
        latest.session_id, undefined, HISTORY_INITIAL_PAGE);
      wsRef.current.sendSwitchSession(latest.session_id, latestEngine, spaceRef.current);
    }
  }, [
    pendingNotificationTarget,
    state.sessions,
    state.focusedSid,
    requestHistory,
  ]);

  // Direct sidebar selection and newly-created sessions both update the
  // per-surface bookmark. A later Work/Code or engine toggle can therefore
  // restore the exact view without relying on whichever list row happens to be
  // newest at that moment.
  useEffect(() => {
    if (!focusedSid || state.newChat) return;
    const selected = state.sessions.find((session) => session.session_id === focusedSid);
    if (!selected) return;
    const selectedEngine = (selected.engine as Engine | undefined) ?? engine;
    const selectedSpace: Space = selected.space === "work" ? "work" : "code";
    lastFocusBySurfaceRef.current[`${selectedSpace}:${selectedEngine}`] = focusedSid;
  }, [focusedSid, state.newChat, state.sessions, engine]);

  // Warm the cwd-scoped Codex Skill catalog when a session becomes usable.
  // Composer completion then reads memory synchronously; an expired entry stays
  // visible while this refresh runs in the background.
  useEffect(() => {
    if (!authed || !focusedSid || focusedEngine !== "codex" || state.newChat
        || !capabilityCwd || state.connState !== "connected"
        || !state.wrapperOnline) return;
    requestSkillCatalog({
      key: focusedSkillCatalogKey,
      engine: focusedEngine,
      space,
      cwd: capabilityCwd,
      skillsOnly: true,
    });
  }, [
    authed,
    capabilityCwd,
    focusedEngine,
    focusedSid,
    focusedSkillCatalogKey,
    requestSkillCatalog,
    space,
    state.connState,
    state.newChat,
    state.wrapperOnline,
  ]);

  // An open Extensions sheet follows the focused authorization scope. Switching
  // device, engine, space, or cwd must fetch that scope instead of retaining a
  // report from the previously visible session.
  useEffect(() => {
    if (!capabilitiesOpen || !authed || state.connState !== "connected"
        || !state.wrapperOnline) return;
    setCapabilitiesLoading(true);
    requestSkillCatalog({
      key: focusedSkillCatalogKey,
      engine: focusedEngine,
      space,
      cwd: capabilityCwd,
      skillsOnly: false,
    }, true);
  }, [
    authed,
    capabilitiesOpen,
    capabilityCwd,
    focusedEngine,
    focusedSkillCatalogKey,
    requestSkillCatalog,
    space,
    state.connState,
    state.wrapperOnline,
  ]);

  // Keep a long-lived tab bounded without evicting anything that can still be
  // acted on. ACK callbacks run the same prune when an outbox target becomes
  // reclaimable; otherwise an idle runtime protected during retry would linger.
  useEffect(() => {
    if (Object.keys(state.runtimes).length <= MAX_RUNTIME_SESSIONS) return;
    dispatch({
      type: "prune_runtimes",
      protectedSids: wsRef.current?.pendingSessionIds() ?? [],
    });
  }, [state.runtimes, focusedSid, state.btwByParentSid, state.artifact?.sid]);

  // Persist the focused session's turns to IndexedDB (Phase-2 will write through
  // background sessions too). Coalesced in cache.ts.
  useEffect(() => {
    const sid = rt.ccSessionId;
    const revision = rt.historyRevision;
    if (!sid || !revision
        || historyInvalidationsRef.current.has(sid)
        || isHistoryRecoveryPending(state.historyRecovery, sid)) return;
    import("./cache").then(({ saveSession }) => {
      const live = wsRef.current?.lastSeqFor(sid) || 0;
      saveSession(
        sid, rt.turns, live, revision,
        wsRef.current?.generationFor(sid),
        rt.control,
      );
    });
  }, [
    focusedSid, rt.turns, rt.ccSessionId, rt.historyRevision, rt.control,
    state.historyRecovery,
  ]);

  // Race the small authoritative newest page with IndexedDB. A healthy local
  // projection still paints immediately, but a slow/blocked IDB open must never
  // delay the wrapper's four-turn response. hydrate_cache only fills an empty
  // runtime, so whichever source wins cannot clobber the other. A 6s fallback
  // clears the spinner only when both cache and wrapper stay silent.
  useEffect(() => {
    const sid = focusedSid;
    if (!sid) return;
    let cancelled = false;
    let requestFrame: number | null = null;
    const cacheEpoch = historyCacheEpochRef.current.get(sid) ?? 0;
    if (state.connState === "connected") {
      requestFrame = window.requestAnimationFrame(() => {
        requestFrame = null;
        if (!cancelled) {
          requestHistory(sid, undefined, HISTORY_INITIAL_PAGE);
        }
      });
    }
    void import("./cache").then(({ loadSession }) => loadSession(sid)).then((cached) => {
      const valid = !cancelled
          && cacheEpoch === (historyCacheEpochRef.current.get(sid) ?? 0)
          && !historyInvalidationsRef.current.has(sid)
          && cached && Array.isArray(cached.turns)
          && (cached.turns.length || cached.control);
      if (valid && cached) {
        dispatch({
          type: "hydrate_cache", sid,
          turns: (cached.turns as Turn[]).map((turn) => ({
            ...turn, detailLoading: false,
          })),
          revision: cached.revision,
          generation: cached.generation ?? cached.control?.generation,
          control: cached.control,
        });
      }
    });
    const t = window.setTimeout(() => dispatch({
      type: "hydrate_cache", sid, turns: [], revision: null,
    }), 6000);
    return () => {
      cancelled = true;
      window.clearTimeout(t);
      if (requestFrame != null) window.cancelAnimationFrame(requestFrame);
    };
  }, [focusedSid, requestHistory, state.connState]);

  // Cmd/Ctrl+B => toggle sidebar; Cmd/Ctrl+Shift+B => open latest turn's diff
  useEffect(() => {
    if (!authed) return;
    const onKey = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey)) return;
      const k = e.key.toLowerCase();
      if (k === "b" && e.shiftKey) {           // diff (shared right slot)
        e.preventDefault();
        const latest = shortcutRef.current;
        if (latest.artifact?.kind === "gitdiff" && latest.rightView === "diff") dispatch({ type: "clear_artifact" });
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
        if ((rt.control && sessionControlLocksInput(rt.control))
            || (!rt.control && rt.external)) return;
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
  }, [authed, focusedSid, rt.perm, rt.collaborationMode, rt.control,
    rt.external, focusedEngine]);

  const loadHistoryTurnDetail = useCallback((
    displayTurnId: string, before?: string | null,
    autoLoad = true,
  ): boolean => {
    const current = stateRef.current;
    const sid = current.focusedSid;
    const runtime = sid ? current.runtimes[sid] : null;
    if (!sid || !runtime?.historyRevision) return false;
    const browse = current.historyBrowse?.sid === sid
      ? current.historyBrowse : null;
    const displayed = (browse?.turns ?? runtime.turns).find(
      (turn) => turn.id === displayTurnId
        || canonicalTurnId(turn) === displayTurnId);
    if (!displayed) return false;
    const turnId = canonicalTurnId(displayed);
    const scope = historyPageScopeFor(
      sid, runtime.historyRevision, focusedEngine, space);
    const context: HistoryDetailRequestContext = browse
      ? {
          target: "browse",
          scopeKey: browse.scopeKey,
          sid,
          revision: browse.revision,
          turnId,
          before,
          viewId: browse.viewId,
          windowEpoch: browse.windowEpoch,
        }
      : {
          target: "runtime",
          scopeKey: historyPageCacheScopeKey(scope),
          sid,
          revision: runtime.historyRevision,
          turnId,
          before,
          autoLoad,
        };
    if (!historyDetailRequestsRef.current.begin(context)) return false;
    const sent = wsRef.current?.sendGetTurnDetail(
      sid, turnId, context.revision, before) ?? false;
    if (!sent) {
      historyDetailRequestsRef.current.cancel(context);
      return false;
    }
    if (context.target === "browse") {
      dispatch({
        type: "history_browse_detail_requested",
        sid,
        scopeKey: context.scopeKey,
        revision: context.revision,
        viewId: context.viewId,
        windowEpoch: context.windowEpoch,
        turnId,
        before,
      });
    } else {
      dispatch({
        type: "turn_detail_requested", sid, turnId, before, autoLoad,
      });
    }
    return true;
  }, [focusedEngine, historyPageScopeFor, space]);
  useEffect(() => {
    const current = stateRef.current;
    const sid = current.focusedSid;
    const runtime = sid ? current.runtimes[sid] : null;
    if (!sid || !runtime || current.historyBrowse?.sid === sid) return;
    // Restore only the newest affected row and only its newest detail page.
    // Older rows retain their instant cached projection until the user opens
    // them, avoiding a refresh-triggered multi-megabyte pagination cascade.
    const target = [...runtime.turns].reverse().find((turn) =>
      turn.detailRestorePending === true && turn.detailLoading !== true);
    if (!target) return;
    loadHistoryTurnDetail(target.id, undefined, false);
  }, [loadHistoryTurnDetail, state.historyBrowse, state.runtimes,
    state.focusedSid]);
  useEffect(() => {
    const current = stateRef.current;
    const sid = current.focusedSid;
    const runtime = sid ? current.runtimes[sid] : null;
    if (!sid || !runtime) return;
    const turns = current.historyBrowse?.sid === sid
      ? current.historyBrowse.turns : runtime.turns;
    const next = nextAutoLoadDetailTurn(turns);
    if (!next) return;
    loadHistoryTurnDetail(next.turnId, next.before);
  }, [loadHistoryTurnDetail, state.historyBrowse, state.runtimes,
    state.focusedSid]);

  // Prime the ring as soon as the focused session is usable. Previously the
  // first read only happened when the user opened the popover or a turn ended,
  // so a freshly loaded/restored session misleadingly painted an empty ring.
  // wrapperOnline deliberately gates reconnects until snapshot/replay proves
  // that the replacement wrapper has finished restoring resident sessions.
  useEffect(() => {
    if (!authed || !focusedSid || state.newChat
        || state.connState !== "connected" || !state.wrapperOnline) return;
    if (stateRef.current.runtimes[focusedSid]?.contextRequestId) return;
    const requestId = wsRef.current?.sendGetContext();
    if (requestId) {
      dispatch({ type: "begin_context_request", sid: focusedSid, requestId });
    }
  }, [
    authed,
    focusedSid,
    state.connState,
    state.newChat,
    state.wrapperOnline,
  ]);

  const refreshStatus = useCallback(() => {
    if (!focusedSid || focusedEngine !== "codex") return;
    if (stateRef.current.runtimes[focusedSid]?.statusRequestId) return;
    const requestId = wsRef.current?.sendGetStatus();
    if (requestId) {
      dispatch({ type: "begin_status_request", sid: focusedSid, requestId });
    }
  }, [focusedEngine, focusedSid]);
  useEffect(() => {
    if (!authed || !focusedSid || focusedEngine !== "codex" || state.newChat
        || rt.state !== "idle"
        || state.connState !== "connected" || !state.wrapperOnline) return;
    if (stateRef.current.runtimes[focusedSid]?.statusRequestId) return;
    refreshStatus();
  }, [
    authed,
    focusedEngine,
    focusedSid,
    refreshStatus,
    rt.state,
    state.connState,
    state.newChat,
    state.wrapperOnline,
  ]);

  if (!authReady) {
    return <div className="login" aria-busy="true">正在连接中继…</div>;
  }

  if (!authed) {
    return <LoginForm onLogin={() => { dispatch({ type: "reset" }); setAuthed(true); }} theme={theme} onToggleTheme={toggleTheme} />;
  }

  const sendDeferredQuery = (
    sid: string,
    query: PendingQuery,
    delivery: "queue" | "replace",
  ): boolean => {
    const ws = wsRef.current;
    if (!ws) return false;
    const currentState = stateRef.current;
    const unconfirmed = collectUnconfirmedQueries(
      currentState.runtimes,
      delivery === "replace" ? sid : undefined,
    );
    if (!canEnqueueQuery(
      unconfirmed,
      query,
      deferredQueueCapacity(
        currentState, delivery === "replace" ? sid : undefined),
    )) {
      dispatch({
        type: "command_error",
        detail: "排队已满（最多 32 条 / 64 MiB），请先等待发送。",
      });
      return false;
    }
    const msg_id = uuid();
    if (!ws.sendDeferredQueryTo(
      sid, query.prompt, msg_id, delivery, query.images, query.files,
    )) return false;
    const currentReplacement = currentState.runtimes[sid]?.pendingSend;
    const optimistic: PendingQuery = {
      ...query,
      msg_id,
      imageCount: query.images?.length,
      fileCount: query.files?.length,
      queueKind: delivery,
      queueState: "submitting",
      retainedBytes: undefined,
      queueError: undefined,
      failedAt: undefined,
      replacesRetainedBytes: delivery === "replace"
        ? currentReplacement?.queueState === "queued"
          ? currentReplacement.retainedBytes
          : currentReplacement?.replacesRetainedBytes
        : undefined,
    };
    dispatch(delivery === "queue"
      ? { type: "enqueue", sid, query: optimistic }
      : { type: "set_pending", sid, query: optimistic });
    return true;
  };

  const cancelQueuedQuery = (sid: string, query: PendingQuery): void => {
    if (!query.msg_id) return;
    if (query.queueState !== "failed"
        && !wsRef.current?.sendCancelQueuedQueryTo(sid, query.msg_id)) {
      return;
    }
    dispatch({ type: "remove_deferred", sid, msgId: query.msg_id });
  };

  const inspectQueuedQuery = (sid: string, query: PendingQuery): void => {
    if (!query.msg_id) return;
    const serverOwned = query.queueState === "queued";
    const requestId = serverOwned
      ? wsRef.current?.sendGetQueuedQueryTo(sid, query.msg_id) ?? null
      : null;
    setQueuedQueryEditor({
      sid,
      msgId: query.msg_id,
      preview: query.prompt,
      prompt: serverOwned ? null : query.prompt,
      kind: query.queueKind ?? null,
      state: query.queueState ?? "submitting",
      imageCount: query.imageCount ?? query.images?.length ?? 0,
      fileCount: query.fileCount ?? query.files?.length ?? 0,
      loading: serverOwned && requestId !== null,
      saving: false,
      error: serverOwned && requestId === null
        ? "完整消息读取请求未发送，请等待连接恢复后重试。"
        : query.queueError ?? null,
      detailRequestId: requestId,
      updateRequestId: null,
      pendingPrompt: null,
    });
  };

  const updateQueuedQuery = (prompt: string): boolean => {
    const current = queuedQueryEditor;
    if (!current || current.saving) return false;
    if (current.state === "failed") {
      dispatch({
        type: "update_failed_deferred",
        sid: current.sid,
        msgId: current.msgId,
        prompt,
      });
      setQueuedQueryEditor((editor) => (
        editor
        && editor.sid === current.sid
        && editor.msgId === current.msgId
          ? {
              ...editor,
              preview: prompt.slice(0, 512),
              prompt,
            }
          : editor
      ));
      return true;
    }
    if (current.state !== "queued") return false;
    const requestId = wsRef.current?.sendUpdateQueuedQueryTo(
      current.sid, current.msgId, prompt) ?? null;
    if (requestId === null) {
      setQueuedQueryEditor((editor) => editor ? {
        ...editor,
        error: "修改请求未发送，请等待连接恢复后重试。",
      } : null);
      return false;
    }
    setQueuedQueryEditor((editor) => (
      editor
      && editor.sid === current.sid
      && editor.msgId === current.msgId
        ? {
            ...editor,
            saving: true,
            error: null,
            updateRequestId: requestId,
            pendingPrompt: prompt,
          }
        : editor
    ));
    return true;
  };

  const retryQueuedQuery = (): boolean => {
    const current = queuedQueryEditor;
    if (!current || current.saving || current.prompt === null) return false;
    if (current.state === "queued") {
      return updateQueuedQuery(current.prompt);
    }
    if (current.state !== "failed") return false;
    const runtime = stateRef.current.runtimes[current.sid];
    const query = runtime?.failedDeferred.find(
      (candidate) => candidate.msg_id === current.msgId);
    if (!query) return false;
    const sent = sendDeferredQuery(
      current.sid,
      { ...query, prompt: current.prompt },
      query.queueKind === "replace" ? "replace" : "queue",
    );
    if (!sent) return false;
    dispatch({
      type: "remove_deferred",
      sid: current.sid,
      msgId: current.msgId,
    });
    setQueuedQueryEditor(null);
    return true;
  };

  const sendQuery = (prompt: string, images?: QueryImg[], files?: QueryFile[]): boolean => {
    const ws = wsRef.current;
    if (!ws || !focusedSid) return false;
    const query = { prompt, images, files };
    const currentState = stateRef.current;
    const currentRuntime = currentState.runtimes[focusedSid];
    const awaitingAcceptance = !!(
      ws.pendingQueryFor(focusedSid) || currentRuntime?.acceptancePending
    );
    const hasDeferred = !!(
      currentRuntime?.queue.length || currentRuntime?.pendingSend
    );
    if (awaitingAcceptance || hasDeferred) {
      const sendMode = currentRuntime?.sendMode ?? "steer";
      const delivery = awaitingAcceptance && sendMode !== "queue"
        ? "replace" : "queue";
      const unconfirmed = collectUnconfirmedQueries(
        currentState.runtimes,
        delivery === "replace" ? focusedSid : undefined,
      );
      if (!canEnqueueQuery(
        unconfirmed,
        query,
        deferredQueueCapacity(
          currentState,
          delivery === "replace" ? focusedSid : undefined,
        ),
      )) {
        dispatch({
          type: "command_error",
          detail: "排队已满（最多 32 条 / 64 MiB），请先等待发送。",
        });
        return false;
      }
      return sendDeferredQuery(focusedSid, query, delivery);
    }
    const msg_id = uuid();
    if (!ws.sendQueryTo(focusedSid, prompt, msg_id, images, files)) return false;
    const activityMs = Date.now();
    const surfaceKey = `${space}:${engine}`;
    const cached = sessionListsBySurfaceRef.current[surfaceKey];
    if (cached) {
      sessionListsBySurfaceRef.current[surfaceKey] = bumpSessionActivity(
        cached, focusedSid, activityMs);
    }
    sessionActivityPendingRef.current.add(focusedSid);
    dispatch({ type: "query_sent", sid: focusedSid, prompt, msg_id, images, files,
      ts: activityMs });
    return true;
  };
  const sendSteer = (
    prompt: string, images?: QueryImg[], files?: QueryFile[],
  ): boolean => {
    const ws = wsRef.current;
    if (!ws || !focusedSid || focusedEngine !== "codex") return false;
    const msg_id = uuid();
    if (!ws.sendSteerTo(focusedSid, prompt, msg_id, images, files)) return false;
    if (stateRef.current.historyBrowse?.sid === focusedSid) {
      dispatch({ type: "return_to_latest", sid: focusedSid });
    }
    return true;
  };
  const loadOlderHistoryPage = (
    anchorTurnId?: string,
  ): boolean | { accepted: true; viewId: string } => {
    const current = stateRef.current;
    const sid = current.focusedSid;
    const runtime = sid ? current.runtimes[sid] : null;
    if (!sid || !runtime?.historyRevision || !wsRef.current
        || runtime.historyInvalidated
        || isHistoryRecoveryPending(current.historyRecovery, sid)) return false;
    const existing = current.historyBrowse?.sid === sid
      ? current.historyBrowse : null;
    if (existing) {
      if (!existing.hasOlder || !existing.olderCursor
          || !historyPageScopesRef.current.has(existing.scopeKey)) return false;
      const context: HistoryBrowseRequestContext = {
        scopeKey: existing.scopeKey,
        viewId: existing.viewId,
        windowEpoch: existing.windowEpoch,
        pendingBefore: existing.olderCursor,
        sourcePageKey: existing.oldestPageKey,
        anchorTurnId: anchorTurnId ?? null,
      };
      return requestHistory(
        sid, existing.olderCursor, HISTORY_MORE_PAGE,
        existing.generation, existing.revision, context);
    }
    if (!runtime.hasMore || !runtime.oldestId) return false;
    const scope = historyPageScopeFor(
      sid, runtime.historyRevision, focusedEngine, space);
    const scopeKey = historyPageCacheScopeKey(scope);
    const viewId = uuid();
    const basePageKey = `${HISTORY_LATEST_PAGE_KEY}:${viewId}`;
    const browseGeneration = runtime.historyGeneration
      ?? wsRef.current.generationFor(sid)
      ?? null;
    const basePage: HistoryBrowsePage = {
      pageKey: basePageKey,
      turns: runtime.turns,
      hasOlder: true,
      olderCursor: runtime.oldestId,
      hasNewer: false,
      newerPageKey: null,
      isLatest: true,
    };
    void historyPageCacheRef.current.putPage(scope, basePage);
    dispatch({
      type: "begin_history_browse",
      sid,
      scopeKey,
      revision: runtime.historyRevision,
      generation: browseGeneration,
      viewId,
      basePageKey,
    });
    const accepted = requestHistory(
      sid, runtime.oldestId, HISTORY_MORE_PAGE,
      browseGeneration, runtime.historyRevision, {
        scopeKey,
        viewId,
        windowEpoch: 0,
        pendingBefore: runtime.oldestId,
        sourcePageKey: basePageKey,
        anchorTurnId: anchorTurnId ?? null,
      });
    return accepted ? { accepted: true, viewId } : false;
  };
  const loadNewerHistoryPage = (anchorTurnId?: string): boolean => {
    const browse = stateRef.current.historyBrowse;
    if (!browse || !browse.hasNewer || !browse.newerPageKey) return false;
    const scope = historyPageScopesRef.current.get(browse.scopeKey);
    if (!scope) return false;
    const frozen = {
      sid: browse.sid,
      scopeKey: browse.scopeKey,
      revision: browse.revision,
      generation: browse.generation,
      viewId: browse.viewId,
      windowEpoch: browse.windowEpoch,
      pageKey: browse.newerPageKey,
      anchorTurnId: anchorTurnId ?? null,
    };
    void (async () => {
      const page = await historyPageCacheRef.current.getPage(
        scope, frozen.pageKey);
      const currentState = stateRef.current;
      const current = currentState.historyBrowse;
      if (currentState.focusedSid !== frozen.sid
          || !current
          || !acceptsCachedNewerPage(current, frozen)) return;
      if (page?.isLatest && current.latestDirty) {
        dispatch({
          type: "history_browse_newer_settled",
          sid: frozen.sid,
          scopeKey: frozen.scopeKey,
          revision: frozen.revision,
          generation: frozen.generation,
          viewId: frozen.viewId,
          windowEpoch: frozen.windowEpoch,
          pageKey: frozen.pageKey,
        });
        return;
      }
      if (!page) {
        dispatch({
          type: "history_browse_newer_unavailable",
          sid: frozen.sid,
          scopeKey: frozen.scopeKey,
          revision: frozen.revision,
          generation: frozen.generation,
          viewId: frozen.viewId,
          windowEpoch: frozen.windowEpoch,
        });
        return;
      }
      const protectedTurnIds = protectedHistoryTurnIds(
        frozen.anchorTurnId,
        textSelectionGuardRef.current,
        {
          sid: frozen.sid,
          revision: frozen.revision,
          viewId: frozen.viewId,
          scopeKey: frozen.scopeKey,
        },
      );
      const mutation = appendNewerPage(current, page, {
        expectedScopeKey: frozen.scopeKey,
        expectedViewId: frozen.viewId,
        expectedWindowEpoch: frozen.windowEpoch,
        expectedNewerPageKey: frozen.pageKey,
        protectedTurnIds,
      });
      if (mutation.projection === current) return;
      for (const evicted of mutation.evictedPages) {
        void historyPageCacheRef.current.putPage(scope, evicted);
      }
      dispatch({
        type: "install_history_browse_newer",
        sid: frozen.sid,
        scopeKey: frozen.scopeKey,
        revision: frozen.revision,
        generation: frozen.generation,
        viewId: frozen.viewId,
        windowEpoch: frozen.windowEpoch,
        page,
        protectedTurnIds,
        prepared: {
          from: current,
          to: mutation.projection,
        },
      });
    })();
    return true;
  };
  const returnToLatestHistory = () => {
    const sid = stateRef.current.historyBrowse?.sid;
    if (sid) dispatch({ type: "return_to_latest", sid });
  };
  // One command creates the session and starts its first query atomically. The
  // wrapper targets the new temp-keyed ctx directly; no later focus event is used
  // to route or trigger this message.
  const sendFirstMessage = (prompt: string, images?: QueryImg[], files?: QueryFile[],
                            collaborationMode?: CollaborationModeName,
                            permissionMode?: CodexPermissionMode,
                            permissionProfile?: string,
                            webSearch?: CodexWebSearchMode,
                            serviceTier?: CodexServiceTier): boolean => {
    if (!wsRef.current || !state.newChat) return false;
    const { cwd, cwdSource, model, effort } = state.newChat;
    // Null is meaningful: let the local CLI/app-server use its configured defaults.
    // Only explicit user choices cross the wire; otherwise a stale fallback catalog
    // could silently override the machine's real model or reasoning configuration.
    const msg_id = uuid();
    const queued = wsRef.current.sendNewSession(
      space === "work" ? null : cwd, engine, model, effort,
      { prompt, msg_id, images, files },
      engine === "codex" ? collaborationMode : undefined,
      engine === "codex"
        ? (space === "work" ? "never" : permissionMode)
        : undefined,
      engine === "codex" && space === "code"
        ? permissionProfile
        : undefined,
      engine === "codex" && space === "code"
        ? webSearch
        : undefined,
      engine === "codex" ? serviceTier : undefined,
      space, space === "work" ? workProjectId : undefined);
    if (queued) {
      pendingCreateRef.current = msg_id;
      createRequestsRef.current.set(msg_id, {
        scopeKey: sessionScopeKey(machineId, engine, space),
        cwdSource,
      });
      while (createRequestsRef.current.size > 64) {
        const oldest = createRequestsRef.current.keys().next().value;
        if (!oldest) break;
        createRequestsRef.current.delete(oldest);
      }
      setCreateError(null);
    }
    return queued;
  };
  const pickNewChatModel = (model: string | null) => {
    const current = state.newChat;
    if (!current) return;
    const compatibleEffort = compatibleNewChatEffort(
      engine,
      model,
      current.effort,
      state.catalog,
      newChatDefaults.model,
    );
    dispatch({ type: "set_new_chat_model", model });
    if (compatibleEffort !== current.effort) {
      dispatch({
        type: "set_new_chat_effort",
        effort: compatibleEffort,
      });
    }
  };
  const pickNewChatEffort = (effort: string | null) => {
    if (!state.newChat) return;
    dispatch({ type: "set_new_chat_effort", effort });
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
  const getPermissionProfiles = () => {
    wsRef.current?.sendGetPermissionProfiles();
  };
  const setPermissionProfile = (profile: string) => {
    wsRef.current?.sendSetPermissionProfile(profile);
  };
  const setWebSearch = (mode: CodexWebSearchMode) => {
    wsRef.current?.sendSetWebSearch(mode);
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
    const command = parseGoalCommand(args, focusedEngine);
    if (command.kind === "clear") {
      wsRef.current?.sendClearGoal();
      setGoalUi({ revealed: false, open: false });
      return;
    }
    setGoalUi({ revealed: true, open: true });
    if (command.kind === "show") {
      wsRef.current?.sendGetGoal();
    } else if (command.kind === "resume") {
      // Codex resumes the existing condition by changing only its status.
      if (focusedEngine === "codex") {
        wsRef.current?.sendSetGoal(null, "active", null);
      }
    } else {
      wsRef.current?.sendSetGoal(command.objective, "active", null);
    }
  };
  const openStatus = () => {
    if (!focusedSid) return;
    setStatusOpenSid(focusedSid);
    refreshStatus();
  };
  const requestContext = () => {
    if (!focusedSid) return;
    const requestId = wsRef.current?.sendGetContext();
    if (requestId) {
      dispatch({ type: "begin_context_request", sid: focusedSid, requestId });
    }
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
  const openSessionMigration = (session: SessionInfo) => {
    if (pendingSessionMigrationRef.current || !confirmArtifactDiscard()) return;
    setMigrateError(null);
    setMigrateSession(session);
  };
  const submitSessionMigration = (cwd: string) => {
    const source = migrateSession;
    if (!source || pendingSessionMigrationRef.current) return;
    setMigrateError(null);
    const requestId = wsRef.current?.sendMigrateSession(
      source.session_id, cwd) ?? null;
    if (!requestId) {
      setMigrateError("请求未发送，请等待连接恢复后重试。");
      return;
    }
    pendingSessionMigrationRef.current = {
      requestId,
      sessionId: source.session_id,
    };
    setMigrateCreating(true);
  };
  const closeSessionMigration = () => {
    if (pendingSessionMigrationRef.current) return;
    setMigrateSession(null);
    setMigrateError(null);
  };
  const getDiff = (file: string) => {
    if (!confirmArtifactDiscard()) return;
    const requestId = wsRef.current?.sendGetDiff(file, theme) ?? null;
    if (!requestId) return;
    setRightView("diff");
    dispatch({ type: "open_artifact_loading", file, sid: focusedSid, requestId });
  };
  const openTurnDiff = (files: string[], diff: string) => {
    if (!diff || !confirmArtifactDiscard()) return;
    setRightView("diff");
    dispatch({ type: "set_artifact", artifact: {
      file: files.length === 1 ? files[0] : `本轮改动 · ${files.length} 个文件`,
      sid: focusedSid,
      kind: "gitdiff",
      sections: parseGitDiff(diff),
    } });
  };
  const previewFileForSid = (
    targetSid: string | null,
    file: string,
    line?: number,
  ) => {
    if (!targetSid) return;
    if (!confirmArtifactDiscard()) return;
    const requestId = uuid();
    if (!wsRef.current?.sendGetFilePreview(file, requestId, targetSid)) return;
    setRightView("diff");
    dispatch({
      type: "open_file_loading",
      file,
      sid: targetSid,
      requestId,
      kind: isMarkdownPath(file) ? "md" : "file",
      line,
    });
  };
  const previewFile = (file: string, line?: number) =>
    previewFileForSid(focusedSid, file, line);
  const previewBtwFile = (file: string, line?: number) =>
    previewFileForSid(activeBtwSid, file, line);
  const previewArtifactFile = (file: string, line?: number) =>
    previewFileForSid(state.artifact?.sid ?? focusedSid, file, line);
  const previewMarkdown = (file: string) => previewFile(file);
  const loadPreviewAsset = (file: string, previewId: string): boolean => {
    const targetSid = state.artifact?.sid ?? focusedSid;
    return !!targetSid && !!wsRef.current?.sendGetPreviewAsset(
      file, previewId, uuid(), targetSid);
  };
  const saveMarkdown = (file: string, content: string, expectedSize: number,
                        expectedMtimeNs: string, expectedRevision: string): string | null => {
    const targetSid = state.artifact?.sid ?? focusedSid;
    if (!targetSid) return null;
    const requestId = uuid();
    if (!wsRef.current?.sendSaveMarkdown(
      file, content, expectedSize, expectedMtimeNs, expectedRevision,
      requestId, targetSid)) return null;
    dispatch({ type: "start_file_save", requestId, content });
    return requestId;
  };
  // Each /btw stays pinned to its parent session. Navigation hides it without
  // destroying the fork; returning to that parent restores it. Other sessions
  // can open their own independent side conversations.
  const openBtw = () => {
    if (!confirmArtifactDiscard()) return;
    setRightView("btw");
    const parentSid = visibleParentSid;
    if (!parentSid || activeBtw
        || pendingBtwByParentRef.current.has(parentSid)) return;
    const requestId = wsRef.current?.sendOpenBtw(parentSid) ?? null;
    if (!requestId) {
      setBtwOpeningFor(parentSid, false);
      return;
    }
    pendingBtwByParentRef.current.set(parentSid, requestId);
    const requestParents = btwRequestParentsRef.current;
    requestParents.set(requestId, parentSid);
    while (requestParents.size > 64) {
      const oldest = requestParents.keys().next().value as string | undefined;
      if (!oldest) break;
      requestParents.delete(oldest);
    }
    setBtwOpeningFor(parentSid, true);
  };
  const sendBtw = (prompt: string): boolean => {
    const sid = activeBtwSid;
    const ws = wsRef.current;
    if (!sid || !ws) return false;
    const runtime = stateRef.current.runtimes[sid];
    const awaitingAcceptance = !!(
      ws.pendingQueryFor(sid) || runtime?.acceptancePending
    );
    if (awaitingAcceptance || runtime?.queue.length || runtime?.pendingSend) {
      const delivery = awaitingAcceptance && activeBtwSendMode !== "queue"
        ? "replace" : "queue";
      const query = { prompt };
      const currentState = stateRef.current;
      const unconfirmed = collectUnconfirmedQueries(
        currentState.runtimes,
        delivery === "replace" ? sid : undefined,
      );
      if (!canEnqueueQuery(
        unconfirmed,
        query,
        deferredQueueCapacity(
          currentState, delivery === "replace" ? sid : undefined),
      )) return false;
      return sendDeferredQuery(sid, query, delivery);
    }
    const msg_id = uuid();
    if (!ws.sendQueryTo(sid, prompt, msg_id)) return false;
    dispatch({
      type: "query_sent", sid, prompt, msg_id, ts: Date.now(),
    });
    return true;
  };
  const steerBtw = (prompt: string): boolean => {
    const sid = activeBtwSid;
    const ws = wsRef.current;
    if (!sid || !ws || activeBtw?.engine !== "codex") return false;
    return ws.sendSteerTo(sid, prompt, uuid());
  };
  const interruptBtw = (sid: string) => {
    wsRef.current?.sendInterruptTo(sid);
  };
  const setBtwModel = (sid: string, model: string) => {
    wsRef.current?.sendSetModelTo(sid, model);
  };
  const setBtwEffort = (sid: string, effort: string) => {
    wsRef.current?.sendSetEffortTo(sid, effort);
  };
  const setBtwSendMode = (
    sid: string, mode: SendMode,
  ) => {
    setBtwSendModeBySid((current) => (
      current[sid] === mode ? current : { ...current, [sid]: mode }
    ));
  };
  const closeBtw = () => {
    const parentSid = visibleParentSid;
    if (!parentSid) return;
    const pendingRequestId = pendingBtwByParentRef.current.get(parentSid);
    pendingBtwByParentRef.current.delete(parentSid);
    activeBtwByParentRef.current.delete(parentSid);
    setBtwOpeningFor(parentSid, false);
    if (pendingRequestId) {
      // Keep the request -> parent tombstone. A late success is classified as
      // stale and its newly-created fork is closed immediately.
      btwRequestParentsRef.current.set(pendingRequestId, parentSid);
    }
    if (activeBtw) {
      btwDraftsRef.current.delete(activeBtwDraftKey);
      setBtwSendModeBySid((current) => {
        if (!(activeBtw.sid in current)) return current;
        const next = { ...current };
        delete next[activeBtw.sid];
        return next;
      });
      setCompletionReceipts((receipts) => acknowledgeCompletion(
        receipts, parentSid, { btwSid: activeBtw.sid }));
      wsRef.current?.sendCloseBtw(activeBtw.sid);
      dispatch({ type: "clear_btw", parentSid });
    }
  };
  // Header tab switch between the two right-slot views (opening the target lazily).
  const switchRight = (v: "diff" | "btw") => {
    if (v === "diff") {
      setRightView("diff");
      if (!state.artifact) getDiff("");
    } else openBtw();
  };
  shortcutRef.current = {
    artifact: state.artifact, btwSid: activeBtwSid, rightView,
    getDiff, openBtw, closeBtw,
  };
  const logout = async () => {
    try {
      const response = await fetch("/api/logout", {
        method: "POST", credentials: "same-origin", cache: "no-store",
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      await import("./cache").then((module) => module.clearCache());
      await historyPageCacheRef.current.clear();
      wsRef.current?.stop();
      pendingCreateRef.current = null;
      createRequestsRef.current.clear();
      pendingBtwByParentRef.current.clear();
      pendingSessionForkRef.current = null;
      pendingWorktreeForkRef.current = null;
      pendingSessionMigrationRef.current = null;
      setMigrateSession(null);
      setMigrateCreating(false);
      setMigrateError(null);
      sessionActivityPendingRef.current.clear();
      activeBtwByParentRef.current.clear();
      btwRequestParentsRef.current.clear();
      discardedBtwSidsRef.current.clear();
      historyInvalidationsRef.current.clear();
      historyInvalidationGenerationsRef.current.clear();
      historyCacheEpochRef.current.clear();
      historyRequestsRef.current.clear();
      clearHistoryDetailRequests();
      historyPageScopesRef.current.clear();
      composerDraftsRef.current.clear();
      btwDraftsRef.current.clear();
      setBtwSendModeBySid({});
      setCreateError(null);
      setForkingPointId(null);
      setForkWorktreeSession(null);
      setForkWorktreeCreating(false);
      setForkWorktreeError(null);
      setBtwOpeningByParentSid({});
      setCompletionReceipts({});
      dispatch({ type: "reset" });
      setAuthed(false);
    } catch {
      dispatch({ type: "command_error", detail: "退出失败：服务暂不可用，请稍后重试" });
    }
  };
  const updateNotificationMode = async (mode: NotificationMode): Promise<boolean> => {
    if (mode !== "off") {
      if (typeof Notification === "undefined") {
        dispatch({ type: "command_error", detail: "当前浏览器不支持系统通知。" });
        return false;
      }
      const permission = Notification.permission === "granted"
        ? "granted" : await Notification.requestPermission();
      if (permission !== "granted") {
        dispatch({ type: "command_error", detail: "通知权限未开启，设置没有更改。" });
        return false;
      }
    }
    writeNotificationMode(localStorage, mode);
    setNotificationMode(mode);
    return true;
  };
  const activeDevice = remoteDevices.find(
    (device) => device.machine_id === machineId);
  const activeDeviceOnline = state.connState === "connected" && state.wrapperOnline;
  // A native client can advance the transcript without a wrapper-owned turn.
  // Present that mirrored activity as running in every status surface while
  // leaving Composer on the authoritative write state (so a read-only App turn
  // never gains a Stop button it cannot actually control).
  const focusedSessionState = state.sessions.find(
    (session) => session.session_id === focusedSid)?.state;
  const effectiveState = mergeSessionActivityState(
    focusedSessionState, rt.state, rt.mirroredRunning,
  ) ?? rt.state;

  return (
    <div className={"shell" + (sidebarOpen ? " sidebar-open" : "") + ((state.artifact || activeBtw || btwOpening) ? " panel-open" : "")} onTouchStart={onTouchStart} onTouchEnd={onTouchEnd}>
      <SessionsSidebar
        open={sidebarOpen}
        space={space}
        onSpaceChange={switchSpace}
        sessions={state.sessions}
        liveStates={Object.fromEntries(state.sessions.map((session) => {
          const runtime = state.runtimes[session.session_id];
          return [session.session_id, mergeSessionActivityState(
            session.state,
            runtime?.state,
            runtime?.mirroredRunning,
          ) ?? "idle"];
        }))}
        completionBadges={completionBadges}
        activeSessionId={focusedSid}
        onSelect={(id) => {
          if (!confirmArtifactDiscard()) return;
          cancelPendingNotificationTarget();
          const selected = state.sessions.find((s) => s.session_id === id);
          if (selected) focusListedSession(selected);
        }}
        onNew={() => { if (!confirmArtifactDiscard()) return; cancelPendingNotificationTarget(); pendingCreateRef.current = null; setCreateError(null); setStatusOpenSid(null); setNewChatAutoFocus(true); wsRef.current?.setFocusedSid(null); dispatch({ type: "enter_new_chat", cwd: "~", cwdSource: "default" }); if (isMobile()) setSidebarOpen(false); }}
        onNewInDir={(cwd) => { if (!confirmArtifactDiscard()) return; cancelPendingNotificationTarget(); pendingCreateRef.current = null; setCreateError(null); setStatusOpenSid(null); setNewChatAutoFocus(true); wsRef.current?.setFocusedSid(null); dispatch({ type: "enter_new_chat", cwd, cwdSource: "explicit" }); if (isMobile()) setSidebarOpen(false); }}
        onClose={() => setSidebarOpen(false)}
        onRename={(id, title) => wsRef.current?.sendRenameSession(id, title, engine, space)}
        onArchive={(id, archived) => { wsRef.current?.sendArchiveSession(id, archived, engine, space); }}
        onPin={(session, pinned) => {
          const target = sessionCommandTarget(session, engine, space);
          const surfaceKey = `${target.space}:${target.engine}`;
          const cached = sessionListsBySurfaceRef.current[surfaceKey];
          if (cached) {
            sessionListsBySurfaceRef.current[surfaceKey] = setSessionPinned(
              cached, session.session_id, pinned);
          }
          dispatch({ type: "set_session_pinned", sid: session.session_id, pinned });
          wsRef.current?.sendPinSession(
            session.session_id, pinned, target.engine, target.space);
        }}
        onDelete={(id) => {
          const warning = space === "work"
            ? "删除后将永久移除这项工作及其私有文件，确定继续吗？"
            : "删除后将永久移除这条会话历史；代码文件不会被删除，确定继续吗？";
          if (!window.confirm(warning)) return;
          const deleted = state.sessions.find(
            (session) => session.session_id === id);
          const target = deleted
            ? sessionCommandTarget(deleted, engine, space)
            : { engine, space };
          composerDraftsRef.current.delete(composerDraftKey(
            machineId, target.space, target.engine, id,
          ));
          invalidateHistoryPageScopes(id);
          if (focusedSid === id) clearHistoryDetailRequests();
          if (focusedSid === id) dispatch({ type: "enter_new_chat", cwd: "~", cwdSource: "default" });
          wsRef.current?.sendDeleteSession(id, engine, space);
        }}
        onForkWorktree={openForkWorktree}
        onMigrate={openSessionMigration}
      />
      <DirPicker
        open={dirPickerOpen}
        path={state.dirPicker?.path ?? null}
        parent={state.dirPicker?.parent ?? null}
        dirs={state.dirPicker?.dirs ?? []}
        responseRequestId={state.dirPicker?.requestId ?? null}
        onBrowse={(p) => wsRef.current?.sendListDir(p) ?? null}
        onConfirm={(cwd) => { if (state.newChat) dispatch({ type: "set_new_chat_cwd", cwd, cwdSource: "explicit" }); setDirPickerOpen(false); }}
        onClose={() => setDirPickerOpen(false)}
      />
      <DirPicker
        key={`migration-${migrateSession?.session_id ?? "closed"}-${migrateSession?.cwd ?? "unset"}`}
        open={migrateSession !== null}
        path={state.dirPicker?.path ?? null}
        parent={state.dirPicker?.parent ?? null}
        dirs={state.dirPicker?.dirs ?? []}
        responseRequestId={state.dirPicker?.requestId ?? null}
        initialPath={migrateSession?.cwd ?? null}
        title="迁移 Codex 会话"
        confirmLabel="迁移到此目录"
        busy={migrateCreating}
        error={migrateError}
        waitForInitialBrowse
        onBrowse={(p) => wsRef.current?.sendListDir(p) ?? null}
        onConfirm={submitSessionMigration}
        onClose={closeSessionMigration}
      />
      <section className={`pane ${space}-pane`}>
        <header className={`c-head ${space}-head`}>
          <div className="titlewrap">
            <div className="ttl">
              <button className="surface-head-title" onClick={() => setSidebarOpen(true)}>
                <span className="surface-head-mark"><Icon name={space === "work" ? "work" : "code"} size={18} /></span>
                <span>{space === "work" ? "Work" : "Code"}</span>
              </button>
            </div>
            <div className="sub">{space === "work" ? "私有工作区 · " : ""}{rt.ccSessionId ? `session ${rt.ccSessionId.slice(0, 8)}` : "connected"}</div>
          </div>
          <span className={`hstat ${effectiveState}`}><span className="sd" />
            <span className="hstat-label">{effectiveState}</span></span>
          {space === "code" && focusedSid && !state.newChat && (
            <TerminalControl control={rt.control} engine={focusedEngine}
              availability={state.connState !== "connected" || !state.wrapperOnline
                ? "offline" : rt.replaying || !rt.syncReady ? "syncing" : "online"}
              legacyExternal={!rt.control && !!rt.external}
              legacyTakeoverPending={rt.takeoverPending}
              legacyMessage={rt.takeoverMessage}
              onTakeover={() => wsRef.current?.sendTakeover(focusedSid)} />
          )}
          <button className={`device-trigger${activeDeviceOnline ? " online" : ""}`}
            onClick={() => setDeviceSheetOpen(true)} aria-label="设备中心"
            title={`${activeDevice?.label ?? machineId} · ${activeDeviceOnline ? "在线" : "离线"}`}>
            <Icon name="devices" size={18} />
            <span>{activeDevice?.label ?? machineId}</span><i />
          </button>
          <button className="engine-toggle" onClick={toggleEngine} aria-label="切换新会话引擎"
            title="新建会话使用的引擎">{engine === "codex" ? "◇ Codex" : "✳ Claude"}</button>
          <HeaderMenu
            theme={theme}
            notificationMode={notificationMode}
            notificationBinding={pushBinding.state}
            notificationAvailable={typeof Notification !== "undefined"}
            onNotificationMode={updateNotificationMode}
            onToggleTheme={toggleTheme}
            onLogout={() => void logout()}
          />
        </header>

        <ReconnectBanner banner={state.banner}
          replaying={rt.replaying || historyView.recovering}
          truncated={rt.truncated}
          busy={state.connState !== "connected" || !state.wrapperOnline
            || rt.replaying || historyView.recovering}
          onDismiss={dismissBanner} />
        <NoticeStack notices={rt.notices}
          onDismiss={(noticeId) => {
            if (focusedSid) dispatch({ type: "dismiss_notice", sid: focusedSid, noticeId });
          }} />

        {state.newChat ? (
          <NewChatView cwd={state.newChat.cwd}
            controlScopeKey={sessionScopeKey(machineId, engine, space)}
            space={space}
            createError={createError}
            autoFocus={newChatAutoFocus}
            engine={engine}
            catalog={state.catalog}
            model={state.newChat.model}
            effort={state.newChat.effort}
            defaultModel={newChatDefaults.model}
            defaultEffort={newChatDefaults.effort}
            workDashboard={workDashboards[engine] ?? null}
            selectedProjectId={workProjectId}
            onSelectProject={setWorkProjectId}
            onManageWork={() => setWorkManagerOpen(true)}
            onPickCwd={() => setDirPickerOpen(true)}
            onPickModel={pickNewChatModel}
            onPickEffort={pickNewChatEffort}
            permissionProfiles={
              newChatPermissionCatalog?.machineId === machineId
                && newChatPermissionCatalog.cwd === state.newChat.cwd
                ? newChatPermissionCatalog.profiles
                : null
            }
            onGetPermissionProfiles={(cwd) => {
              wsRef.current?.sendGetPermissionProfiles(cwd);
            }}
            onSend={sendFirstMessage} />
        ) : (
          <>
            <ChatView sid={focusedSid} turns={historyView.turns}
              loading={!!rt.loading}
              surface={space}
              engine={focusedEngine} forkingPointId={forkingPointId}
              hasMore={historyView.hasMore}
              historyRevision={rt.historyRevision}
              historyViewRevision={historyView.viewRevision}
              historyViewId={historyView.viewId}
              historyScopeKey={
                historyView.browsing
                  && state.historyBrowse?.sid === focusedSid
                  ? state.historyBrowse.scopeKey
                  : activeScopeKey
              }
              historyWindowEpoch={historyView.windowEpoch}
              historyCursor={historyView.oldestId}
              browseMode={historyView.browsing}
              hasNewer={historyView.hasNewer}
              onLoadMore={loadOlderHistoryPage}
              onLoadNewer={loadNewerHistoryPage}
              onReturnLatest={returnToLatestHistory}
              onLoadDetail={historyView.recovering
                ? undefined : loadHistoryTurnDetail}
              onEdit={historyView.recovering
                ? undefined : (prompt) => setEditPrompt(prompt)}
              onGetDiff={historyView.recovering ? undefined : getDiff}
              onOpenTurnDiff={historyView.recovering
                ? undefined : openTurnDiff}
              onPreviewMarkdown={historyView.recovering
                ? undefined : previewMarkdown}
              onOpenFile={historyView.recovering ? undefined : previewFile}
              onOpenArtifacts={historyView.recovering ? undefined : () => {
                if (focusedSid) {
                  wsRef.current?.sendGetWorkArtifacts(focusedEngine, focusedSid);
                }
                setWorkArtifactsOpen(true);
              }}
              imageAssets={inlineImageAssets}
              onLoadImage={historyView.recovering
                ? undefined : loadFocusedMessageImage}
              historyImageAssets={historyImageAssets}
              onLoadHistoryImage={historyView.recovering
                ? undefined : loadHistoryImage}
              onTextSelectionGuardChange={updateTextSelectionGuard}
              onFork={!historyView.recovering && space === "code"
                ? forkFromTurn : undefined} />

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
          draftKey={focusedComposerDraftKey}
          draftStore={composerDraftsRef.current}
          surface={space}
          state={rt.state}
          catalog={state.catalog}
          connState={state.connState}
          wrapperOnline={state.wrapperOnline}
          sendMode={rt.sendMode}
          setSendMode={(m) => focusedSid && dispatch({
            type: "set_send_mode", sid: focusedSid, mode: m,
          })}
          queue={rt.queue}
          pendingSend={rt.pendingSend}
          failedDeferred={rt.failedDeferred}
          unconfirmedQueued={unconfirmedQueued}
          unconfirmedReplaceable={unconfirmedReplaceable}
          queueCapacity={queueCapacity}
          replaceQueueCapacity={replaceQueueCapacity}
          model={rt.model}
          effort={rt.effort}
          perm={rt.perm}
          permissionProfile={rt.permissionProfile}
          permissionProfiles={rt.permissionProfiles}
          webSearch={rt.webSearch}
          collaborationMode={rt.collaborationMode}
          fast={rt.fast}
          control={rt.control}
          external={rt.external}
          takeoverPending={rt.takeoverPending}
          takeoverMessage={rt.takeoverMessage}
          engine={focusedEngine}
          editPrompt={editPrompt}
          onEditConsumed={() => setEditPrompt(null)}
          onSendQuery={sendQuery}
          onSteerQuery={sendSteer}
          onInterrupt={interrupt}
          onEnqueue={(query) => (
            focusedSid ? sendDeferredQuery(focusedSid, query, "queue") : false
          )}
          onSetPending={(query) => (
            focusedSid ? sendDeferredQuery(focusedSid, query, "replace") : false
          )}
          onRemoveQueued={(query) => {
            if (focusedSid) cancelQueuedQuery(focusedSid, query);
          }}
          onInspectQueued={(query) => {
            if (focusedSid) inspectQueuedQuery(focusedSid, query);
          }}
          onSetModel={setModel}
          onSetEffort={setEffort}
          onSetServiceTier={setServiceTier}
          onSetPerm={setPerm}
          onGetPermissionProfiles={getPermissionProfiles}
          onSetPermissionProfile={setPermissionProfile}
          onSetWebSearch={setWebSearch}
          onSetCollaborationMode={setCollaborationMode}
          onClear={() => dispatch({
            type: "enter_new_chat",
            cwd: space === "work" ? "~" : (currentCwd || "~"),
            cwdSource: space === "work" || !currentCwd ? "default" : "inherited",
          })}
          onContext={requestContext}
          onOpenBtw={openBtw}
          onPreview={previewMarkdown}
          onGoal={runGoal}
          onStatus={openStatus}
          onRefreshUsage={refreshStatus}
          onReview={(target, value) => {
            if (focusedSid) wsRef.current?.sendStartReview(focusedSid, target, value);
          }}
          onCompact={() => {
            if (focusedSid) wsRef.current?.sendCompactSession(focusedSid);
          }}
          onOpenExtensions={(kind) => {
            setCapabilitiesKind(kind);
            setCapabilitiesOpen(true);
            setCapabilitiesLoading(true);
            requestSkillCatalog({
              key: focusedSkillCatalogKey,
              engine: focusedEngine,
              space,
              cwd: capabilityCwd,
              skillsOnly: false,
            }, true);
          }}
          skills={skillCatalogs[focusedSkillCatalogKey]?.items}
          onRequestSkills={() => {
            requestSkillCatalog({
              key: focusedSkillCatalogKey,
              engine: focusedEngine,
              space,
              cwd: capabilityCwd,
              skillsOnly: true,
            });
          }}
          workArtifactCount={space === "work" ? currentWorkArtifacts.length : 0}
          onOpenArtifacts={() => {
            if (focusedSid) {
              wsRef.current?.sendGetWorkArtifacts(focusedEngine, focusedSid);
            }
            setWorkArtifactsOpen(true);
          }}
          contextReport={rt.contextReport}
          contextError={rt.contextError}
          statusReport={rt.statusReport}
          statusError={rt.statusError}
          statusLoading={rt.statusRequestId !== null}
        />
          </>
        )}
        {/* context usage now lives in the composer's ring popover (see Composer) */}
      </section>
      {/* Shared right slot: diff and /btw take turns; header tabs switch. */}
      {(() => {
        const btwShowing = !!activeBtw || btwOpening;
        const view = rightView === "btw" && btwShowing ? "btw"
          : state.artifact ? "diff" : btwShowing ? "btw" : null;
        if (view === "btw")
          return <BtwPanel sid={activeBtwSid ?? undefined} rt={activeBtwSid ? state.runtimes[activeBtwSid] : undefined}
            engine={activeBtw?.engine} opening={btwOpening && !activeBtw}
            active="btw" hasArtifact={!!state.artifact} artifactKind={state.artifact?.kind} onTab={switchRight}
            catalog={state.catalog}
            draftKey={activeBtwDraftKey} draftStore={btwDraftsRef.current}
            sendMode={activeBtwSendMode}
            unconfirmedQueued={unconfirmedQueued}
            unconfirmedReplaceable={btwUnconfirmedReplaceable}
            queueCapacity={queueCapacity}
            replaceQueueCapacity={btwReplaceQueueCapacity}
            onSend={sendBtw}
            onSteer={steerBtw}
            onInterrupt={() => {
              if (activeBtwSid) interruptBtw(activeBtwSid);
            }}
            onSetSendMode={(mode) => {
              if (activeBtwSid) setBtwSendMode(activeBtwSid, mode);
            }}
            onEnqueue={(query) => {
              return activeBtwSid
                ? sendDeferredQuery(activeBtwSid, query, "queue") : false;
            }}
            onSetPending={(query) => {
              return activeBtwSid
                ? sendDeferredQuery(activeBtwSid, query, "replace") : false;
            }}
            onRemoveQueued={(query) => {
              if (activeBtwSid) cancelQueuedQuery(activeBtwSid, query);
            }}
            onInspectQueued={(query) => {
              if (activeBtwSid) inspectQueuedQuery(activeBtwSid, query);
            }}
            onSetModel={(model) => {
              if (activeBtwSid) setBtwModel(activeBtwSid, model);
            }}
            onSetEffort={(effort) => {
              if (activeBtwSid) setBtwEffort(activeBtwSid, effort);
            }}
            onOpenFile={previewBtwFile} onClose={closeBtw}
            onDismissNotice={(noticeId) => {
              if (activeBtwSid) dispatch({ type: "dismiss_notice", sid: activeBtwSid, noticeId });
            }} />;
        if (view === "diff" && state.artifact)
          return <ArtifactPanel artifact={state.artifact} active="diff" hasBtw={!!activeBtw}
            onTab={switchRight} onRefresh={previewArtifactFile}
            onOpenFile={previewArtifactFile} onLoadPreviewAsset={loadPreviewAsset}
            onSaveMarkdown={saveMarkdown} onDirtyChange={setArtifactDirty}
            onClose={() => dispatch({ type: "clear_artifact" })} />;
        return null;
      })()}
      <QueuedQueryDialog
        key={queuedQueryEditor
          ? `${queuedQueryEditor.sid}:${queuedQueryEditor.msgId}`
          : "closed"}
        editor={queuedQueryEditor}
        onClose={() => {
          if (!queuedQueryEditor?.saving) setQueuedQueryEditor(null);
        }}
        onSave={updateQueuedQuery}
        onRetry={retryQueuedQuery} />
      {rt.pendingQuestion && (
        <QuestionSheet
          key={rt.pendingQuestion.ask_id}
          header={rt.pendingQuestion.header}
          question={rt.pendingQuestion.question}
          options={rt.pendingQuestion.options}
          allowText={rt.pendingQuestion.allow_text}
          secret={rt.pendingQuestion.secret}
          multiSelect={rt.pendingQuestion.multi_select}
          onAnswer={(answer) => {
            const question = rt.pendingQuestion;
            if (!focusedSid || !question) return;
            if (!wsRef.current?.sendAnswerQuestion(
              focusedSid, question.ask_id, answer)) return;
            dispatch({
              type: "answer_question",
              sid: focusedSid,
              ask_id: question.ask_id,
            });
          }}
        />
      )}
      <StatusSheet open={shouldOpenCodexStatus(statusOpenSid, focusedSid, focusedEngine)} report={rt.statusReport}
        notices={rt.notices}
        error={rt.statusError}
        onClose={() => setStatusOpenSid(null)}
        onRefresh={openStatus}
        onDismissNotice={(noticeId) => {
          if (focusedSid) dispatch({ type: "dismiss_notice", sid: focusedSid, noticeId });
        }} />
      <ForkWorktreeSheet open={forkWorktreeSession !== null} session={forkWorktreeSession}
        creating={forkWorktreeCreating} error={forkWorktreeError}
        onConfirm={submitForkWorktree} onClose={closeForkWorktree} />
      <WorkDashboardSheet open={workManagerOpen && space === "work"}
        dashboard={workDashboards[engine] ?? null}
        selectedProjectId={workProjectId}
        onSelectProject={setWorkProjectId}
        onClose={() => setWorkManagerOpen(false)}
        onCreateProject={(name, description) => !!wsRef.current?.sendCreateWorkProject(engine, name, description)}
        onDeleteProject={(projectId) => !!wsRef.current?.sendDeleteWorkProject(engine, projectId)}
        onAddSource={(projectId, kind, title, uri, file) => !!wsRef.current?.sendAddWorkSource(engine, projectId, kind, title, uri, file)}
        onDeleteSource={(sourceId) => !!wsRef.current?.sendDeleteWorkSource(engine, sourceId)}
        onCreateSchedule={(title, prompt, nextRunAt, repeatSeconds, projectId) => !!wsRef.current?.sendCreateWorkSchedule(engine, title, prompt, nextRunAt, repeatSeconds, projectId)}
        onDeleteSchedule={(scheduleId) => !!wsRef.current?.sendDeleteWorkSchedule(engine, scheduleId)}
        onCreatePlugin={(name, instructions, projectId) => !!wsRef.current?.sendCreateWorkPlugin(engine, name, instructions, projectId)}
        onDeletePlugin={(pluginId) => !!wsRef.current?.sendDeleteWorkPlugin(engine, pluginId)} />
      <WorkArtifactsSheet open={workArtifactsOpen && space === "work"
          && !state.newChat && currentWorkArtifacts.length > 0}
        artifacts={currentWorkArtifacts}
        onOpen={(path) => { setWorkArtifactsOpen(false); previewFile(path); }}
        onClose={() => setWorkArtifactsOpen(false)} />
      <CapabilitiesSheet open={capabilitiesOpen}
        engine={focusedEngine}
        activeKind={capabilitiesKind}
        readOnly={space === "work"}
        report={capabilitiesByScope[focusedSkillCatalogKey] ?? null}
        loading={capabilitiesLoading}
        onKindChange={setCapabilitiesKind}
        onRefresh={() => {
          setCapabilitiesLoading(true);
          requestSkillCatalog({
            key: focusedSkillCatalogKey,
            engine: focusedEngine,
            space,
            cwd: capabilityCwd,
            skillsOnly: false,
          }, true);
        }}
        onManagePlugin={(item, action) => {
          const verb = action === "install" ? "安装" : "卸载";
          if (!window.confirm(`${verb}插件「${item.name}」将修改本机 ${focusedEngine === "codex" ? "Codex" : "Claude"} 配置，确定继续吗？`)) return;
          setCapabilitiesLoading(true);
          const requestId = wsRef.current?.sendManageEnginePlugin(
            focusedEngine, space, action, item.id,
            capabilityCwd);
          trackCapabilityMutation(requestId, {
            key: focusedSkillCatalogKey, engine: focusedEngine, space,
            cwd: capabilityCwd, skillsOnly: false,
          });
        }}
        onManageSkill={(item: EngineCapabilityItem, action) => {
          const labels = { enable: "启用", disable: "停用", remove: "删除" } as const;
          if (!window.confirm(`${labels[action]} Skill「${item.name}」？${action === "remove" ? "删除会移动到本机可恢复回收目录。" : ""}`)) return;
          setCapabilitiesLoading(true);
          const requestId = wsRef.current?.sendManageEngineSkill(
            focusedEngine, space, action, { skillId: item.id },
            capabilityCwd);
          trackCapabilityMutation(requestId, {
            key: focusedSkillCatalogKey, engine: focusedEngine, space,
            cwd: capabilityCwd, skillsOnly: false,
          });
        }}
        onCreateSkill={(draft: SkillDraft) => {
          setCapabilitiesLoading(true);
          const requestId = wsRef.current?.sendManageEngineSkill(
            focusedEngine, space, "create", draft,
            capabilityCwd);
          trackCapabilityMutation(requestId, {
            key: focusedSkillCatalogKey, engine: focusedEngine, space,
            cwd: capabilityCwd, skillsOnly: false,
          });
        }}
        onRemoveHook={(item: EngineCapabilityItem) => {
          if (!window.confirm(`删除 Hook「${item.name}」？配置文件中的其他内容会原样保留。`)) return;
          setCapabilitiesLoading(true);
          const requestId = wsRef.current?.sendManageEngineHook(
            focusedEngine, space, "remove", { hookId: item.id },
            capabilityCwd);
          trackCapabilityMutation(requestId, {
            key: focusedSkillCatalogKey, engine: focusedEngine, space,
            cwd: capabilityCwd, skillsOnly: false,
          });
        }}
        onCreateHook={(draft: HookDraft) => {
          setCapabilitiesLoading(true);
          const requestId = wsRef.current?.sendManageEngineHook(
            focusedEngine, space, "create", draft,
            capabilityCwd);
          trackCapabilityMutation(requestId, {
            key: focusedSkillCatalogKey, engine: focusedEngine, space,
            cwd: capabilityCwd, skillsOnly: false,
          });
        }}
        onClose={() => setCapabilitiesOpen(false)} />
      <DeviceSheet open={deviceSheetOpen}
        currentId={machineId}
        devices={remoteDevices}
        pairing={devicePairing}
        onDevices={(nextDevices, nextPairing) => {
          setRemoteDevices(nextDevices);
          setDevicePairing(nextPairing);
        }}
        onSelect={(nextMachineId) => {
          cancelPendingNotificationTarget();
          if (nextMachineId !== machineId) setMachineId(nextMachineId);
          setDeviceSheetOpen(false);
        }}
        onClose={() => setDeviceSheetOpen(false)} />
    </div>
  );
}
