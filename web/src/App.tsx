import { DshApi } from "./dsh-api";
import type { DshPanelSelection } from "./components/DshPanel";
import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useReducer,
  useRef,
  useState,
  type TouchEvent,
} from "react";
import { RelayWs, sessionScopeKey, type EventOwnership } from "./ws";
import { RemoteViewerContext, useRemoteViewerLinks } from "./remote-viewer-context";
import { ViewerPagesProvider } from "./components/ViewerPagesProvider";
import { MANUAL_UNREAD_STORAGE } from "./manual-unread-storage";
import type { ViewerPage } from "./remote-viewer";
import { readViewerSelections, writeViewerSelections, viewerScopeKey, setViewerSelection,
  type ViewerSelection } from "./remote-viewer";
import {
  createRuntime,
  deferredQueueCapacity,
  initialState,
  modelCatalogScopeKey,
  nativeProfileSessionId,
  reduce,
  type PendingQuery,
  type PreviewAuthorizationState,
} from "./reducer";
import type { Turn } from "./domain/conversation";
import { uuid } from "./util";
import { TurnFilePageRequests, type LoadTurnFilePage } from "./turn-file-pages";
import { Icon } from "./icons";
import { ChatView } from "./components/ChatView";
import { Composer } from "./components/Composer";
import type { QueuedQueryEditor } from "./components/QueuedQueryDialog";
import { ReconnectBanner } from "./components/ReconnectBanner";
import { NoticeStack } from "./components/NoticeStack";
import { presentCommandProblem } from "./problem-presentation";
import { LoginForm } from "./components/LoginForm";
import {
  compatibleNewChatEffort,
  newChatCatalogRequest,
  reconcileNewChatSelection,
  resolveNewChatLocalDefaults,
} from "./new-chat-selection";
const NewChatView = lazy(() => import("./components/NewChatView").then(m => ({ default: m.NewChatView })));
import { QuestionSheet } from "./components/QuestionSheet";
import { WorkDashboardSheet } from "./components/WorkDashboardSheet";
import type { HookDraft, SkillDraft } from "./components/CapabilitiesSheet";
import { TerminalControl } from "./components/TerminalControl";
import { DeviceSheet, type PairingState, type RemoteDevice } from "./components/DeviceSheet";
import { HeaderMenu } from "./components/HeaderMenu";
import {
  claudeProfileIdForSession,
  claudeProfilePresentation,
  codexProfileIdForSession,
  codexProfilePresentation,
} from "./codex-profile-presentation";
import { parseGoalCommand } from "./goal-command";
import {
  BTW_PANEL_SCOPES_KEY, btwPanelScopeKey, readBtwPanelScopes,
  rekeyBtwPanelScope, setBtwPanelScope,
} from "./btw-panel-state";
import {
  dismissGoalUi,
  goalStableIdentity,
  goalUiScopeForEvent,
  goalUiScopeKey,
  readGoalUiPreferences,
  reconcileGoalUiPreference,
  shouldRecoverGoalUi,
  rekeyGoalUiPreference,
  rememberGoalUi,
  resetGoalDismissMigrationTracking,
  writeGoalUiPreferences,
  type GoalUiPreferences,
} from "./scoped-goal-ui";
import { shouldOpenCodexStatus } from "./status-capabilities";
import { permsFor, type Catalog } from "./data";
import type { AutoCompactSelection } from "./auto-compact";
import {
  normalizeSessionList,
  scopedFocusForSessionList,
  shouldAcceptSessionList,
  updateScopedSessionLifecycle,
} from "./session-list";
import { clearLegacyAuthMarkers, probeSession } from "./session-auth";
import {
  nextActiveDetailRequest,
  nextAutoLoadDetailTurn,
} from "./history-detail-projection";
import {
  canEnqueueQuery,
  collectUnconfirmedQueries,
} from "./runtime-drain";
import type { SendMode } from "./composer-submit";
import { MAX_RUNTIME_SESSIONS } from "./runtime-bounds";
import {
  FORK_FOCUS_REFRESH_MS,
  forkFocusLeaseSession,
  isTerminalSessionMigrationError,
  isTerminalWorktreeForkError,
  matchesSessionForkRequest,
  matchesSessionMigrationRequest,
  matchesWorktreeForkRequest,
  reconcileOpenMigrationSession,
  type PendingSessionFork,
  type ForkFocusLease,
  type PendingSessionMigration,
  type PendingWorktreeFork,
  withoutForkFocusPlaceholder,
} from "./session-worktree";
import { matchesBtwRequest,
  normalizeDiffTheme, normalizeEngine, type Snapshot, type QueryImg,
  type QueryFile, type SessionInfo, type CodexPermissionMode,
  type CodexWebSearchMode, type PermissionProfileInfo,
  type CodexServiceTier, type CollaborationModeName,
  type DiffTheme, type Engine, type Space,
  type SessionControl, type History,
  sessionControlLocksInput } from "./protocol";
import type { EngineCapabilities, EngineCapabilityItem, EngineCapabilityKind, WorkArtifactInfo, WorkDashboard } from "./protocol";
import { isMarkdownPath } from "./preview-path";
import { parseGitDiff } from "./diff";
import { resolveSidebarSwipe } from "./responsive-layout";
import {
  bumpSessionActivity,
  mergeSessionActivityState,
  selectSurfaceSession,
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
  type CancelledHistoryBrowseRequest,
  type HistoryBrowseRequestContext,
  type HistoryDetailRequestContext,
  type HistoryRequestOptions,
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
  cachedLatestRequiresLiveRuntime,
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
  acknowledgeMatchingCompletion,
  catalogCompletionProjection,
  completionAcknowledgementId,
  completionBadgeKind,
  completionNeedsHistoryRepair,
  discardBtwCompletionReceipts,
  idleTurnNeedsHistoryRepair,
  markCompletionUnread,
  nextTerminalHistoryRepairAttempt,
  newestCompletionProjection,
  rekeyCompletionReceipts,
  settleTerminalHistoryRepairAttempt,
  type CompletionBadgeKind,
  type CompletionReceipts,
  type TerminalHistoryRepairAttempt,
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
import {
  completedGoalHasNewerUserTurn,
  latestPlanProgress,
  planFollowsCompletedGoal,
  SessionPlanProgressCache,
} from "./plan-progress";
import {
  ENGINE_SPACES_KEY,
  LEGACY_SPACE_KEY,
  readEngineSpaces,
  rememberEngineSpace,
} from "./surface-preferences";
import {
  activeTurnCandidateIds,
  displayActiveTurnOwnerId,
} from "./process-blocks";
import type { AgentDetail, FilesListed } from "./protocol";
import type { AgentDetailSelection } from "./components/AgentDetailController";
import type { RightPanelView } from "./components/PanelTabs";

const THEME_KEY = "cc_remote_theme";
const ENGINE_KEY = "cc_remote_engine";  // which backend the NEXT new session uses
const MACHINE_KEY = "cc_remote_machine";
const DshGoalPanel = lazy(() => import("./components/DshGoalPanel"));
const GoalPanel = lazy(() => import("./components/GoalPanel").then(
  ({ GoalPanel: Panel }) => ({ default: Panel }),
));
const BtwPanel = lazy(() => import("./components/BtwPanel").then(
  ({ BtwPanel: Panel }) => ({ default: Panel }),
));
const ArtifactPanel = lazy(() => import("./components/ArtifactPanel").then(
  ({ ArtifactPanel: Panel }) => ({ default: Panel }),
));
const SessionFilesPanel = lazy(() => import("./components/SessionFilesPanel"));
const DirPicker = lazy(() => import("./components/DirPicker").then(
  ({ DirPicker }) => ({ default: DirPicker }),
));
const QueuedQueryDialog = lazy(() => import("./components/QueuedQueryDialog").then(
  ({ QueuedQueryDialog: Dialog }) => ({ default: Dialog }),
));
const RemoteViewerPanel = lazy(() => import("./components/RemoteViewerPanel").then(
  ({ RemoteViewerPanel: Panel }) => ({ default: Panel }),
));
const StatusSheet = lazy(() => import("./components/StatusSheet").then(
  ({ StatusSheet: Sheet }) => ({ default: Sheet }),
));
const ForkWorktreeSheet = lazy(() => import("./components/ForkWorktreeSheet").then(
  ({ ForkWorktreeSheet: Sheet }) => ({ default: Sheet }),
));
const UsageActivitySheet = lazy(() => import("./components/UsageActivitySheet").then(
  ({ UsageActivitySheet: Sheet }) => ({ default: Sheet }),
));
const WorkArtifactsSheet = lazy(() => import("./components/WorkArtifactsSheet").then(
  ({ WorkArtifactsSheet: Sheet }) => ({ default: Sheet }),
));
const CapabilitiesSheet = lazy(() => import("./components/CapabilitiesSheet").then(
  ({ CapabilitiesSheet: Sheet }) => ({ default: Sheet }),
));
const AgentDetailController = lazy(() => import("./components/AgentDetailController").then(
  ({ AgentDetailController: Controller }) => ({ default: Controller }),
));
const DshPanel = lazy(() => import("./components/DshPanel"));
const SessionsSidebar = lazy(() => import("./components/SessionsSidebar").then(
  ({ SessionsSidebar: Sidebar }) => ({ default: Sidebar }),
));

interface QueuedQueryEditorState extends QueuedQueryEditor {
  detailRequestId: string | null;
  updateRequestId: string | null;
  pendingPrompt: string | null;
}

const MAX_TERMINAL_HISTORY_REPAIR_ATTEMPTS = 2;

// The sidebar is an overlay on mobile (<980px, matches index.css) but a
// persistent grid column on desktop. So auto-close it after picking a session
// ONLY on mobile; on desktop keep it open.
const isMobile = () => window.matchMedia("(max-width: 979px)").matches;

/** Present one account's catalog under the historical engine key used by
 * model pickers. The underlying cache remains profile-keyed, so a missing
 * secondary-account response never falls back to another account's models. */
function catalogForEngineProfile(
  catalog: Catalog,
  engine: Engine,
  profileId?: string | null,
): Catalog {
  const scoped = catalog[modelCatalogScopeKey(engine, profileId)];
  return {
    ...catalog,
    [engine]: scoped ?? (profileId ? [] : (catalog[engine] ?? [])),
  };
}

export default function App() {
  const [theme, setTheme] = useState<DiffTheme>(
    () => normalizeDiffTheme(localStorage.getItem(THEME_KEY)));
  const initialEngineRef = useRef(normalizeEngine(localStorage.getItem(ENGINE_KEY)));
  const initialSpacesRef = useRef(readEngineSpaces(localStorage, initialEngineRef.current));
  const [engine, setEngine] = useState<Engine>(initialEngineRef.current);
  const [space, setSpace] = useState<Space>(initialSpacesRef.current[initialEngineRef.current]);
  const spacesByEngineRef = useRef<Record<Engine, Space>>(initialSpacesRef.current);
  const [authed, setAuthed] = useState(false);
  const [authReady, setAuthReady] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [dirPickerOpen, setDirPickerOpen] = useState(false);
  const [dshPresetSelection, setDshPresetSelection] = useState<{ scope: string; value: string } | null>(null);
  const [createError, setCreateError] = useState<string | null>(null);
  const [newChatAutoFocus, setNewChatAutoFocus] = useState(true);
  // A cold Work/Code or engine switch has no trustworthy row to paint until
  // its scoped SessionList arrives. Keep that gap non-interactive: rendering a
  // default `~` composer here can create a real session in the wrong cwd before
  // the wrapper has had a chance to restore the remembered conversation.
  const [restoringSurfaceScope, setRestoringSurfaceScope] =
    useState<string | null>(null);
  const [editPrompt, setEditPrompt] = useState<string | null>(null);
  const [queuedQueryEditor, setQueuedQueryEditor] =
    useState<QueuedQueryEditorState | null>(null);
  // The right slot is shared by artifacts and /btw.
  const [btwPanelScopes, setBtwPanelScopes] = useState(
    () => readBtwPanelScopes(sessionStorage));
  const [rightView, setRightView] = useState<RightPanelView>(
    btwPanelScopes.length ? "btw" : "diff");
  const [agentPanel, setAgentPanel] = useState<AgentDetailSelection | null>(null);
  const [fileBrowser, setFileBrowser] = useState<{
    sid: string; machineId: string; engine: string; space: string;
    path: string; preview: boolean; id: string;
  } | null>(null);
  const filesListenerRef = useRef<((message: FilesListed) => void) | null>(null);
  const listenFiles = useCallback((listener: ((message: FilesListed) => void) | null) => {
    filesListenerRef.current = listener;
  }, []);
  const [viewerSelections, setViewerSelections] = useState(() => readViewerSelections(sessionStorage));
  const [viewerUrl, setViewerUrl] = useState<{ key: string; href: string; openId: string } | null>(null);
  useEffect(() => writeViewerSelections(sessionStorage, viewerSelections), [viewerSelections]);
  const agentDetailListenerRef = useRef<((message: AgentDetail) => void) | null>(null);
  const setAgentDetailListener = useCallback(
    (listener: ((message: AgentDetail) => void) | null) => {
      agentDetailListenerRef.current = listener;
    }, []);
  // true from the moment /btw is clicked until the fork's btw_opened arrives — so
  // the panel appears instantly (spinner) instead of waiting ~1s for the fork.
  const [btwOpeningByParentSid, setBtwOpeningByParentSid] = useState<Record<string, boolean>>({});
  // Goal remains opt-in, but its reveal preference is scoped across machines,
  // Work/Code and engines so a refresh can restore the correct session only.
  const [goalUiByScope, setGoalUiByScope] = useState<Record<string, {
    revealed: boolean;
    open: boolean;
    loading: boolean;
  }>>({});
  const [statusOpenSid, setStatusOpenSid] = useState<string | null>(null);
  const [usageActivityOpen, setUsageActivityOpen] = useState(false);
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
  const [workDashboardMachineId, setWorkDashboardMachineId] =
    useState(machineId);
  const [workArtifactsBySid, setWorkArtifactsBySid] = useState<Record<string, WorkArtifactInfo[]>>({});
  const [completionReceipts, setCompletionReceipts] = useState<CompletionReceipts>({});
  const completionReceiptsRef = useRef(completionReceipts);
  completionReceiptsRef.current = completionReceipts;
  const [btwSendModeBySid, setBtwSendModeBySid] = useState<
    Record<string, SendMode>
  >({});
  const [newChatPermissionCatalog, setNewChatPermissionCatalog] = useState<{
    machineId: string;
    cwd: string;
    codexProfileId: string | null;
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
  const wsRef = useRef<RelayWs | null>(null);
  const [dshApi] = useState(() => new DshApi(() => wsRef.current));
  const [dshPanel, setDshPanel] = useState<{ sid: string; scope: string; selection: DshPanelSelection } | null>(null);
  const [permissionProfileRequests, setPermissionProfileRequests] =
    useState<Record<string, string>>({});
  const archivedBrowseRef = useRef<string | null>(null);
  // Reducer state becomes visible after React commits. Keep the command id in a
  // synchronous ref as well so a close/reopen double click (or two idle frames
  // in one WebSocket batch) cannot enqueue duplicate native Context RPCs.
  const contextRequestLaunchesRef = useRef<Map<string, string>>(new Map());
  // An idle State can be published a few milliseconds before the managed
  // Claude runner drops its final task/write claims.  Bound the compensating
  // retries for that narrow finalizer race; a genuinely active turn still
  // waits for its next authoritative idle frame.
  const contextDeferredRetryAttemptsRef = useRef<Map<string, number>>(new Map());
  const wsLifecycleEpochRef = useRef(0);
  const goalUiPreferencesRef = useRef<GoalUiPreferences>(
    readGoalUiPreferences(localStorage));
  const goalRecoveryRequestsRef = useRef<Set<string>>(new Set());
  const goalDismissMigrationsRef = useRef<Set<string>>(new Set());
  const goalDismissMigrationByRequestRef = useRef<Map<string, string>>(
    new Map());
  const planProgressCacheRef = useRef(new SessionPlanProgressCache());
  const goalRequestScopeByIdRef = useRef<Map<
    string, { scopeKey: string; sid: string }
  >>(new Map());
  const persistGoalUiPreferences = useCallback((next: GoalUiPreferences) => {
    goalUiPreferencesRef.current = writeGoalUiPreferences(localStorage, next);
  }, []);
  const sendContextRequestTo = useCallback((
    sid: string,
    refresh: boolean,
    transport: RelayWs | null = wsRef.current,
  ): string | null => {
    const runtime = stateRef.current.runtimes[sid];
    if (runtime?.contextRequestId
        || contextRequestLaunchesRef.current.has(sid)) return null;
    const requestId = transport?.sendGetContextTo(sid, refresh) ?? null;
    if (!requestId) return null;
    contextRequestLaunchesRef.current.set(sid, requestId);
    dispatch({ type: "begin_context_request", sid, requestId });
    return requestId;
  }, []);
  const resumeListedSession = useCallback((
    session: SessionInfo,
    targetEngine: Engine,
    targetSpace: Space,
    transport: RelayWs | null = wsRef.current,
  ) => {
    if (session.tag === "archived") {
      archivedBrowseRef.current = session.session_id;
      return;
    }
    archivedBrowseRef.current = null;
    transport?.sendSwitchSession(
      session.session_id, targetEngine, targetSpace);
  }, []);
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
        request.codexProfileId,
        request.claudeProfileId,
        request.sid,
      ) ?? null,
    );
  }
  const focusedSkillScopeRef = useRef<SkillCatalogRequest | null>(null);
  const historyRequestsRef = useRef(new HistoryRequestCoordinator());
  const turnFileRequestsRef = useRef(new TurnFilePageRequests());
  const terminalHistoryRepairRef = useRef<Map<
    string, TerminalHistoryRepairAttempt
  >>(new Map());
  const [terminalHistoryRepairEpoch, setTerminalHistoryRepairEpoch] =
    useState(0);
  const btwHydrationKeyRef = useRef("");
  const historyDetailRequestsRef = useRef(new HistoryDetailRequestCoordinator(
    (context) => {
      dispatch({ type: "history_detail_cancelled", context });
    },
  ));
  const historyDetailResetAttemptsRef = useRef(new Set<string>());
  const clearHistoryDetailRequests = useCallback(() => {
    for (const context of historyDetailRequestsRef.current.clear()) {
      dispatch({ type: "history_detail_cancelled", context });
    }
  }, []);
  const settleCancelledHistoryBrowse = useCallback((
    cancelled: readonly CancelledHistoryBrowseRequest[],
  ) => {
    for (const request of cancelled) {
      const browse = request.browse;
      dispatch({
        type: "history_browse_page_failed",
        sid: request.sid,
        scopeKey: browse.scopeKey,
        revision: request.revision,
        generation: request.generation,
        viewId: browse.viewId,
        windowEpoch: browse.windowEpoch,
        before: browse.pendingBefore,
      });
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
  const forkFocusLeaseRef = useRef<ForkFocusLease | null>(null);
  const forkFocusLeaseTimerRef = useRef<number | null>(null);
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
  // Correlate each pending creation with its original parent. Navigating away
  // never destroys the result: the owner-scoped open remains pinned there
  // until its explicit close button is used.
  const btwRequestParentsRef = useRef<Map<string, string>>(new Map());
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
  const clearForkFocusLease = useCallback((
    refresh = false,
    dropPlaceholder = true,
  ) => {
    const lease = forkFocusLeaseRef.current;
    if (forkFocusLeaseTimerRef.current !== null) {
      window.clearTimeout(forkFocusLeaseTimerRef.current);
      forkFocusLeaseTimerRef.current = null;
    }
    if (!lease) return;
    forkFocusLeaseRef.current = null;
    if (!dropPlaceholder) return;
    const surfaceKey = `${lease.space}:${lease.engine}`;
    const current = sessionListsBySurfaceRef.current[surfaceKey] ?? [];
    const sessions = withoutForkFocusPlaceholder(current, lease);
    sessionListsBySurfaceRef.current[surfaceKey] = sessions;
    historySessionListsRef.current[surfaceKey] = sessions;
    dispatch({
      type: "drop_fork_placeholder",
      sid: lease.childSessionId,
      parentSid: lease.parentSessionId,
    });
    if (refresh) wsRef.current?.sendListSessions(lease.engine, lease.space);
  }, []);
  const startForkFocusLease = useCallback((lease: ForkFocusLease) => {
    // Replacing one successful fork with another is explicit navigation. The
    // old synthetic row no longer has a lease and must not linger undeletable.
    clearForkFocusLease(false, true);
    forkFocusLeaseRef.current = lease;
    const refresh = () => {
      const current = forkFocusLeaseRef.current;
      if (current?.requestId !== lease.requestId
          || current.childSessionId !== lease.childSessionId) return;
      wsRef.current?.sendListSessions(current.engine, current.space);
      current.refreshAt = Date.now() + FORK_FOCUS_REFRESH_MS;
      forkFocusLeaseTimerRef.current = window.setTimeout(
        refresh, FORK_FOCUS_REFRESH_MS);
    };
    forkFocusLeaseTimerRef.current = window.setTimeout(
      refresh, Math.max(0, lease.refreshAt - Date.now()));
  }, [clearForkFocusLease]);
  useEffect(() => () => {
    if (forkFocusLeaseTimerRef.current !== null) {
      window.clearTimeout(forkFocusLeaseTimerRef.current);
    }
  }, []);
  const requestHistory = useCallback((
    sid: string,
    before: string | null | undefined,
    limit: number,
    generation?: string | null,
    revision?: string | null,
    browse?: HistoryBrowseRequestContext,
    options?: HistoryRequestOptions,
  ) => {
    const ws = wsRef.current;
    if (!ws) return false;
    const current = stateRef.current;
    const completion = newestCompletionProjection(
      current.runtimes[sid]?.completion,
      catalogCompletionProjection(current.sessions.find(
        (session) => session.session_id === sid)),
    );
    const requestOptions: HistoryRequestOptions = {
      ...options,
      causalKey: options?.causalKey === undefined
        ? completion?.id ?? null
        : options.causalKey,
    };
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
    ), settleCancelledHistoryBrowse, requestOptions);
  }, [settleCancelledHistoryBrowse]);
  const settleTerminalHistoryRepair = useCallback((
    sid: string,
    causalKey: string,
  ) => {
    const current = terminalHistoryRepairRef.current.get(sid);
    if (!current || causalKey !== current.key) return;
    const settled = settleTerminalHistoryRepairAttempt(current);
    if (!settled) return;
    terminalHistoryRepairRef.current.set(sid, settled);
    // The matching History reducer runs in the same inbound-message batch.
    // This local epoch gives the repair effect an explicit response boundary,
    // including when the reducer correctly rejects an older build_seq page.
    setTerminalHistoryRepairEpoch((value) => value + 1);
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
    rightView: RightPanelView;
    getDiff: (file: string) => void;
    openBtw: () => void;
    collapseBtw: () => void;
  }>({ artifact: null, btwSid: null, rightView: "diff",
    getDiff: () => {}, openBtw: () => {}, collapseBtw: () => {} });

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
    clearForkFocusLease(false);
    setMigrateSession(null);
    setMigrateCreating(false);
    setMigrateError(null);
    pendingBtwByParentRef.current.clear();
    activeBtwByParentRef.current.clear();
    btwRequestParentsRef.current.clear();
    setBtwOpeningByParentSid({});
    setBtwSendModeBySid({});
    setQueuedQueryEditor(null);
    btwDraftsRef.current.clear();
    setCompletionReceipts({});
    resetGoalDismissMigrationTracking(
      goalDismissMigrationsRef.current,
      goalDismissMigrationByRequestRef.current,
    );
    planProgressCacheRef.current.reset();
    sessionListsBySurfaceRef.current = {};
    historySessionListsRef.current = {};
    preferredSurfaceFocusRef.current = null;
    authoritativeSurfaceListsRef.current.clear();
    notificationListRequestRef.current = null;
    setRestoringSurfaceScope(null);
    sessionActivityPendingRef.current.clear();
    skillCatalogsRef.current = {};
    skillCatalogRequestsRef.current?.reset();
    setSkillCatalogs({});
    setCapabilitiesByScope({});
    setStatusOpenSid(null);
    setUsageActivityOpen(false);
    setWorkManagerOpen(false);
    setWorkProjectId(null);
    setWorkDashboards({});
    setWorkDashboardMachineId(machineId);
    setWorkArtifactsOpen(false);
    setWorkArtifactsBySid({});
    setGoalUiByScope((current) => Object.fromEntries(
      Object.entries(current).map(([key, value]) => [
        key, { ...value, open: false },
      ]),
    ));
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
  }, [clearForkFocusLease, clearHistoryDetailRequests, machineId]);

  // The focused session's runtime (turns/state/model/perm/queue/...). Falls back
  // to an empty runtime before any session is focused.
  const focusedSid = state.focusedSid;
  const visibleParentSid = state.newChat || previousMachineRef.current !== machineId
    ? null : focusedSid;
  const btwPanelKey = visibleParentSid
    ? btwPanelScopeKey(machineId, space, engine, visibleParentSid) : null;
  const btwPanelVisible = !!btwPanelKey && btwPanelScopes.includes(btwPanelKey);
  const btwPanelVisibleRef = useRef(btwPanelVisible);
  btwPanelVisibleRef.current = btwPanelVisible;
  const activeBtwGroup = visibleParentSid
    ? state.btwByParentSid[visibleParentSid] : undefined;
  const activeBtw = activeBtwGroup?.chats.find(
    (chat) => chat.sid === activeBtwGroup.activeSid);
  const activeBtwSid = activeBtw?.sid ?? null;
  const btwOpening = visibleParentSid
    ? !!btwOpeningByParentSid[visibleParentSid] : false;
  // The rendered slot owns desktop split space, never retained chat data.
  // A hidden BTW can keep running; an empty/opening visible BTW still needs room.
  const btwShowing = btwPanelVisible && !!visibleParentSid;
  const viewerKey = visibleParentSid
    ? viewerScopeKey({ machineId, space, engine, sid: visibleParentSid }) : null;
  const viewerShowing = !!viewerKey && Object.hasOwn(viewerSelections, viewerKey);
  const closeViewer = useCallback(() => {
    if (!viewerKey) return;
    setViewerSelections((current) => {
      const next = { ...current };
      delete next[viewerKey];
      return next;
    });
    setViewerUrl(null);
  }, [viewerKey]);
  const openViewer = useCallback((href?: string) => {
    if (!viewerKey || !confirmArtifactDiscard()) return;
    setAgentPanel(null);
    setViewerSelections((current) => setViewerSelection(current, viewerKey, href ? null : current[viewerKey] ?? null));
    setViewerUrl(href ? { key: viewerKey, href, openId: uuid() } : null);
  }, [viewerKey, confirmArtifactDiscard]);
  const openViewerLink = useRemoteViewerLinks(
    authed && state.connState === "connected" ? viewerKey : null, openViewer);
  const openViewerPage = useCallback((page: ViewerPage) => {
    if (!viewerKey || !confirmArtifactDiscard()) return;
    setAgentPanel(null);
    setViewerUrl(null);
    setViewerSelections((current) => setViewerSelection(current, viewerKey,
      { machine_id: page.machine_id, site_id: page.site_id, entry: page.entry }));
  }, [viewerKey, confirmArtifactDiscard]);
  const selectViewer = (selection: ViewerSelection | null) => {
    if (viewerKey) setViewerSelections((current) => setViewerSelection(current, viewerKey, selection));
  };
  const filesShowing = fileBrowser?.sid === visibleParentSid
    && fileBrowser?.machineId === machineId && fileBrowser?.engine === engine
    && fileBrowser?.space === space;
  const visibleRightPanel = viewerShowing ? "viewer" : agentPanel ? "agent"
    : filesShowing ? "files"
    : rightView === "btw" && btwShowing ? "btw"
      : state.artifact ? "diff" : btwShowing ? "btw" : null;
  // Questions, hydration and completion receipts must agree with the rendered
  // slot, including when an agent or artifact covers a retained side chat.
  const visibleBtwSid = visibleRightPanel === "btw" ? activeBtwSid : null;
  const visibleBtwSidRef = useRef(visibleBtwSid);
  visibleBtwSidRef.current = visibleBtwSid;
  const activeBtwQuestionVisible = !!(
    visibleBtwSid && state.runtimes[visibleBtwSid]?.pendingQuestion
  );
  useEffect(() => {
    if (!visibleBtwSid
        || state.connState !== "connected" || !state.wrapperOnline) {
      btwHydrationKeyRef.current = "";
      return;
    }
    if (btwHydrationKeyRef.current === visibleBtwSid) return;
    if (wsRef.current?.sendSyncBtw(visibleBtwSid)) {
      btwHydrationKeyRef.current = visibleBtwSid;
    }
  }, [
    visibleBtwSid,
    state.connState,
    state.wrapperOnline,
  ]);
  const completionBadgeSids = new Set([
    ...Object.keys(completionReceipts),
    ...state.sessions.filter(
      (session) => session.completion_unread === true,
    ).map((session) => session.session_id),
    ...Object.keys(state.runtimes).filter(
      (sid) => state.runtimes[sid]?.completion?.unread === true),
  ]);
  const catalogCompletionBySid = new Map(state.sessions.flatMap((session) => {
    const completion = catalogCompletionProjection(session);
    return completion
      ? [[session.session_id, completion] as const]
      : [];
  }));
  const completionBadges = Object.fromEntries(
    [...completionBadgeSids].flatMap((sid) => {
      const local = completionReceipts[sid] ?? {
        main: false, mainCompletionId: null,
        mainTurnEndSeq: null, mainTurnEndGeneration: null, btwSids: [],
      };
      const authoritative = newestCompletionProjection(
        state.runtimes[sid]?.completion,
        catalogCompletionBySid.get(sid),
      );
      const kind = completionBadgeKind(local, authoritative?.unread);
      return kind ? [[sid, kind]] : [];
    }),
  ) as Record<string, CompletionBadgeKind>;
  const activeScopeKey = sessionScopeKey(machineId, engine, space);
  useEffect(() => () => dshApi.reset(), [activeScopeKey, dshApi]);
  const activeWorkDashboard = workDashboardMachineId === machineId
    ? workDashboards[engine] ?? null
    : null;
  const activeWorkProjectId = workDashboardMachineId === machineId
    ? workProjectId
    : null;
  const currentCwd = state.cwdByScope[activeScopeKey] ?? "";
  const newChatCwd = state.newChat?.cwd ?? null;
  const knownClaudeProfileIds = new Set(
    state.claudeProfiles.map((profile) => profile.id));
  const knownCodexProfileIds = new Set(
    state.codexProfiles.map((profile) => profile.id));
  const requestedNewChatClaudeProfileId = engine === "claude"
    ? state.newChat?.claudeProfileId
      ?? state.claudeProfileByScope[activeScopeKey]
      ?? state.defaultClaudeProfileId
    : null;
  const requestedNewChatCodexProfileId = engine === "codex"
    ? state.newChat?.codexProfileId
      ?? state.codexProfileByScope[activeScopeKey]
      ?? state.defaultCodexProfileId
    : null;
  // Preserve an explicitly selected id even if a later registry refresh drops
  // it. Silently replacing a drafted Work turn with the default account would
  // cross the user's billing and conversation boundary.
  const newChatClaudeProfileId = engine === "claude"
    ? requestedNewChatClaudeProfileId
      ?? state.claudeProfiles.find(
        (profile) => profile.id === state.defaultClaudeProfileId)?.id
      ?? state.claudeProfiles[0]?.id
      ?? null
    : null;
  const newChatCodexProfileId = engine === "codex"
    ? requestedNewChatCodexProfileId
      ?? state.codexProfiles.find(
        (profile) => profile.id === state.defaultCodexProfileId)?.id
      ?? state.codexProfiles[0]?.id
      ?? null
    : null;
  const newChatCodexProfileMissing = engine === "codex"
    && !!newChatCodexProfileId
    && !knownCodexProfileIds.has(newChatCodexProfileId);
  const newChatClaudeProfileMissing = engine === "claude"
    && !!newChatClaudeProfileId
    && !knownClaudeProfileIds.has(newChatClaudeProfileId);
  const newChatProfileId = engine === "codex"
    ? newChatCodexProfileId : newChatClaudeProfileId;
  const newChatCatalogScopeKey = modelCatalogScopeKey(
    engine, newChatProfileId);
  const newChatCatalog = catalogForEngineProfile(
    state.catalog, engine, newChatProfileId);
  const newChatDefaults = resolveNewChatLocalDefaults(
    engine,
    space,
    newChatCwd ?? "",
    state.catalogDefault,
    state.catalogDefaultEffort,
    state.catalogDefaultCwd,
    newChatCatalogScopeKey,
  );
  const rt = state.runtimes[focusedSid ?? ""] ?? createRuntime();
  const historyView = displayHistoryProjection(
    state.historyRecovery,
    focusedSid,
    rt,
    state.historyBrowse,
    state.retainedHistoryBrowse,
  );
  const focusedSession = state.sessions.find(
    (session) => session.session_id === focusedSid);
  const archivedBrowse = focusedSession?.tag === "archived";
  const focusedEngine = (focusedSession?.engine ?? engine) as "claude" | "codex" | "dsh";
  useEffect(() => {
    if (!focusedSid || state.newChat) {
      archivedBrowseRef.current = null;
      return;
    }
    const focusedSpace = focusedSession?.space === "work" ? "work" : space;
    if (focusedSession?.tag === "archived") {
      archivedBrowseRef.current = focusedSid;
      return;
    }
    const archivedBrowse = archivedBrowseRef.current;
    if (archivedBrowse !== focusedSid) return;
    if (state.connState !== "connected" || !state.wrapperOnline) return;
    archivedBrowseRef.current = null;
    wsRef.current?.sendSwitchSession(
      focusedSid, focusedEngine, focusedSpace);
  }, [
    focusedEngine,
    focusedSession?.space,
    focusedSession?.tag,
    focusedSid,
    space,
    state.connState,
    state.newChat,
    state.wrapperOnline,
  ]);
  useEffect(() => {
    setAgentPanel(null);
  }, [activeScopeKey, focusedSid, focusedEngine, space, rt.historyRevision]);
  const focusedCodexProfileId = focusedEngine === "codex"
    ? focusedSession?.codex_profile_id
      ?? codexProfileIdForSession(focusedSid, state.defaultCodexProfileId)
    : null;
  const focusedClaudeProfileId = focusedEngine === "claude"
    ? focusedSession?.claude_profile_id
      ?? claudeProfileIdForSession(focusedSid, state.defaultClaudeProfileId)
    : null;
  const focusedAccountProfileId = focusedEngine === "codex"
    ? focusedCodexProfileId : focusedClaudeProfileId;
  const focusedWorkProfile = space === "work" && !state.newChat
    && focusedAccountProfileId
    ? (focusedEngine === "codex"
      ? codexProfilePresentation(
        state.codexProfiles,
        state.defaultCodexProfileId,
        focusedAccountProfileId,
      )
      : claudeProfilePresentation(
        state.claudeProfiles,
        state.defaultClaudeProfileId,
        focusedAccountProfileId,
      ))
    : null;
  const focusedNativeSessionId = focusedSession?.native_session_id
    ?? (rt.ccSessionId
      ? nativeProfileSessionId(rt.ccSessionId)
      : rt.ccSessionId);
  const focusedCatalog = catalogForEngineProfile(
    state.catalog, focusedEngine, focusedAccountProfileId);
  const completedGoalRetired = completedGoalHasNewerUserTurn(
    rt.goal, rt.turns,
  ) || completedGoalHasNewerUserTurn(rt.goal, historyView.turns);
  // A live plan always wins over plans found while browsing older history.
  // When the runtime's small newest-turn window has no plan, the displayed
  // history may still reveal the plan which owns this session's long task.
  const runtimePlanProgress = latestPlanProgress(rt.turns);
  const historyPlanProgress = historyView.browsing || historyView.recovering
    ? latestPlanProgress(historyView.turns) : null;
  let planProgress = focusedSid
      ? planProgressCacheRef.current.resolve({
        machineId,
        engine: focusedEngine,
        space,
        sid: focusedSid,
        runtime: runtimePlanProgress,
        history: historyPlanProgress,
        runtimeTurns: rt.turns,
        historyTurns: historyView.turns,
        recovering: historyView.recovering,
        runtimeLoading: rt.loading === true,
      })
    : null;
  // An unfinished snapshot from the completed Goal must not briefly become the
  // standalone monitor for the next task. Keep only a Plan whose owning user
  // turn starts after the authoritative Goal completion boundary.
  if (completedGoalRetired && planProgress
      && !planFollowsCompletedGoal(rt.goal, planProgress)) {
    planProgressCacheRef.current.clear({
      machineId, engine: focusedEngine, space, sid: focusedSid!,
    });
    planProgress = null;
  }
  const planProgressSource = planProgress?.source ?? null;
  const capabilityCwd = focusedSession?.cwd || currentCwd;
  const focusedComposerDraftKey = composerDraftKey(
    machineId, space, focusedEngine, focusedSid ?? "",
  );
  const focusedSkillCatalogKey = skillCatalogKey(
    machineId, focusedEngine, space, capabilityCwd,
    focusedCodexProfileId, focusedClaudeProfileId, focusedSid);
  focusedSkillScopeRef.current = {
    key: focusedSkillCatalogKey,
    engine: focusedEngine,
    space,
    cwd: capabilityCwd,
    skillsOnly: true,
    claudeProfileId: focusedClaudeProfileId,
    codexProfileId: focusedCodexProfileId,
    sid: focusedSid,
  };
  const activeBtwDraftKey = composerDraftKey(
    machineId, space,
    (activeBtw?.engine === "codex" ? "codex" : "claude"),
    `btw:${activeBtwSid ?? visibleParentSid ?? "opening"}`,
  );
  const inlineImageAssets = focusedSid
    ? inlineImageAssetsRef.current.forSession(focusedSid) : {};
  const btwInlineImageAssets = activeBtwSid
    ? inlineImageAssetsRef.current.forSession(activeBtwSid) : {};
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
  const focusedGoalScopeKey = focusedSid
    ? goalUiScopeKey(machineId, space, focusedEngine, focusedSid) : null;
  const storedGoalPreference = focusedGoalScopeKey
    ? goalUiPreferencesRef.current[focusedGoalScopeKey] : undefined;
  const storedGoalIdentity = goalStableIdentity(rt.goal);
  const goalUi = focusedGoalScopeKey
    ? goalUiByScope[focusedGoalScopeKey] ?? (storedGoalPreference?.known
      ? {
          revealed: rt.goalDismissed ? false : storedGoalPreference.hiddenGoal
            ? !!rt.goal && storedGoalPreference.hiddenGoal !== storedGoalIdentity
            : !!rt.goal || shouldRecoverGoalUi(storedGoalPreference),
          open: false,
          loading: !rt.goal,
        }
      : undefined)
    : undefined;
  const focusedCompletion = newestCompletionProjection(
    rt.completion,
    catalogCompletionProjection(focusedSession),
  );
  const terminalRepairState = mergeSessionActivityState(
    focusedSession?.state,
    rt.state,
    rt.mirroredRunning,
  );

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
    if (focusedEngine === "dsh" || !authed || !focusedSid || !focusedGoalScopeKey || state.newChat
        || archivedBrowse
        || state.connState !== "connected" || !state.wrapperOnline) {
      return;
    }
    const requestKey = `${wsLifecycleEpochRef.current}\0${focusedGoalScopeKey}`;
    if (goalRecoveryRequestsRef.current.has(requestKey)) return;
    const requestId = wsRef.current?.sendGetGoalTo(focusedSid) ?? null;
    if (!requestId) return;
    goalRecoveryRequestsRef.current.add(requestKey);
    goalRequestScopeByIdRef.current.set(requestId, {
      scopeKey: focusedGoalScopeKey,
      sid: focusedSid,
    });
    setGoalUiByScope((current) => {
      if (current[focusedGoalScopeKey]?.open) return current;
      const preference = goalUiPreferencesRef.current[focusedGoalScopeKey];
      const runtime = stateRef.current.runtimes[focusedSid];
      const goal = runtime?.goal;
      return {
        ...current,
        [focusedGoalScopeKey]: {
          // Discover an existing Goal silently on a new browser/device. The
          // authoritative non-null response reveals it unless the wrapper says
          // this exact generation was dismissed. Legacy local dismissals are
          // migrated when that response arrives.
          revealed: goal
            ? !runtime.goalDismissed
              && preference?.hiddenGoal !== goalStableIdentity(goal)
            : shouldRecoverGoalUi(preference),
          open: false,
          loading: !goal,
        },
      };
    });
  }, [
    authed,
    archivedBrowse,
    focusedEngine,
    focusedGoalScopeKey,
    focusedSid,
    state.connState,
    state.newChat,
    state.wrapperOnline,
  ]);

  useEffect(() => {
    // Request/build ordering is scoped to one wrapper connection. Keep each
    // sid's bounded attempts across ordinary focus changes so a legacy receipt
    // outside the newest page cannot rescan a multi-megabyte transcript every
    // time the user returns to the session.
    terminalHistoryRepairRef.current.clear();
  }, [machineId, state.connState]);

  useEffect(() => {
    const completionId = focusedCompletion?.id ?? null;
    if (!focusedSid || state.newChat
        || historyView.browsing || state.connState !== "connected"
        || !state.wrapperOnline || !rt.syncReady
        || terminalRepairState !== "idle" || rt.acceptancePending) return;
    const completionMissing = completionNeedsHistoryRepair(
      historyView.turns, completionId);
    const idleTailMissing = idleTurnNeedsHistoryRepair(historyView.turns);
    if (!completionMissing && !idleTailMissing) {
      terminalHistoryRepairRef.current.delete(focusedSid);
      return;
    }
    const latest = historyView.turns[historyView.turns.length - 1];
    // A successful completion receipt is the strongest post-TurnEnd causal
    // key. Failed/interrupted turns have no unread receipt, so bind their
    // fallback to the exact idle generation/lifecycle and newest display row.
    const repairKey = completionMissing && completionId
      ? completionId
      : [
          "idle",
          wsRef.current?.generationFor(focusedSid) ?? "",
          rt.lastLifecycleSeq,
          latest?.clientMsgId ?? latest?.historyTurnId ?? latest?.id ?? "",
        ].join("\0");
    const attempt = nextTerminalHistoryRepairAttempt(
      terminalHistoryRepairRef.current.get(focusedSid),
      repairKey,
      MAX_TERMINAL_HISTORY_REPAIR_ATTEMPTS,
    );
    if (!attempt) return;
    const accepted = requestHistory(
      focusedSid,
      undefined,
      HISTORY_INITIAL_PAGE,
      wsRef.current?.generationFor(focusedSid),
      undefined,
      undefined,
      { supersedePending: true, causalKey: repairKey },
    );
    if (accepted) {
      terminalHistoryRepairRef.current.set(focusedSid, attempt);
    }
  }, [
    focusedCompletion?.id,
    focusedSid,
    historyView.browsing,
    historyView.turns,
    requestHistory,
    rt.acceptancePending,
    rt.lastLifecycleSeq,
    rt.syncReady,
    state.connState,
    state.newChat,
    state.wrapperOnline,
    terminalHistoryRepairEpoch,
    terminalRepairState,
  ]);

  useEffect(() => {
    const acknowledgeVisible = () => {
      if (document.hidden) return;
      const current = stateRef.current;
      const parentSid = current.newChat ? null : current.focusedSid;
      if (!parentSid) return;
      const btwSid = visibleBtwSidRef.current;
      const completion = newestCompletionProjection(
        current.runtimes[parentSid]?.completion,
        catalogCompletionProjection(current.sessions.find(
          (session) => session.session_id === parentSid)),
      );
      const completionId = completionAcknowledgementId(
        completionReceiptsRef.current[parentSid], completion);
      const mainAcknowledgementQueued = completionId !== null
        && wsRef.current?.sendAcknowledgeCompletionTo(
          parentSid, completionId) !== null;
      setCompletionReceipts((receipts) => {
        let next = acknowledgeCompletion(
          receipts, parentSid, { main: mainAcknowledgementQueued });
        if (btwSid) {
          next = acknowledgeCompletion(
            next, parentSid, { btwSid });
        }
        return next;
      });
    };
    acknowledgeVisible();
    document.addEventListener("visibilitychange", acknowledgeVisible);
    return () => document.removeEventListener(
      "visibilitychange", acknowledgeVisible);
  }, [
    visibleParentSid,
    visibleBtwSid,
    rt.completion?.id,
    rt.completion?.unread,
    focusedSession?.completion_id,
    focusedSession?.completion_revision,
    focusedSession?.completion_unread,
  ]);

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
    // DSH skills belong to a native session, including when another browser
    // owns the wrapper's global focus. A new-chat draft has no catalog yet.
    if (request.engine === "dsh" && !request.sid?.startsWith("dsh@")) {
      setCapabilitiesLoading(false);
      return false;
    }
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
  const loadMessageImage = useCallback((
    sid: string,
    path: string,
    stablePreviewId?: string,
  ): boolean => {
    const ws = wsRef.current;
    const current = stateRef.current;
    const focusedSid = current.newChat ? null : current.focusedSid;
    const activeBtwSid = focusedSid
      ? current.btwByParentSid[focusedSid]?.activeSid ?? null : null;
    if (!ws || (focusedSid !== sid && activeBtwSid !== sid)) return false;
    const cache = inlineImageAssetsRef.current;
    const assetKey = stablePreviewId ?? path;
    if (cache.has(sid, path, assetKey)) return true;
    const previewId = stablePreviewId ?? uuid();
    const requestId = uuid();
    if (!cache.begin({
      sid, path, assetKey, previewId, requestId,
    })) return false;
    if (!ws.sendGetPreviewAsset(path, previewId, requestId, sid)) {
      cache.cancel(requestId);
      return false;
    }
    bumpInlineImageRevision();
    return true;
  }, []);
  const loadFocusedMessageImage = useCallback((
    path: string,
    stablePreviewId?: string,
  ) => (
    focusedSid
      ? loadMessageImage(focusedSid, path, stablePreviewId)
      : false
  ), [focusedSid, loadMessageImage]);
  const loadBtwMessageImage = useCallback((
    path: string,
    stablePreviewId?: string,
  ) => (
    activeBtwSid
      ? loadMessageImage(activeBtwSid, path, stablePreviewId)
      : false
  ), [activeBtwSid, loadMessageImage]);
  const authorizeMessageImage = useCallback((
    authorization: PreviewAuthorizationState,
    decision: "allow" | "deny",
  ): boolean => {
    const cache = inlineImageAssetsRef.current;
    const request = cache.authorizationRequest(authorization);
    if (!request) return false;
    if (!wsRef.current?.sendAuthorizePreview(
      authorization.authorizationId,
      authorization.requestId,
      decision,
      request.sid,
    )) return false;
    if (!cache.markAuthorizationSubmitting(authorization)) return false;
    bumpInlineImageRevision();
    return true;
  }, []);
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
  useEffect(() => {
    try {
      sessionStorage.setItem(
        BTW_PANEL_SCOPES_KEY, JSON.stringify(btwPanelScopes));
    } catch { /* storage is best-effort in private browsing */ }
  }, [btwPanelScopes]);
  const toggleTheme = () => setTheme((t) => (t === "dark" ? "light" : "dark"));

  // `engine` selects the backend (Claude Code / Codex): the whole UI re-skins via
  // data-engine, and the sidebar re-lists that engine's own sessions.
  const engineRef = useRef(engine);
  const harnessPointerSelectionRef = useRef(false);
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
      engine: listed?.engine === "codex" || listed?.engine === "claude" || listed?.engine === "dsh"
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
    localStorage.setItem(LEGACY_SPACE_KEY, space);
    spacesByEngineRef.current = rememberEngineSpace(
      spacesByEngineRef.current, engine, space);
    localStorage.setItem(ENGINE_SPACES_KEY, JSON.stringify(spacesByEngineRef.current));
  }, [engine, space]);
  useEffect(() => {
    if (newChatCwd === null || state.connState !== "connected"
        || !state.wrapperOnline || newChatCodexProfileMissing
        || newChatClaudeProfileMissing) return;
    const request = newChatCatalogRequest(
      engine, space, newChatCwd,
      newChatCodexProfileId, newChatClaudeProfileId);
    if (!request) return;
    wsRef.current?.sendGetModels(
      request.engine, request.cwd,
      request.codexProfileId, request.claudeProfileId);
  }, [
    engine,
    newChatCwd,
    newChatClaudeProfileId,
    newChatClaudeProfileMissing,
    newChatCodexProfileId,
    newChatCodexProfileMissing,
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
      newChatCatalog,
      newChatDefaults.model,
    );
    if (reconciled.model === state.newChat.model
        && reconciled.effort === state.newChat.effort) return;
    dispatch({ type: "set_new_chat_selection", ...reconciled });
  }, [
    engine,
    newChatCatalog,
    newChatDefaults.model,
    state.newChat,
  ]);
  useEffect(() => {
    if (state.newChat || (focusedEngine !== "dsh" && !focusedAccountProfileId)
        || state.connState !== "connected" || !state.wrapperOnline) return;
    wsRef.current?.sendGetModels(
      focusedEngine,
      focusedEngine === "claude" ? capabilityCwd : undefined,
      focusedCodexProfileId,
      focusedClaudeProfileId);
  }, [
    capabilityCwd,
    focusedAccountProfileId,
    focusedClaudeProfileId,
    focusedCodexProfileId,
    focusedEngine,
    state.connState,
    state.newChat,
    state.wrapperOnline,
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
      lastFocusBySurfaceRef.current[
        sessionScopeKey(machineId, currentEngine, currentSpace)
      ] = focusedSid;
    }
  }, [focusedSid, machineId, state.newChat]);

  const prepareSurfaceSwitch = useCallback((
    nextEngine: Engine,
    nextSpace: Space,
    preserveAuthority = false,
  ) => {
    clearForkFocusLease(false);
    rememberSurfaceFocus(engine, space);
    const surfaceKey = `${nextSpace}:${nextEngine}`;
    const focusScopeKey = sessionScopeKey(
      machineId, nextEngine, nextSpace);
    if (!preserveAuthority) {
      authoritativeSurfaceListsRef.current.delete(surfaceKey);
    }
    const cachedSessions = sessionListsBySurfaceRef.current[surfaceKey] ?? [];
    dispatch({
      type: "restore_session_list",
      sessions: cachedSessions,
    });
    const remembered = lastFocusBySurfaceRef.current[focusScopeKey];
    const immediate = preserveAuthority
      ? null : selectSurfaceSession(cachedSessions, remembered);
    preferredSurfaceFocusRef.current = preserveAuthority
      ? null
      : remembered ? { key: focusScopeKey, sid: remembered } : null;
    didInitFocusRef.current = preserveAuthority;
    const ws = wsRef.current;
    ws?.setSurface(nextEngine, nextSpace);
    if (immediate) {
      // The cached list is scoped to this machine+engine+space. Paint its exact
      // remembered row synchronously; the accepted list still validates it and
      // clears/falls back if another client deleted it meanwhile.
      dispatch({ type: "exit_new_chat" });
      dispatch({ type: "focus_session", sid: immediate.session_id });
      ws?.setFocusedSid(immediate.session_id, nextEngine, nextSpace);
      requestHistory(
        immediate.session_id, undefined, HISTORY_INITIAL_PAGE);
      resumeListedSession(immediate, nextEngine, nextSpace, ws);
      if (nextSpace === "work") {
        ws?.sendGetWorkArtifacts(nextEngine, immediate.session_id);
      }
      setRestoringSurfaceScope(null);
    } else {
      // Do not expose a sendable home-directory draft while the target
      // catalog is still unknown. The accepted list either restores a session
      // or, only after confirming it is empty, creates the scoped default
      // draft in the initial-focus effect below.
      ws?.setFocusedSid(null);
      dispatch({ type: "exit_new_chat" });
      setRestoringSurfaceScope(focusScopeKey);
    }
    setNewChatAutoFocus(false);
  }, [
    clearForkFocusLease,
    engine,
    machineId,
    rememberSurfaceFocus,
    requestHistory,
    resumeListedSession,
    space,
  ]);

  const focusListedSession = useCallback((selected: SessionInfo) => {
    const selectedEngine: Engine = selected.engine === "codex"
      || selected.engine === "claude" || selected.engine === "dsh"
      ? selected.engine : engineRef.current;
    const selectedSpace: Space = selected.space === "work"
      || selected.space === "code"
      ? selected.space : spaceRef.current;
    const id = selected.session_id;
    const focusScopeKey = sessionScopeKey(
      machineId, selectedEngine, selectedSpace);
    // The click is newer than any surface-restore intent already in flight.
    // Claim the target synchronously because a SessionList can arrive before
    // React commits the focus_session dispatch below.
    didInitFocusRef.current = true;
    preferredSurfaceFocusRef.current = null;
    lastFocusBySurfaceRef.current[focusScopeKey] = id;
    if (forkFocusLeaseRef.current?.childSessionId !== id) {
      clearForkFocusLease(false);
    }
    pendingCreateRef.current = null;
    setCreateError(null);
    setStatusOpenSid(null);
    setUsageActivityOpen(false);
    setWorkArtifactsOpen(false);
    dispatch({ type: "exit_new_chat" });
    dispatch({ type: "focus_session", sid: id });
    setRestoringSurfaceScope(null);
    wsRef.current?.setFocusedSid(id, selectedEngine, selectedSpace);
    requestHistory(id, undefined, HISTORY_INITIAL_PAGE);
    resumeListedSession(selected, selectedEngine, selectedSpace);
    if (selectedSpace === "work") {
      wsRef.current?.sendGetWorkArtifacts(selectedEngine, id);
    }
    if (isMobile()) setSidebarOpen(false);
  }, [clearForkFocusLease, machineId, requestHistory, resumeListedSession]);

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
          ? {
              key: sessionScopeKey(
                origin.machineId, origin.engine, origin.space),
              sid: origin.sid,
            }
          : null;
        didInitFocusRef.current = false;
        setRestoringSurfaceScope(null);
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
  const toggleEngine = (nextEngine: Engine) => {
    cancelPendingNotificationTarget();
    const nextSpace = spacesByEngineRef.current[nextEngine];
    pendingCreateRef.current = null;
    setCreateError(null);
    setStatusOpenSid(null);
    setUsageActivityOpen(false);
    setWorkArtifactsOpen(false);
    setWorkProjectId(null);
    prepareSurfaceSwitch(nextEngine, nextSpace);
    engineRef.current = nextEngine;
    spaceRef.current = nextSpace;
    setEngine(nextEngine);
    setSpace(nextSpace);
    if (isMobile()) setSidebarOpen(false);
  };

  const switchSpace = (next: Space) => {
    if ((engine === "dsh" && next !== "code") || next === space || !confirmArtifactDiscard()) return;
    cancelPendingNotificationTarget();
    pendingCreateRef.current = null;
    setCreateError(null);
    setStatusOpenSid(null);
    setForkWorktreeSession(null);
    setForkWorktreeError(null);
    setWorkArtifactsOpen(false);
    prepareSurfaceSwitch(engine, next);
    spaceRef.current = next;
    setSpace(next);
  };

  // WebSocket lifecycle
  useEffect(() => {
    if (!authed) return;
    const historyRequests = historyRequestsRef.current;
    const turnFileRequests = turnFileRequestsRef.current;
    const contextRequestLaunches = contextRequestLaunchesRef.current;
    const contextDeferredRetryAttempts =
      contextDeferredRetryAttemptsRef.current;
    const lifecycleEpoch = ++wsLifecycleEpochRef.current;
    setPermissionProfileRequests((current) =>
      Object.keys(current).length ? {} : current);
    didInitFocusRef.current = false;  // re-arm initial-focus for this connection lifecycle
    authoritativeSurfaceListsRef.current.delete(`${spaceRef.current}:${engineRef.current}`);

    let cancelled = false;
    let effectWs: RelayWs | null = null;
    const acceptsLifecycle = () => !cancelled
      && wsLifecycleEpochRef.current === lifecycleEpoch;
    const deferredContextRetryTimers = new Set<string>();
    const scheduleDeferredClaudeContextRefresh = (
      sid: string,
      engineHint?: Engine,
      afterBusy = false,
    ) => {
      if (engineHint && engineHint !== "claude") return;
      let delayMs = 0;
      if (afterBusy) {
        const attempt = contextDeferredRetryAttempts.get(sid) ?? 0;
        const delays = [100, 300, 750] as const;
        if (attempt >= delays.length) return;
        contextDeferredRetryAttempts.set(sid, attempt + 1);
        delayMs = delays[attempt];
      } else {
        // A real idle boundary starts a fresh bounded catch-up window.
        contextDeferredRetryAttempts.delete(sid);
      }
      if (deferredContextRetryTimers.has(sid)) return;
      deferredContextRetryTimers.add(sid);
      window.setTimeout(() => {
        deferredContextRetryTimers.delete(sid);
        if (!acceptsLifecycle()) return;
        const current = stateRef.current;
        const runtime = current.runtimes[sid];
        if (!runtime?.contextRefreshDeferred
            || runtime.contextRequestId
            || runtime.state !== "idle") return;
        const session = current.sessions.find(
          (candidate) => candidate.session_id === sid);
        const eventEngine = engineHint
          ?? session?.engine
          ?? (current.focusedSid === sid ? engineRef.current : undefined);
        if (eventEngine !== "claude") return;
        sendContextRequestTo(sid, true, effectWs);
      }, delayMs);
    };
    goalRecoveryRequestsRef.current.clear();
    goalRequestScopeByIdRef.current.clear();
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
      const sid = e.sid ?? e.cc_session_id;
      if (sid && e.state === "idle") {
        // A replacement Wrapper normally restores a resident session with a
        // Snapshot rather than another direct State(idle). Preserve an explicit
        // refresh intent across that generation boundary instead of leaving the
        // popover waiting forever.
        scheduleDeferredClaudeContextRefresh(sid, ownership?.engine);
      }
    }

    (async () => {
      let seeded = { cursors: {} as Record<string, number>,
        generations: {} as Record<string, string>,
        controls: {} as Record<string, SessionControl> };
      try { seeded = await import("./cache").then((m) => m.loadAllReplayState()); } catch { /* best-effort */ }
      if (cancelled) return;
      const ws = new RelayWs({
        onEvent: (msg, ownership) => {
          if (!acceptsLifecycle()) return;
          if (turnFileRequestsRef.current.accept(msg)) return;
          if (dshApi.accept(msg)) return;
          const settlesContextRequest = !!(
            (msg.type === "context_report"
                || (msg.type === "error" && msg.code !== "wrapper_offline"))
              && msg.sid
              && msg.request_id
              && (
                contextRequestLaunchesRef.current.get(msg.sid)
                  === msg.request_id
                || stateRef.current.runtimes[msg.sid]?.contextRequestId
                  === msg.request_id
              )
          );
          if (settlesContextRequest && msg.sid) {
            contextRequestLaunchesRef.current.delete(msg.sid);
            if (msg.type !== "error" || msg.code !== "busy") {
              contextDeferredRetryAttempts.delete(msg.sid);
            }
          }
          if (msg.type === "history_invalidated") {
            const session = stateRef.current.sessions.find(
              (candidate) => candidate.session_id === msg.session_id);
            const scoped = ownership ?? {
              machineId,
              engine: session?.engine ?? engineRef.current,
              space: session?.space ?? spaceRef.current,
            };
            planProgressCacheRef.current.clear({
              machineId: scoped.machineId,
              engine: scoped.engine,
              space: scoped.space,
              sid: msg.session_id,
            });
          } else if (msg.type === "session_rekey") {
            const session = stateRef.current.sessions.find(
              (candidate) => candidate.session_id === msg.old_key
                || candidate.session_id === msg.session_id);
            const scoped = ownership ?? {
              machineId,
              engine: session?.engine ?? engineRef.current,
              space: session?.space ?? spaceRef.current,
            };
            planProgressCacheRef.current.rekey({
              machineId: scoped.machineId,
              engine: scoped.engine,
              space: scoped.space,
            }, msg.old_key, msg.session_id);
          }
          // SessionList is a scoped response, not a broadcast catalog. Without
          // the exact request ownership it may belong to an older surface or
          // WebSocket generation and must not mutate any sidebar/focus cache.
          if (msg.type === "session_list" && !ownership) return;
          if (msg.type === "goal_state" && msg.sid) {
            const completedGoalRequest = msg.request_id
              ? goalRequestScopeByIdRef.current.get(msg.request_id)
              : undefined;
            if (msg.request_id) {
              goalRequestScopeByIdRef.current.delete(msg.request_id);
            }
            if (completedGoalRequest) {
              goalRecoveryRequestsRef.current.delete(
                `${lifecycleEpoch}\0${completedGoalRequest.scopeKey}`);
            }
            const key = goalUiScopeForEvent(msg.sid, ownership);
            if (key) {
              const localGoalIdentity = goalStableIdentity(msg.goal);
              const localPreference = goalUiPreferencesRef.current[key];
              const legacyDismissed = !!localGoalIdentity
                && localPreference?.hiddenGoal === localGoalIdentity;
              const authoritativeDismissed = msg.dismissed === true;
              const goalSessionArchived = stateRef.current.sessions.find(
                (session) => session.session_id === msg.sid,
              )?.tag === "archived";
              const migrationKey = msg.goal_id
                ? `${machineId}\0${msg.sid}\0${msg.goal_id}` : null;
              if (migrationKey && authoritativeDismissed) {
                goalDismissMigrationsRef.current.delete(migrationKey);
                for (const [requestId, pendingKey] of
                  goalDismissMigrationByRequestRef.current) {
                  if (pendingKey === migrationKey) {
                    goalDismissMigrationByRequestRef.current.delete(requestId);
                  }
                }
              } else if (migrationKey && legacyDismissed
                  && !goalSessionArchived
                  && !goalDismissMigrationsRef.current.has(migrationKey)) {
                const requestId = ws.sendDismissGoalTo(
                  msg.sid, msg.goal_id!);
                if (requestId) {
                  goalDismissMigrationsRef.current.add(migrationKey);
                  goalDismissMigrationByRequestRef.current.set(
                    requestId, migrationKey);
                }
              }
              const reconciled = reconcileGoalUiPreference(
                goalUiPreferencesRef.current,
                key,
                msg.goal,
                Date.now(),
                authoritativeDismissed,
              );
              persistGoalUiPreferences(reconciled.preferences);
              setGoalUiByScope((current) => {
                const previous = current[key] ?? {
                  revealed: false,
                  open: false,
                  loading: false,
                };
                return {
                  ...current,
                  [key]: {
                    revealed: reconciled.revealed,
                    open: previous.open,
                    loading: false,
                  },
                };
              });
              for (const [requestId, requestScope] of
                goalRequestScopeByIdRef.current) {
                if (requestScope.scopeKey === key) {
                  goalRequestScopeByIdRef.current.delete(requestId);
                }
              }
              recoverableReads.complete(["goal", key].join("\u0000"));
            }
          } else if (msg.type === "completion_state" && msg.sid
              && msg.unread !== true) {
            // Clear the page-local fallback as soon as the wrapper confirms an
            // acknowledgement for the same completion from any browser. A
            // delayed read receipt for an older completion cannot clear a
            // newer turn_end fallback.
            const authoritative = newestCompletionProjection(
              stateRef.current.runtimes[msg.sid]?.completion,
              {
                id: msg.completion_id ?? null,
                unread: msg.unread ?? false,
                revision: msg.revision ?? 0,
              },
            );
            setCompletionReceipts((receipts) => (
              acknowledgeMatchingCompletion(
                receipts, msg.sid!, authoritative,
                {
                  authoritativeSeq: msg.seq,
                  authoritativeGeneration: ws.generationFor(msg.sid!),
                })));
          } else if (msg.type === "session_list") {
            // A sleeping browser can miss the live acknowledgement and later
            // reconnect after the session was evicted. Its catalog row carries
            // the same durable receipt, so clear any older page-local fallback
            // instead of letting it reappear if the runtime is pruned.
            setCompletionReceipts((receipts) => {
              let next = receipts;
              for (const session of msg.sessions) {
                const catalog = catalogCompletionProjection(session);
                if (!catalog) continue;
                const authoritative = newestCompletionProjection(
                  stateRef.current.runtimes[session.session_id]?.completion,
                  catalog,
                );
                next = acknowledgeMatchingCompletion(
                  next, session.session_id, authoritative);
              }
              return next;
            });
          } else if (msg.type === "error" && msg.request_id) {
            const coordinator = skillCatalogRequestsRef.current;
            const failedSkills = msg.code === "wrapper_offline"
              ? null : coordinator?.fail(msg.request_id);
            if (failedSkills && failedSkills.key === focusedSkillScopeRef.current?.key
                && !coordinator?.hasPendingRead(failedSkills.key, false)
                && !coordinator?.hasPendingMutation(failedSkills.key)) {
              setCapabilitiesLoading(false);
            }
            const failedMigration =
              goalDismissMigrationByRequestRef.current.get(msg.request_id);
            if (failedMigration && msg.code !== "wrapper_offline") {
              goalDismissMigrationByRequestRef.current.delete(msg.request_id);
              goalDismissMigrationsRef.current.delete(failedMigration);
            }
            const request = goalRequestScopeByIdRef.current.get(msg.request_id);
            if (request) {
              goalRequestScopeByIdRef.current.delete(msg.request_id);
              const { scopeKey: key, sid } = request;
              goalRecoveryRequestsRef.current.delete(
                `${lifecycleEpoch}\0${key}`);
              setGoalUiByScope((current) => {
                const previous = current[key];
                if (!previous) return {
                  ...current,
                  [key]: {
                    revealed: !!goalUiPreferencesRef.current[key]?.known,
                    open: false,
                    loading: false,
                  },
                };
                return {
                  ...current,
                  [key]: {
                    ...previous,
                    loading: false,
                  },
                };
              });
              recoverableReads.retry(["goal", key].join("\u0000"), () => {
                const current = stateRef.current;
                if (cancelled || current.focusedSid !== sid
                    || current.sessions.find(
                      (session) => session.session_id === sid,
                    )?.tag === "archived") return;
                const requestId = ws.sendGetGoalTo(sid);
                if (!requestId) return;
                goalRequestScopeByIdRef.current.set(
                  requestId, { scopeKey: key, sid });
                setGoalUiByScope((current) => ({
                  ...current,
                  [key]: {
                    ...(current[key] ?? {
                      revealed: true, open: false, loading: false,
                    }),
                    loading: true,
                  },
                }));
              });
              // This is a one-shot read repair, not a failed model/control
              // operation. The Goal chip owns its retry state; feeding the same
              // correlated Error into the reducer would also create a global
              // "操作未完成" banner on every transient refresh failure.
              return;
            }
          }
          if (msg.type === "session_rekey"
              && inlineImageAssetsRef.current.rekeySession(
                msg.old_key,
                msg.session_id,
              )) {
            bumpInlineImageRevision();
          }
          if (msg.type === "preview_authorization_required"
              && inlineImageAssetsRef.current.requireAuthorization(msg)) {
            bumpInlineImageRevision();
          }
          if (msg.type === "preview_authorization_result") {
            const retry =
              inlineImageAssetsRef.current.acceptAuthorizationResult(msg);
            if (retry !== false) {
              bumpInlineImageRevision();
              if (retry && !ws.sendGetPreviewAsset(
                retry.path,
                retry.previewId,
                retry.requestId,
                retry.sid,
              )) {
                inlineImageAssetsRef.current.cancel(retry.requestId);
                bumpInlineImageRevision();
              }
            }
          }
          if (msg.type === "preview_asset"
              && inlineImageAssetsRef.current.accept(msg)) {
            bumpInlineImageRevision();
          }
          if (msg.type === "history_image"
              && historyImageAssetsRef.current.accept(msg)) {
            bumpHistoryImageRevision();
            if (msg.error && msg.session_id === stateRef.current.focusedSid
                && msg.revision !== stateRef.current.runtimes[msg.session_id]?.historyRevision) {
              requestHistory(msg.session_id, undefined, HISTORY_INITIAL_PAGE);
            }
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
              ([, group]) => group.chats.some((chat) => chat.sid === msg.sid),
            );
            const isBtw = !!btwOwner;
            // A closed/stale fork can still drain one final terminal frame.
            // Without an owner it must not appear as a fake top-level session.
            if (!msg.sid.startsWith("btw-") || isBtw) {
              const parentSid = btwOwner?.[0] ?? msg.sid;
              const sameVisibleParent = !document.hidden
                && !current.newChat
                && current.focusedSid === parentSid;
              const isBtwPanelVisible = isBtw
                && sameVisibleParent
                && visibleBtwSidRef.current === msg.sid;
              const alreadySeen = isBtw
                ? isBtwPanelVisible : sameVisibleParent;
              if (!alreadySeen) {
                setCompletionReceipts((receipts) => markCompletionUnread(
                  receipts,
                  parentSid,
                  msg.sid!,
                  isBtw ? "btw" : "main",
                  isBtw ? null
                    : msg.checkpoint_id ?? msg.turn_id ?? null,
                  isBtw ? null : msg.seq ?? null,
                  isBtw ? null : ws.generationFor(msg.sid!),
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
              && !msg.sid.startsWith("btw-")
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
          let settledHistoryCausalKey: string | undefined;
          if (msg.type === "history") {
            const completedHistory =
              historyRequestsRef.current.complete(msg);
            settledHistoryCausalKey = completedHistory.settledCausalKey;
            const browseWaiters = completedHistory.matched;
            for (const browse of completedHistory.stale) {
              dispatch({
                type: "history_browse_page_failed",
                sid: msg.session_id,
                scopeKey: browse.scopeKey,
                revision: stateRef.current.historyBrowse?.revision
                  ?? msg.revision,
                generation: stateRef.current.historyBrowse?.generation,
                viewId: browse.viewId,
                windowEpoch: browse.windowEpoch,
                before: browse.pendingBefore,
              });
            }
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
              if (completedHistory.stale.some((waiter) => {
                const browse = stateRef.current.historyBrowse;
                return browse?.sid === msg.session_id
                  && browse.scopeKey === waiter.scopeKey
                  && browse.viewId === waiter.viewId
                  && browse.windowEpoch === waiter.windowEpoch;
              })
                  && stateRef.current.focusedSid === msg.session_id) {
                // The cursor came from an obsolete revision/generation. Exit
                // that read-only browse lifetime and refresh the canonical head
                // rather than leaving mobile paging in a permanent spinner.
                dispatch({ type: "return_to_latest", sid: msg.session_id });
                const currentRuntime =
                  stateRef.current.runtimes[msg.session_id];
                const currentGeneration = ws.generationFor(msg.session_id)
                  ?? currentRuntime?.pendingHistoryGeneration
                  ?? currentRuntime?.historyGeneration;
                const currentRevision = currentRuntime?.pendingHistoryRevision
                  ?? (currentRuntime?.historyInvalidated
                    ? undefined : currentRuntime?.historyRevision);
                requestHistory(
                  msg.session_id, undefined, HISTORY_INITIAL_PAGE,
                  currentGeneration, currentRevision);
              }
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
            const detailTargets =
              historyDetailRequestsRef.current.completeAll(msg);
            if (detailTargets.length === 0) return;
            const retryKey = [
              "detail", msg.session_id, msg.revision, msg.turn_id,
              msg.before ?? "",
            ].join("\u0000");
            const activeDetailTargets = (
              targets: readonly HistoryDetailRequestContext[],
              retrying = false,
            ) => {
              const current = stateRef.current;
              return targets.filter((detailTarget) => {
                if (detailTarget.target === "browse") {
                  const browse = current.historyBrowse;
                  return !!browse
                    && current.focusedSid === detailTarget.sid
                    && browse.sid === detailTarget.sid
                    && browse.scopeKey === detailTarget.scopeKey
                    && browse.viewId === detailTarget.viewId
                    && browse.revision === detailTarget.revision
                    && browse.turns.some((turn) =>
                      canonicalTurnId(turn) === detailTarget.turnId
                      || turn.id === detailTarget.turnId);
                }
                const runtime = current.runtimes[detailTarget.sid];
                const turn = runtime?.turns.find(
                  (item) => canonicalTurnId(item) === detailTarget.turnId
                    || item.id === detailTarget.turnId);
                return current.focusedSid === detailTarget.sid
                  && !!turn
                  && (!retrying
                    || !!detailTarget.before || !turn.detailLoaded)
                  && runtime?.historyRevision === detailTarget.revision;
              });
            };
            const releaseDetailFailure = (
              targets: readonly HistoryDetailRequestContext[],
            ) => {
              let runtimeReleased = false;
              for (const detailTarget of targets) {
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
                    error: msg.error,
                    resetRequired: msg.reset_required,
                    before: detailTarget.before,
                  });
                } else if (!runtimeReleased) {
                  dispatch({ type: "event", event: msg, ownership });
                  runtimeReleased = true;
                }
              }
            };
            if (msg.authoritative === false) {
              if (msg.reset_required) {
                recoverableReads.complete(retryKey);
                const resetKey = [
                  msg.session_id, msg.revision, msg.turn_id,
                ].join("\u0000");
                if (historyDetailResetAttemptsRef.current.has(resetKey)) {
                  releaseDetailFailure(detailTargets);
                  return;
                }
                const cancelledTargets =
                  historyDetailRequestsRef.current.cancelTurn({
                    sid: msg.session_id,
                    revision: msg.revision,
                    turnId: msg.turn_id,
                  });
                const activeTargets = activeDetailTargets([
                  ...detailTargets,
                  ...cancelledTargets,
                ]);
                if (activeTargets.length === 0) return;
                const resetTargets: HistoryDetailRequestContext[] =
                  activeTargets.map((target) => ({
                    ...target,
                    before: null,
                  }));
                const uniqueTargets = new Map<string,
                  HistoryDetailRequestContext>();
                for (const target of resetTargets) {
                  const key = target.target === "browse"
                    ? [target.target, target.scopeKey, target.viewId,
                        target.windowEpoch].join("\u0000")
                    : [target.target, target.scopeKey,
                        target.autoLoad ? 1 : 0].join("\u0000");
                  uniqueTargets.set(key, target);
                }
                const registrations = [...uniqueTargets.values()].map(
                  (detailTarget) => ({
                    detailTarget,
                    registration:
                      historyDetailRequestsRef.current.register(detailTarget),
                  }),
                ).filter(({ registration }) => registration.accepted);
                if (registrations.length === 0) {
                  releaseDetailFailure(detailTargets);
                  return;
                }
                historyDetailResetAttemptsRef.current.add(resetKey);
                if (historyDetailResetAttemptsRef.current.size > 256) {
                  const oldest = historyDetailResetAttemptsRef.current
                    .values().next().value;
                  if (oldest) {
                    historyDetailResetAttemptsRef.current.delete(oldest);
                  }
                }
                if (registrations.some(
                    ({ registration }) => registration.send)) {
                  const target = registrations[0].detailTarget;
                  const sent = ws.sendGetTurnDetail(
                    msg.session_id, msg.turn_id, target.revision, null);
                  if (!sent) {
                    historyDetailResetAttemptsRef.current.delete(resetKey);
                    for (const { detailTarget } of registrations) {
                      historyDetailRequestsRef.current.cancel(detailTarget);
                      dispatch({
                        type: "history_detail_cancelled",
                        context: detailTarget,
                      });
                    }
                    return;
                  }
                }
                for (const { detailTarget } of registrations) {
                  dispatch({
                    type: "history_detail_reset_requested",
                    context: detailTarget,
                  });
                }
                return;
              }
              releaseDetailFailure(detailTargets);
              recoverableReads.retry(retryKey, () => {
                if (cancelled) return;
                if (stateRef.current.focusedSid !== msg.session_id) return;
                const activeTargets = activeDetailTargets(
                  detailTargets, true);
                if (activeTargets.length === 0) return;
                const registrations = activeTargets.map((detailTarget) => ({
                  detailTarget,
                  registration:
                    historyDetailRequestsRef.current.register(detailTarget),
                })).filter(({ registration }) => registration.accepted);
                if (registrations.length === 0) return;
                if (registrations.some(({ registration }) => registration.send)) {
                  const target = registrations[0].detailTarget;
                  const sent = ws.sendGetTurnDetail(
                    msg.session_id, msg.turn_id, target.revision, target.before);
                  if (!sent) {
                    for (const { detailTarget } of registrations) {
                      historyDetailRequestsRef.current.cancel(detailTarget);
                    }
                    return;
                  }
                }
                for (const { detailTarget } of registrations) {
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
                }
              });
              return;
            } else {
              recoverableReads.complete(retryKey);
            }
            let runtimeTarget = false;
            for (const detailTarget of detailTargets) {
              if (detailTarget.target === "runtime") {
                runtimeTarget = true;
                continue;
              }
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
            }
            if (!runtimeTarget) return;
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
          if (msg.type === "btw_sync"
              && msg.revision >= stateRef.current.btwRevision) {
            const retained = new Set(msg.sessions.map((chat) => chat.btw_sid));
            const removed = Object.entries(stateRef.current.btwByParentSid)
              .flatMap(([parentSid, group]) => group.chats
                .filter((chat) => !retained.has(chat.sid))
                .map((chat) => [parentSid, chat] as const));
            for (const [, chat] of removed) {
              for (const draftSpace of ["code", "work"] as const) {
                btwDraftsRef.current.delete(composerDraftKey(
                  machineId, draftSpace, chat.engine, `btw:${chat.sid}`,
                ));
              }
            }
            if (removed.length > 0) {
              setBtwSendModeBySid((current) => {
                const next = { ...current };
                for (const [, chat] of removed) delete next[chat.sid];
                return next;
              });
              setCompletionReceipts((current) => removed.reduce(
                (next, [parentSid, chat]) => acknowledgeCompletion(
                  next, parentSid, { btwSid: chat.sid }),
                current,
              ));
            }
          }
          if (msg.type === "btw_opened") {
            const knownChat = Object.values(
              stateRef.current.btwByParentSid).some((group) =>
              group.chats.some((chat) => chat.sid === msg.btw_sid));
            const requestedParent = btwRequestParentsRef.current.get(
              msg.request_id) ?? null;
            const pendingRequestId = requestedParent
              ? pendingBtwByParentRef.current.get(requestedParent) ?? null
              : null;
            if (msg.revision < stateRef.current.btwRevision) {
              // A newer authoritative sync/close already decided this fork's
              // existence. Settle this exact pending command as well: after a
              // reload the catalog can restore the fork before its cached open
              // response arrives, and leaving the spinner behind would disable
              // creation of every later sibling side chat.
              if (requestedParent && matchesBtwRequest(
                pendingRequestId, msg.request_id)) {
                pendingBtwByParentRef.current.delete(requestedParent);
                setBtwOpeningFor(requestedParent, false);
              }
              btwRequestParentsRef.current.delete(msg.request_id);
              return;
            }
            if (knownChat && !matchesBtwRequest(
              pendingRequestId, msg.request_id)) {
              // Reliable response replay after BtwSync. The catalog already
              // owns it, and re-applying the open would steal tab selection.
              btwRequestParentsRef.current.delete(msg.request_id);
              return;
            }
            const activeRequest = activeBtwByParentRef.current.get(
              msg.parent_sid)
              ?? (requestedParent
                ? activeBtwByParentRef.current.get(requestedParent) : undefined)
              ?? null;
            if (activeRequest?.requestId === msg.request_id
                && activeRequest.sid === msg.btw_sid) {
              btwRequestParentsRef.current.delete(msg.request_id);
              return; // cached replay after a lost ACK; the fork is already open
            }
            if (requestedParent && matchesBtwRequest(
              pendingRequestId, msg.request_id)) {
              pendingBtwByParentRef.current.delete(requestedParent);
              activeBtwByParentRef.current.delete(requestedParent);
              setBtwOpeningFor(requestedParent, false);
            }
            btwRequestParentsRef.current.delete(msg.request_id);
            activeBtwByParentRef.current.set(msg.parent_sid, {
              requestId: msg.request_id,
              sid: msg.btw_sid,
            });
          } else if (msg.type === "btw_closed") {
            const group = stateRef.current.btwByParentSid[msg.parent_sid];
            const chat = group?.chats.find(
              (candidate) => candidate.sid === msg.btw_sid);
            const tracked = activeBtwByParentRef.current.get(msg.parent_sid);
            if (tracked?.sid === msg.btw_sid) {
              activeBtwByParentRef.current.delete(msg.parent_sid);
            }
            if (chat) {
              for (const draftSpace of ["code", "work"] as const) {
                btwDraftsRef.current.delete(composerDraftKey(
                  machineId, draftSpace, chat.engine, `btw:${msg.btw_sid}`,
                ));
              }
            }
            setBtwSendModeBySid((current) => {
              if (!(msg.btw_sid in current)) return current;
              const next = { ...current };
              delete next[msg.btw_sid];
              return next;
            });
            setCompletionReceipts((current) => acknowledgeCompletion(
              current, msg.parent_sid, { btwSid: msg.btw_sid }));
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
            const parentSession = stateRef.current.sessions.find(
              (session) => session.session_id === msg.parent_session_id,
            );
            const parentCodexProfileId = targetEngine === "codex"
              ? parentSession?.codex_profile_id
                ?? codexProfileIdForSession(
                  msg.parent_session_id,
                  stateRef.current.defaultCodexProfileId,
                )
              : undefined;
            const parentClaudeProfileId = targetEngine === "claude"
              ? parentSession?.claude_profile_id
                ?? claudeProfileIdForSession(
                  msg.parent_session_id,
                  stateRef.current.defaultClaudeProfileId,
                )
              : undefined;
            startForkFocusLease({
              requestId: msg.request_id,
              parentSessionId: msg.parent_session_id,
              childSessionId: msg.session_id,
              engine: targetEngine,
              space: "code",
              machineId,
              cwd: msg.cwd,
              gitBranch: msg.git_branch,
              claudeProfileId: parentClaudeProfileId,
              codexProfileId: parentCodexProfileId,
              refreshAt: Date.now() + FORK_FOCUS_REFRESH_MS,
            });
            ws.setSessionEngines([{
              session_id: msg.session_id,
              engine: targetEngine,
              space: "code",
              claude_profile_id: parentClaudeProfileId,
              codex_profile_id: parentCodexProfileId,
            }]);
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
            handleSnapshot(msg, ownership);
            return;
          }
          if (msg.type === "permission_profiles" && !msg.sid && msg.cwd) {
            const current = stateRef.current;
            const expectedProfileId = current.newChat?.codexProfileId
              ?? current.codexProfileByScope[
                sessionScopeKey(machineId, "codex", "code")]
              ?? current.defaultCodexProfileId;
            const responseProfileId = msg.codex_profile_id
              ?? current.defaultCodexProfileId;
            if (!current.newChat || current.newChat.cwd !== msg.cwd
                || responseProfileId !== expectedProfileId) return;
            setNewChatPermissionCatalog({
              machineId,
              cwd: msg.cwd,
              codexProfileId: responseProfileId,
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
              }
              // This ref is only a response-replay hint. The reducer's BTW
              // catalog owns every resident child, so a rekey collision must
              // not discard either side chat.
            }
            if (ownership) {
              setViewerSelections((current) => {
                const old = viewerScopeKey({ ...ownership, sid: msg.old_key });
                if (!Object.hasOwn(current, old)) return current;
                const key = viewerScopeKey({ ...ownership, sid: msg.session_id });
                const next = { ...current, [key]: current[key] ?? current[old] };
                delete next[old];
                return next;
              });
              setBtwPanelScopes((current) => rekeyBtwPanelScope(
                current,
                btwPanelScopeKey(ownership.machineId, ownership.space,
                  ownership.engine, msg.old_key),
                btwPanelScopeKey(ownership.machineId, ownership.space,
                  ownership.engine, msg.session_id),
              ));
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
            if (ownership) {
              const oldGoalKey = goalUiScopeKey(
                ownership.machineId, ownership.space, ownership.engine,
                msg.old_key);
              const nextGoalKey = goalUiScopeKey(
                ownership.machineId, ownership.space, ownership.engine,
                msg.session_id);
              setGoalUiByScope((current) => {
                const prior = current[oldGoalKey];
                if (!prior) return current;
                const next = { ...current, [nextGoalKey]: prior };
                delete next[oldGoalKey];
                return next;
              });
              persistGoalUiPreferences(rekeyGoalUiPreference(
                goalUiPreferencesRef.current,
                oldGoalKey,
                nextGoalKey,
              ));
            }
          }
          // RelayWs only assigns ownership to a correlated, current-generation
          // catalog response. Reject an old/unknown list before it can rewrite
          // session/profile maps or warm a sibling surface.
          if (msg.type === "session_list" && !ownership) return;
          let normalizedListedSessions: SessionInfo[] | null = null;
          if (msg.type === "session_list") {
            const listedSpace = msg.space ?? "code";
            const surfaceKey = `${listedSpace}:${msg.engine}`;
            const normalized = normalizeSessionList(
              sessionListsBySurfaceRef.current[surfaceKey] ?? [],
              stateRef.current.codexProfiles,
              stateRef.current.defaultCodexProfileId,
              msg,
            );
            const lease = forkFocusLeaseSession(
              forkFocusLeaseRef.current,
              normalized.sessions,
              machineId,
              msg.engine,
              listedSpace,
            );
            if (!lease && forkFocusLeaseRef.current
                && forkFocusLeaseRef.current.machineId === machineId
                && forkFocusLeaseRef.current.engine === msg.engine
                && forkFocusLeaseRef.current.space === listedSpace) {
              const materialized = normalized.sessions.some(
                (session) => session.session_id
                  === forkFocusLeaseRef.current?.childSessionId,
              );
              clearForkFocusLease(false, !materialized);
            }
            normalizedListedSessions = lease
              ? [lease, ...normalized.sessions] : normalized.sessions;
            historySessionListsRef.current[
              surfaceKey
            ] = normalizedListedSessions;
            ws.setSessionEngines(normalizedListedSessions);
            sessionListsBySurfaceRef.current[surfaceKey] = normalizedListedSessions;
            authoritativeSurfaceListsRef.current.add(surfaceKey);
            bumpNotificationListRevision();
            prefetchedSurfacesRef.current.add(surfaceKey);
            // Warm the sibling Work/Code surface once per page lifetime. Codex
            // reuses the just-read native catalog in the wrapper, so this does
            // not start a second app-server and the user's first toggle is fast.
            // DSH is Code-only: its runtime rejects even a read of Work.
            const siblingSpace: Space = listedSpace === "work" ? "code" : "work";
            const siblingKey = `${siblingSpace}:${msg.engine}`;
            if (msg.engine !== "dsh" && !prefetchedSurfacesRef.current.has(siblingKey)) {
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
            setWorkDashboardMachineId(machineId);
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
            const listedSpace = msg.space ?? "code";
            const listedScopeKey = sessionScopeKey(
              machineId, msg.engine, listedSpace);
            const scopedCurrentSid = scopedFocusForSessionList(
              currentSid,
              stateRef.current.sessions,
              lastFocusBySurfaceRef.current[listedScopeKey],
              msg.engine,
              listedSpace,
            );
            if (scopedCurrentSid && !scopedCurrentSid.startsWith("tmp-")
                && !normalizedListedSessions?.some(
                  (session) => session.session_id === scopedCurrentSid)) {
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
          if (msg.type === "files_listed") {
            filesListenerRef.current?.(msg);
            return;
          }
          if (msg.type === "file_preview" && msg.directory && !msg.error) {
            const current = stateRef.current;
            const artifact = current.artifact;
            if (artifact && msg.sid && artifact.sid === msg.sid && artifact.requestId === msg.request_id) {
              dispatch({ type: "clear_artifact" });
              setFileBrowser({ sid: msg.sid, machineId, engine: engineRef.current, space: spaceRef.current,
                path: msg.path, preview: false, id: uuid() });
            }
            return;
          }
          if (msg.type === "agent_detail") {
            agentDetailListenerRef.current?.(msg);
            // Requester-scoped details never belong in the conversation
            // reducer or another browser's side panel.
            return;
          }
          if (msg.type === "session_list" && normalizedListedSessions) {
            dispatch({ type: "event", event: {
              ...msg, sessions: normalizedListedSessions,
            }, ownership });
          } else {
            dispatch({ type: "event", event: msg, ownership });
          }
          if (msg.type === "history" && !msg.before
              && msg.authoritative !== false
              && settledHistoryCausalKey) {
            settleTerminalHistoryRepair(
              msg.session_id, settledHistoryCausalKey);
          }
          if (settlesContextRequest && msg.type === "error"
              && msg.code === "busy" && msg.sid) {
            // The reducer first converts this exact request into a deferred
            // intent. If the UI already saw idle before the runner finished its
            // finalizer, there will be no second idle frame to wake it; retry a
            // few times with backoff instead of polling indefinitely.
            scheduleDeferredClaudeContextRefresh(
              msg.sid, ownership?.engine, true);
          }
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
            if (eventEngine === "claude") {
              // TurnEnd is emitted before the wrapper releases its query lock.
              // Retry only after the following authoritative idle frame has
              // reached the reducer; otherwise the refresh can race that
              // release, receive busy, and wait forever for another TurnEnd.
              scheduleDeferredClaudeContextRefresh(msg.sid, "claude");
            }
          }
          if (msg.type === "wrapper_reconnected") {
            skillCatalogsRef.current = {};
            setSkillCatalogs({});
            skillCatalogRequestsRef.current?.resetReads();
            const focusedSkills = focusedSkillScopeRef.current;
            if (focusedSkills?.cwd) {
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
          // Refresh from wrapper-owned live/transcript usage. Automatic reads
          // never issue Claude's optional native control request or use model tokens.
          if (msg.type === "turn_end" && msg.sid) {
            const runtime = stateRef.current.runtimes[msg.sid];
            if (!runtime?.contextRequestId
                && !runtime?.contextRefreshDeferred) {
              sendContextRequestTo(msg.sid, false, ws);
            }
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
          if (!acceptsLifecycle()) return;
          dispatch({ type: "conn", connState: s, detail });
          if (s !== "connected") {
            turnFileRequestsRef.current.clear();
            dshApi.reset();
            skillCatalogRequestsRef.current?.resetReads();
            // The fork result is authoritative only for this live connection.
            // A reconnect will obtain a fresh native SessionList, so do not
            // preserve a synthetic child indefinitely across disconnects.
            clearForkFocusLease(false);
          }
          if (s === "connected") {
            goalRecoveryRequestsRef.current.clear();
            goalRequestScopeByIdRef.current.clear();
            // Recovery preamble replay happens before this callback. Forget
            // both halves of the old-socket correlation together, then let
            // the focused Goal's fresh GetGoal response deliberately enqueue
            // a new idempotent migration. Keeping only the in-flight key would
            // suppress that retry forever when the replay returns an Error.
            resetGoalDismissMigrationTracking(
              goalDismissMigrationsRef.current,
              goalDismissMigrationByRequestRef.current,
            );
            recoverableReads.clear();
            settleCancelledHistoryBrowse(
              historyRequestsRef.current.beginConnection());
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
          if (!acceptsLifecycle()) return;
          turnFileRequestsRef.current.clear();
          setAuthReady(false);
          clearLegacyAuthMarkers(localStorage);
          pendingCreateRef.current = null;
          createRequestsRef.current.clear();
          pendingBtwByParentRef.current.clear();
          pendingSessionForkRef.current = null;
          pendingWorktreeForkRef.current = null;
          pendingSessionMigrationRef.current = null;
          clearForkFocusLease(false);
          setMigrateSession(null);
          setMigrateCreating(false);
          setMigrateError(null);
          activeBtwByParentRef.current.clear();
          btwRequestParentsRef.current.clear();
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
          setGoalUiByScope({});
          setStatusOpenSid(null);
          setUsageActivityOpen(false);
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
        onCommandError: (detail) => {
          if (!acceptsLifecycle()) return;
          dispatch({ type: "command_error", detail });
        },
        onOutboxChanged: (protectedSids) => {
          if (!acceptsLifecycle()) return;
          dispatch({ type: "prune_runtimes", protectedSids });
        },
        onCommandCompleted: (commandId) => {
          if (!acceptsLifecycle()) return;
          setPermissionProfileRequests((current) =>
            Object.values(current).includes(commandId)
              ? Object.fromEntries(Object.entries(current)
                .filter(([, id]) => id !== commandId))
              : current);
        },
        onWrapperGenerationChanged: () => {
          if (!acceptsLifecycle()) return;
          turnFileRequestsRef.current.clear();
          setAgentPanel(null);
          agentDetailListenerRef.current = null;
          clearHistoryDetailRequests();
          inlineImageAssetsRef.current.clear();
          historyImageAssetsRef.current.clear();
          bumpInlineImageRevision();
          bumpHistoryImageRevision();
          setCompletionReceipts(discardBtwCompletionReceipts);
          setBtwSendModeBySid({});
          btwDraftsRef.current.clear();
          if (Object.keys(stateRef.current.btwByParentSid).length > 0
              || stateRef.current.btwRevision > 0
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
      effectWs = ws;
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
      if (wsLifecycleEpochRef.current === lifecycleEpoch) {
        wsLifecycleEpochRef.current += 1;
      }
      effectWs?.stop();
      if (wsRef.current === effectWs) wsRef.current = null;
      historyRequests.clear();
      clearHistoryDetailRequests();
      turnFileRequests.clear();
      recoverableReads.clear();
      contextRequestLaunches.clear();
      contextDeferredRetryAttempts.clear();
    };
  }, [
    acceptSkillCatalog,
    dshApi,
    authed,
    clearForkFocusLease,
    clearHistoryDetailRequests,
    installBrowseHistoryPage,
    invalidateHistoryPageScopes,
    machineId,
    persistGoalUiPreferences,
    requestHistory,
    requestSkillCatalog,
    sendContextRequestTo,
    setBtwOpeningFor,
    settleCancelledHistoryBrowse,
    settleTerminalHistoryRepair,
    startForkFocusLease,
  ]);

  // Land on the preferred/recent session only after an accepted list for the
  // active engine+space arrives. Background snapshots never pick focus.
  useEffect(() => {
    if (pendingNotificationTarget) return;
    if (didInitFocusRef.current || !wsRef.current) return;
    const surfaceKey = `${spaceRef.current}:${engineRef.current}`;
    const focusScopeKey = sessionScopeKey(
      machineId, engineRef.current, spaceRef.current);
    if (!authoritativeSurfaceListsRef.current.has(surfaceKey)) return;
    // A directory chosen in New Chat is an explicit user destination. A
    // reconnect/list refresh may validate the catalog, but it must not navigate
    // away from that draft. Cold surface restoration clears the old surface's
    // draft first, so it is deliberately excluded from this guard.
    if (state.newChat?.cwdSource === "explicit"
        && restoringSurfaceScope !== focusScopeKey) {
      preferredSurfaceFocusRef.current = null;
      didInitFocusRef.current = true;
      return;
    }
    if (state.sessions.length === 0) {
      preferredSurfaceFocusRef.current = null;
      didInitFocusRef.current = true;
      const current = stateRef.current;
      if (!current.newChat) {
        const inheritedCwd = current.cwdByScope[focusScopeKey];
        dispatch({
          type: "enter_new_chat",
          cwd: inheritedCwd || "~",
          cwdSource: inheritedCwd ? "inherited" : "default",
          claudeProfileId: engineRef.current === "claude"
            ? current.claudeProfileByScope[focusScopeKey]
              ?? current.defaultClaudeProfileId
            : null,
          codexProfileId: engineRef.current === "codex"
            ? current.codexProfileByScope[focusScopeKey]
              ?? current.defaultCodexProfileId
            : null,
        });
      }
      setRestoringSurfaceScope((scope) => (
        scope === focusScopeKey ? null : scope
      ));
      return;
    }
    const preferred = preferredSurfaceFocusRef.current?.key === focusScopeKey
      ? state.sessions.find((session) => (
          session.session_id === preferredSurfaceFocusRef.current?.sid
          && (session.space ?? "code") === spaceRef.current
          && (session.engine ?? "claude") === engineRef.current
        ))
      : undefined;
    preferredSurfaceFocusRef.current = null;
    const latest = preferred ?? selectSurfaceSession(state.sessions);
    didInitFocusRef.current = true;
    if (latest && latest.session_id !== state.focusedSid) {
      dispatch({ type: "exit_new_chat" });
      dispatch({ type: "focus_session", sid: latest.session_id });
      const latestEngine = (latest.engine as "claude" | "codex" | "dsh") || engineRef.current;
      wsRef.current.setFocusedSid(latest.session_id, latestEngine, spaceRef.current);
      requestHistory(
        latest.session_id, undefined, HISTORY_INITIAL_PAGE);
      resumeListedSession(
        latest, latestEngine, spaceRef.current, wsRef.current);
    }
    setRestoringSurfaceScope((scope) => (
      scope === focusScopeKey ? null : scope
    ));
  }, [
    machineId,
    pendingNotificationTarget,
    restoringSurfaceScope,
    state.newChat,
    state.sessions,
    state.focusedSid,
    requestHistory,
    resumeListedSession,
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
    lastFocusBySurfaceRef.current[
      sessionScopeKey(machineId, selectedEngine, selectedSpace)
    ] = focusedSid;
  }, [focusedSid, state.newChat, state.sessions, engine, machineId]);

  // Warm the cwd/account-scoped Skill catalog when a session becomes usable.
  // Composer completion then reads memory synchronously; an expired entry stays
  // visible while this refresh runs in the background.
  useEffect(() => {
    if (!authed || !focusedSid || state.newChat
        || !capabilityCwd || state.connState !== "connected"
        || !state.wrapperOnline) return;
    requestSkillCatalog({
      key: focusedSkillCatalogKey,
      engine: focusedEngine,
      space,
      cwd: capabilityCwd,
      skillsOnly: true,
      claudeProfileId: focusedClaudeProfileId,
      codexProfileId: focusedCodexProfileId,
      sid: focusedSid,
    });
  }, [
    authed,
    capabilityCwd,
    focusedClaudeProfileId,
    focusedEngine,
    focusedCodexProfileId,
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
      claudeProfileId: focusedClaudeProfileId,
      codexProfileId: focusedCodexProfileId,
      sid: focusedSid,
    }, true);
  }, [
    authed,
    capabilitiesOpen,
    capabilityCwd,
    focusedClaudeProfileId,
    focusedEngine,
    focusedCodexProfileId,
    focusedSkillCatalogKey,
    focusedSid,
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
        rt.historyHeadKnown && !rt.hasMore && !rt.historyInvalidated,
      );
    });
  }, [
    focusedSid, rt.turns, rt.ccSessionId, rt.historyRevision, rt.control,
    rt.historyHeadKnown, rt.hasMore, rt.historyInvalidated,
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
          && (cached.turns.length || cached.control
            || cached.historyAtStart === true);
      if (valid && cached) {
        dispatch({
          type: "hydrate_cache", sid,
          turns: (cached.turns as Turn[]).map((turn) => ({
            ...turn, detailLoading: false,
          })),
          revision: cached.revision,
          generation: cached.generation ?? cached.control?.generation,
          control: cached.control,
          historyAtStart: cached.historyAtStart === true,
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
        if (btwPanelVisibleRef.current && latest.rightView === "btw") {
          latest.collapseBtw();
        }
        else latest.openBtw();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [authed]);

  const setPerm = useCallback((perm: string) => {
    if (focusedEngine === "dsh" && focusedSid) wsRef.current?.sendDshControl(focusedSid, "permission", perm);
    else wsRef.current?.sendSetPerm(perm);
  }, [focusedEngine, focusedSid]);

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
        const modes = focusedEngine === "dsh"
          ? (rt.dsh?.permissions ?? []).filter(p => p.value !== "custom").map(p => p.value)
          : permsFor(focusedEngine).map((p) => p.id);
        if (!modes.length) return;
        const current = modes.indexOf(rt.perm);
        setPerm(modes[current < 0 ? 0 : (current + 1) % modes.length]);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [authed, focusedSid, rt.perm, rt.collaborationMode, rt.control,
    rt.external, rt.dsh?.permissions, focusedEngine, setPerm]);

  const requestHistoryTurnDetail = useCallback((
    displayTurnId: string, before?: string | null,
    autoLoad = false,
    includeBrowseProjection = true,
  ): boolean => {
    const current = stateRef.current;
    const sid = current.focusedSid;
    const runtime = sid ? current.runtimes[sid] : null;
    if (!sid || !runtime?.historyRevision) return false;
    const browse = includeBrowseProjection && current.historyBrowse?.sid === sid
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
    const registration = historyDetailRequestsRef.current.register(context);
    if (!registration.accepted) return false;
    const sent = !registration.send || (
      wsRef.current?.sendGetTurnDetail(
        sid, turnId, context.revision, before) ?? false
    );
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
  const loadHistoryTurnDetail = useCallback((
    displayTurnId: string, before?: string | null,
    autoLoad = false,
  ) => requestHistoryTurnDetail(
    displayTurnId, before, autoLoad, true), [requestHistoryTurnDetail]);
  const loadRuntimeTurnDetail = useCallback((
    displayTurnId: string, before?: string | null,
    autoLoad = false,
  ) => requestHistoryTurnDetail(
    displayTurnId, before, autoLoad, false), [requestHistoryTurnDetail]);
  useEffect(() => {
    const current = stateRef.current;
    const sid = current.focusedSid;
    const runtime = sid ? current.runtimes[sid] : null;
    if (!sid || !runtime || current.newChat
        || current.historyBrowse?.sid === sid
        || current.connState !== "connected" || !current.wrapperOnline
        || !runtime.syncReady || !runtime.historyHeadKnown
        || runtime.historyInvalidated || !runtime.historyRevision) return;
    const request = nextActiveDetailRequest(
      runtime.turns,
      runtime.historyNewestId,
      runtime.state !== "idle" || runtime.mirroredRunning,
    );
    if (!request) return;
    // One newest page establishes the immutable source snapshot. Older pages
    // stay behind the existing explicit pagination controls while live tail
    // frames continue merging independently into the same turn.
    loadRuntimeTurnDetail(request.turnId, request.before, false);
  }, [
    loadRuntimeTurnDetail,
    state.connState,
    state.focusedSid,
    state.historyBrowse,
    state.newChat,
    state.runtimes,
    state.wrapperOnline,
  ]);
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
        || focusedSession?.tag === "archived"
        || state.connState !== "connected" || !state.wrapperOnline) return;
    const contextRuntime = stateRef.current.runtimes[focusedSid];
    const deferred = contextRuntime?.contextRefreshDeferred === true;
    if (!contextRuntime?.contextRequestId
        && (!deferred || focusedEngine === "claude" && contextRuntime?.state === "idle")) {
      sendContextRequestTo(focusedSid, deferred);
    }
    // A long Codex turn can compact before TurnEnd. These visible-session
    // reads are bounded and never invoke a model or resume an engine.
    if (focusedEngine !== "codex") return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible"
          && stateRef.current.runtimes[focusedSid]?.state === "running") {
        sendContextRequestTo(focusedSid, false);
      }
    }, 5000);
    return () => window.clearInterval(timer);
  }, [
    authed,
    focusedEngine,
    focusedSession?.tag,
    focusedSid,
    sendContextRequestTo,
    state.connState,
    state.newChat,
    state.wrapperOnline,
  ]);

  const refreshStatus = useCallback(() => {
    if (!focusedSid || focusedEngine !== "codex") return;
    const current = stateRef.current;
    if (current.sessions.find(
      (session) => session.session_id === focusedSid)?.tag === "archived") return;
    if (current.runtimes[focusedSid]?.statusRequestId) return;
    const requestId = wsRef.current?.sendGetStatus();
    if (requestId) {
      dispatch({ type: "begin_status_request", sid: focusedSid, requestId });
    }
  }, [focusedEngine, focusedSid]);
  const consumeResetCredit = useCallback((creditId?: string | null) => {
    if (!focusedSid || focusedEngine !== "codex") return false;
    const current = stateRef.current;
    const runtime = current.runtimes[focusedSid];
    const session = current.sessions.find(
      (candidate) => candidate.session_id === focusedSid);
    if (
      current.newChat
      || session?.tag === "archived"
      || !runtime
      || runtime.state !== "idle"
      || runtime.statusRequestId
      || runtime.statusError
      || current.connState !== "connected"
      || !current.wrapperOnline
    ) return false;
    const requestId = wsRef.current?.sendConsumeRateLimitResetCredit(
      focusedSid, creditId) ?? null;
    if (requestId) {
      dispatch({
        type: "begin_status_request",
        sid: focusedSid,
        requestId,
      });
    }
    return requestId !== null;
  }, [focusedEngine, focusedSid]);
  useEffect(() => {
    if (!authed || !focusedSid || focusedEngine !== "codex" || state.newChat
        || archivedBrowse
        || rt.state !== "idle"
        || state.connState !== "connected" || !state.wrapperOnline) return;
    if (stateRef.current.runtimes[focusedSid]?.statusRequestId) return;
    refreshStatus();
  }, [
    authed,
    archivedBrowse,
    focusedEngine,
    focusedSid,
    refreshStatus,
    rt.state,
    state.connState,
    state.newChat,
    state.wrapperOnline,
  ]);

  useEffect(() => {
    const artifact = state.artifact;
    const targetSid = artifact?.sid;
    if (!artifact || !targetSid) return;
    const authorization = artifact.authorization?.status === "granted"
      ? artifact.authorization
      : Object.values(artifact.assets ?? {}).find(
        (asset) => asset.authorization?.status === "granted",
      )?.authorization;
    if (!authorization) return;
    const ws = wsRef.current;
    const sent = authorization.operation === "file_preview"
      ? !!ws?.sendGetFilePreview(
        authorization.path,
        authorization.requestId,
        targetSid,
      )
      : !!authorization.previewId && !!ws?.sendGetPreviewAsset(
        authorization.path,
        authorization.previewId,
        authorization.requestId,
        targetSid,
      );
    dispatch({
      type: sent
        ? "preview_authorization_retry_started"
        : "preview_authorization_retry_failed",
      sid: targetSid,
      authorizationId: authorization.authorizationId,
      requestId: authorization.requestId,
    });
  }, [state.artifact]);

  if (!authReady) {
    return <div className="login" aria-busy="true">正在连接中继…</div>;
  }

  if (!authed) {
    return <LoginForm onLogin={() => { dispatch({ type: "reset" }); setAuthed(true); }} theme={theme} onToggleTheme={toggleTheme} />;
  }

  const runtimeIsReadOnly = (sid: string): boolean => {
    const runtime = stateRef.current.runtimes[sid];
    return runtime?.control
      ? sessionControlLocksInput(runtime.control) : !!runtime?.external;
  };
  const sendDeferredQuery = (
    sid: string,
    query: PendingQuery,
    delivery: "queue" | "replace",
  ): boolean => {
    const ws = wsRef.current;
    if (!ws || runtimeIsReadOnly(sid)) return false;
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
    if (!current || current.saving || runtimeIsReadOnly(current.sid)) return false;
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
    if (!ws || !focusedSid || (focusedEngine !== "codex" && focusedEngine !== "dsh")) return false;
    const runtime = stateRef.current.runtimes[focusedSid];
    if (ws.pendingQueryFor(focusedSid) || runtime?.acceptancePending) {
      return false;
    }
    const msg_id = uuid();
    if (!ws.sendSteerTo(focusedSid, prompt, msg_id, images, files)) return false;
    dispatch({
      type: "steer_sent",
      sid: focusedSid,
      prompt,
      msg_id,
      images,
      files,
      ts: Date.now(),
    });
    return true;
  };
  const replyAsyncQuestion = (
    sid: string, prompt: string,
    whenIdle: (text: string) => boolean, whenRunning: (text: string) => boolean,
  ): boolean => {
    const current = stateRef.current;
    const ws = wsRef.current;
    const runtime = current.runtimes[sid];
    // The outbox latch updates synchronously, before React can disable sibling
    // cards. A second answer must not enter the composer's replace-query path.
    if (!ws || current.connState !== "connected" || !current.wrapperOnline
        || !runtime || ws.pendingQueryFor(sid) || runtime.acceptancePending
        || (runtime.control
          ? sessionControlLocksInput(runtime.control) : runtime.external)) return false;
    return runtime.state === "running" ? whenRunning(prompt)
      : runtime.state === "idle" ? whenIdle(prompt) : false;
  };
  const loadOlderHistoryPage = (
    anchorTurnId?: string,
  ): boolean | {
    accepted: true;
    viewId: string;
    scopeKey: string;
    generation: string | null;
  } => {
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
    return accepted ? {
      accepted: true,
      viewId,
      scopeKey,
      generation: browseGeneration,
    } : false;
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
    const latestPageKey = `${HISTORY_LATEST_PAGE_KEY}:${frozen.viewId}`;
    // The synthetic latest page is written to IndexedDB asynchronously. A
    // quick down-swipe can reach it before that write completes; its stable
    // page key is already sufficient to return to the authoritative live tail.
    if (cachedLatestRequiresLiveRuntime(browse, null, latestPageKey)) {
      dispatch({ type: "return_to_latest", sid: frozen.sid });
      return true;
    }
    void (async () => {
      const page = await historyPageCacheRef.current.getPage(
        scope, frozen.pageKey);
      const currentState = stateRef.current;
      const current = currentState.historyBrowse;
      if (currentState.focusedSid !== frozen.sid
          || !current
          || !acceptsCachedNewerPage(current, frozen)) return;
      if (cachedLatestRequiresLiveRuntime(current, page, latestPageKey)) {
        // loadNewerHistoryPage is accepted only from a real downward gesture.
        // The cached latest page became stale while Claude was still writing,
        // so honor that gesture by returning to the authoritative live tail—the
        // same destination as the explicit bottom button.
        dispatch({ type: "return_to_latest", sid: frozen.sid });
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
    if (!wsRef.current || !state.newChat || newChatCodexProfileMissing
        || newChatClaudeProfileMissing
        || restoringSurfaceScope === activeScopeKey) {
      return false;
    }
    const {
      cwd, cwdSource, model, effort,
      autoCompactMode, autoCompactThresholdTokens,
    } = state.newChat;
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
      space, space === "work" ? activeWorkProjectId : undefined,
      engine === "codex" ? newChatCodexProfileId : undefined,
      engine === "claude" ? {
        mode: autoCompactMode,
        thresholdTokens: autoCompactThresholdTokens,
      } : undefined,
      engine === "claude" ? newChatClaudeProfileId : undefined,
      engine === "dsh" && dshPresetSelection?.scope === activeScopeKey ? dshPresetSelection.value : undefined);
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
      newChatCatalog,
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
  const pickNewChatAutoCompact = (selection: AutoCompactSelection) => {
    if (!state.newChat || engine !== "claude") return;
    dispatch({
      type: "set_new_chat_auto_compact",
      mode: selection.mode,
      thresholdTokens: selection.thresholdTokens,
    });
  };
  const pickNewChatCodexProfile = (profileId: string) => {
    if (engine !== "codex") return;
    setNewChatPermissionCatalog(null);
    dispatch({
      type: "set_new_chat_codex_profile",
      scopeKey: activeScopeKey,
      profileId,
    });
  };
  const pickNewChatClaudeProfile = (profileId: string) => {
    if (engine !== "claude") return;
    dispatch({
      type: "set_new_chat_claude_profile",
      scopeKey: activeScopeKey,
      profileId,
    });
  };
  const interrupt = () => wsRef.current?.sendInterrupt();
  const setModel = (model: string) => {
    wsRef.current?.sendSetModel(model);
  };
  const setEffort = (effort: string) => {
    if (focusedEngine === "dsh" && focusedSid) wsRef.current?.sendDshControl(focusedSid, "effort", effort);
    else wsRef.current?.sendSetEffort(effort);
  };
  const setAutoCompact = (selection: AutoCompactSelection): boolean => (
    wsRef.current?.sendSetAutoCompact(
      selection.mode, selection.thresholdTokens) ?? false
  );
  // Codex Fast mode is persisted by app-server per thread. The runtime's Fast
  // event owns the chip state; here we only forward the requested transition.
  const setServiceTier = (tier: string) => {
    wsRef.current?.sendSetServiceTier(tier);
  };
  const getPermissionProfiles = () => {
    wsRef.current?.sendGetPermissionProfiles();
  };
  const setPermissionProfile = (profile: string) => {
    const commandId = wsRef.current?.sendSetPermissionProfile(profile);
    if (commandId && focusedSid) {
      setPermissionProfileRequests((current) => ({
        ...current, [JSON.stringify([machineId, space, focusedEngine, focusedSid])]: commandId,
      }));
    }
  };
  const setWebSearch = (mode: CodexWebSearchMode) => {
    wsRef.current?.sendSetWebSearch(mode);
  };
  const setCollaborationMode = (mode: CollaborationModeName) => {
    wsRef.current?.sendSetCollaborationMode(mode);
  };
  const setGoalUi = (patch: Partial<{
    revealed: boolean;
    open: boolean;
    loading: boolean;
  }>) => {
    if (!focusedGoalScopeKey) return;
    setGoalUiByScope((current) => {
      const previous = current[focusedGoalScopeKey] ?? {
        revealed: false,
        open: false,
        loading: false,
      };
      return {
        ...current,
        [focusedGoalScopeKey]: { ...previous, ...patch },
      };
    });
  };
  const rememberFocusedGoalUi = () => {
    if (!focusedGoalScopeKey) return;
    persistGoalUiPreferences(rememberGoalUi(
      goalUiPreferencesRef.current,
      focusedGoalScopeKey,
    ));
  };
  const requestFocusedGoal = () => {
    if (!focusedSid || !focusedGoalScopeKey || archivedBrowse) return null;
    const requestId = wsRef.current?.sendGetGoalTo(focusedSid) ?? null;
    if (requestId) {
      goalRequestScopeByIdRef.current.set(requestId, {
        scopeKey: focusedGoalScopeKey,
        sid: focusedSid,
      });
    }
    return requestId;
  };
  const runGoal = (args: string) => {
    if (!focusedSid || !focusedGoalScopeKey || archivedBrowse) return;
    const command = parseGoalCommand(args, focusedEngine);
    rememberFocusedGoalUi();
    if (command.kind === "clear") {
      wsRef.current?.sendClearGoal();
      setGoalUi({ revealed: false, open: false, loading: false });
      return;
    }
    setGoalUi({ revealed: true, open: true, loading: command.kind === "show" });
    if (command.kind === "show") {
      requestFocusedGoal();
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
    if (!focusedSid || archivedBrowse) return;
    setStatusOpenSid(focusedSid);
    refreshStatus();
  };
  const openUsageActivity = () => {
    if (engine !== "codex" || archivedBrowse) return;
    setUsageActivityOpen(true);
    refreshStatus();
  };
  const requestContext = () => {
    if (!focusedSid || archivedBrowseRef.current === focusedSid) return;
    const runtime = stateRef.current.runtimes[focusedSid];
    if (runtime?.contextRequestId
        || contextRequestLaunchesRef.current.has(focusedSid)) return;
    if (focusedEngine === "claude" && runtime
        && (runtime.state !== "idle" || runtime.queue.length > 0)) {
      dispatch({ type: "defer_context_request", sid: focusedSid });
      return;
    }
    // Closing and reopening is an explicit retry even if a previous busy
    // response exhausted its automatic finalizer catch-up attempts.
    contextDeferredRetryAttemptsRef.current.delete(focusedSid);
    sendContextRequestTo(focusedSid, true);
  };
  const forkFromTurn = (forkPointId: string) => {
    if (!focusedSid
        || archivedBrowseRef.current === focusedSid
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
    closeViewer();
    const requestId = wsRef.current?.sendGetDiff(file, theme) ?? null;
    if (!requestId) return;
    setRightView("diff");
    dispatch({ type: "open_artifact_loading", file, sid: focusedSid, requestId });
  };
  const openTurnDiff = (files: string[], diff: string) => {
    if (!diff || !confirmArtifactDiscard()) return;
    closeViewer();
    setRightView("diff");
    dispatch({ type: "set_artifact", artifact: {
      file: files.length === 1 ? files[0] : `本轮改动 · ${files.length} 个文件`,
      sid: focusedSid,
      kind: "gitdiff",
      sections: parseGitDiff(diff).filter((section) => files.includes(section.file)),
    } });
  };
  const openArchivedDiff = (turnId: string, revision: string, file: string) => {
    if (!confirmArtifactDiscard()) return;
    const requestId = wsRef.current?.sendGetDiff(file, theme, turnId, revision, focusedEngine) ?? null;
    if (!requestId) return;
    closeViewer();
    setRightView("diff");
    dispatch({ type: "open_artifact_loading", file, sid: focusedSid, requestId });
  };
  const loadTurnFilePage: LoadTurnFilePage = (turnId, revision, offset, signal) => {
    if (!focusedSid) return Promise.reject(new Error("请先选择会话。"));
    return turnFileRequestsRef.current.request({
      sid: focusedSid, engine: focusedEngine, turnId, revision, offset,
    }, () => wsRef.current?.sendGetTurnFileChanges(
      focusedSid, focusedEngine, turnId, revision, offset) ?? null, signal);
  };
  const previewFileForSid = (
    targetSid: string | null,
    file: string,
    line?: number,
    fromBrowser = false,
  ): boolean => {
    if (!targetSid) return false;
    if (!confirmArtifactDiscard()) return false;
    if (!fromBrowser) setFileBrowser(null);
    closeViewer();
    const requestId = uuid();
    if (!wsRef.current?.sendGetFilePreview(
      file, requestId, targetSid)) return false;
    setRightView("diff");
    dispatch({
      type: "open_file_loading",
      file,
      sid: targetSid,
      requestId,
      kind: isMarkdownPath(file) ? "md" : "file",
      line,
    });
    return true;
  };
  const previewFile = (file: string, line?: number) =>
    previewFileForSid(focusedSid, file, line);
  const previewBtwFile = (file: string, line?: number) =>
    previewFileForSid(activeBtwSid, file, line);

  const openAgentDetail = (runId: string, title?: string) => {
    if (!focusedSid || focusedEngine !== "claude" || space !== "code"
        || !rt.historyRevision) return;
    closeViewer();
    setAgentPanel({ sid: focusedSid, revision: rt.historyRevision,
      runId, title: title || "协作代理" });
  };
  const previewAgentFile = (file: string, line?: number) => {
    if (previewFileForSid(focusedSid, file, line)) setAgentPanel(null);
  };

  const previewArtifactFile = (file: string, line?: number) =>
    previewFileForSid(state.artifact?.sid ?? focusedSid, file, line);
  const previewMarkdown = (file: string) => previewFile(file);
  const openFiles = (path = ".") => {
    if (!focusedSid || !confirmArtifactDiscard()) return;
    closeViewer();
    setAgentPanel(null);
    dispatch({ type: "clear_artifact" });
    setFileBrowser({ sid: focusedSid, machineId, engine, space,
      path, preview: false, id: uuid() });
  };
  const loadPreviewAsset = (file: string, previewId: string): boolean => {
    const targetSid = state.artifact?.sid ?? focusedSid;
    if (!targetSid) return false;
    const requestId = uuid();
    if (!wsRef.current?.sendGetPreviewAsset(
      file, previewId, requestId, targetSid)) return false;
    dispatch({
      type: "begin_preview_asset",
      sid: targetSid,
      path: file,
      previewId,
      requestId,
    });
    return true;
  };
  const authorizePreview = (
    authorization: PreviewAuthorizationState,
    decision: "allow" | "deny",
  ): boolean => {
    const artifact = stateRef.current.artifact;
    const targetSid = artifact?.sid;
    if (!artifact || !targetSid) return false;
    const activeAuthorization = artifact.authorization
      ?? Object.values(artifact.assets ?? {}).find(
        (asset) => (
          asset.authorization?.authorizationId
            === authorization.authorizationId
          && asset.authorization.requestId === authorization.requestId
        ),
      )?.authorization;
    if (!activeAuthorization
        || activeAuthorization.authorizationId
          !== authorization.authorizationId
        || activeAuthorization.requestId !== authorization.requestId
        || activeAuthorization.status !== "required") return false;
    if (!wsRef.current?.sendAuthorizePreview(
      authorization.authorizationId,
      authorization.requestId,
      decision,
      targetSid,
    )) return false;
    dispatch({
      type: "submit_preview_authorization",
      sid: targetSid,
      authorizationId: authorization.authorizationId,
      requestId: authorization.requestId,
    });
    return true;
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
  // Each side chat stays pinned to its parent. Panel visibility is a pure view
  // concern; only closeBtw sends the destructive native-fork teardown.
  const createBtw = () => {
    const parentSid = visibleParentSid;
    if (!parentSid || pendingBtwByParentRef.current.has(parentSid)) return;
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
  const openBtw = () => {
    if (!confirmArtifactDiscard()) return;
    if (!btwPanelKey) return;
    closeViewer();
    setRightView("btw");
    setBtwPanelScopes((current) => setBtwPanelScope(current, btwPanelKey, true));
    if (!activeBtwGroup && !btwOpening) createBtw();
  };
  const collapseBtw = () => {
    if (btwPanelKey) setBtwPanelScopes(
      (current) => setBtwPanelScope(current, btwPanelKey, false));
  };
  const selectBtw = (sid: string) => {
    const parentSid = visibleParentSid;
    if (!parentSid) return;
    dispatch({ type: "select_btw", parentSid, btwSid: sid });
  };
  const sendBtw = (prompt: string): boolean => {
    const sid = activeBtwSid;
    const ws = wsRef.current;
    if (!sid || !ws || runtimeIsReadOnly(sid)) return false;
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
    if (!sid || !ws || activeBtw?.engine !== "codex" || runtimeIsReadOnly(sid)) return false;
    const runtime = stateRef.current.runtimes[sid];
    if (ws.pendingQueryFor(sid) || runtime?.acceptancePending) return false;
    const msg_id = uuid();
    if (!ws.sendSteerTo(sid, prompt, msg_id)) return false;
    dispatch({
      type: "steer_sent", sid, prompt, msg_id, ts: Date.now(),
    });
    return true;
  };
  const interruptBtw = (sid: string) => {
    if (runtimeIsReadOnly(sid)) return;
    wsRef.current?.sendInterruptTo(sid);
  };
  const setBtwModel = (sid: string, model: string) => {
    if (runtimeIsReadOnly(sid)) return;
    wsRef.current?.sendSetModelTo(sid, model);
  };
  const setBtwEffort = (sid: string, effort: string) => {
    if (runtimeIsReadOnly(sid)) return;
    wsRef.current?.sendSetEffortTo(sid, effort);
  };
  const setBtwAutoCompact = (
    sid: string, selection: AutoCompactSelection,
  ): boolean => {
    if (runtimeIsReadOnly(sid)) return false;
    return wsRef.current?.sendSetAutoCompactTo(
      sid, selection.mode, selection.thresholdTokens) ?? false;
  };
  const setBtwSendMode = (
    sid: string, mode: SendMode,
  ) => {
    setBtwSendModeBySid((current) => (
      current[sid] === mode ? current : { ...current, [sid]: mode }
    ));
  };
  const closeBtw = (btwSid = activeBtwSid) => {
    const parentSid = visibleParentSid;
    if (!parentSid || !btwSid) return;
    const target = stateRef.current.runtimes[btwSid];
    const hasActiveWork = !!target && (
      target.state !== "idle" || target.mirroredRunning
      || !!target.acceptancePending || !!target.pendingSend
      || target.queue.length > 0
    );
    if (hasActiveWork && !window.confirm(
      "这个侧边对话仍在工作或有排队消息，确定关闭并丢弃吗？")) return;
    if (!wsRef.current?.sendCloseBtw(btwSid)) return;
    const tracked = activeBtwByParentRef.current.get(parentSid);
    if (tracked?.sid === btwSid) activeBtwByParentRef.current.delete(parentSid);
    btwDraftsRef.current.delete(composerDraftKey(
      machineId, space,
      (activeBtwGroup?.chats.find((chat) => chat.sid === btwSid)?.engine
        === "codex" ? "codex" : "claude"),
      `btw:${btwSid}`,
    ));
    setBtwSendModeBySid((current) => {
      if (!(btwSid in current)) return current;
      const next = { ...current };
      delete next[btwSid];
      return next;
    });
    setCompletionReceipts((receipts) => acknowledgeCompletion(
      receipts, parentSid, { btwSid }));
    dispatch({ type: "clear_btw", parentSid, btwSid });
  };
  // Header tab switch between the two right-slot views (opening the target lazily).
  const switchRight = (v: RightPanelView) => {
    if (fileBrowser && !confirmArtifactDiscard()) return;
    setFileBrowser(null);
    if (v === "diff") {
      setRightView("diff");
      if (!state.artifact) getDiff("");
    } else {
      openBtw();
    }
  };
  shortcutRef.current = {
    artifact: state.artifact, btwSid: activeBtwSid, rightView,
    getDiff, openBtw, collapseBtw,
  };
  const logout = async () => {
    try {
      const response = await fetch("/api/logout", {
        method: "POST", credentials: "same-origin", cache: "no-store",
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      try { localStorage.removeItem(MANUAL_UNREAD_STORAGE); } catch { /* Private browsing. */ }
      await import("./cache").then((module) => module.clearCache());
      await historyPageCacheRef.current.clear();
      wsRef.current?.stop();
      pendingCreateRef.current = null;
      createRequestsRef.current.clear();
      pendingBtwByParentRef.current.clear();
      pendingSessionForkRef.current = null;
      pendingWorktreeForkRef.current = null;
      pendingSessionMigrationRef.current = null;
      clearForkFocusLease(false);
      setMigrateSession(null);
      setMigrateCreating(false);
      setMigrateError(null);
      sessionActivityPendingRef.current.clear();
      activeBtwByParentRef.current.clear();
      btwRequestParentsRef.current.clear();
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
      setRestoringSurfaceScope(null);
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
  // Fail closed when a migrated cache contains colliding display aliases. A
  // session-level running bit alone must never animate the wrong historical
  // row, another account, or a read-only browse projection.
  const runtimeActiveOwnerId = displayActiveTurnOwnerId(
    rt.liveOwner?.turnId, rt.acceptancePending);
  const displayActiveOwnerId = historyView.recovering
    ? historyView.activeOwnerId : runtimeActiveOwnerId;
  const runtimeHasActiveTurn = rt.state !== "idle" || rt.mirroredRunning
    || !!rt.acceptancePending;
  const activeTurnCandidates = activeTurnCandidateIds(
    historyView.turns,
    displayActiveOwnerId,
    !historyView.browsing && runtimeHasActiveTurn
      && (!historyView.recovering || !!historyView.activeOwnerId),
  );
  const activeTurnId = activeTurnCandidates.length === 1
    ? activeTurnCandidates[0] : null;
  const ambiguousActiveTurnIds = activeTurnCandidates.length > 1
    ? activeTurnCandidates : [];

  return (
    <ViewerPagesProvider scope={visibleParentSid && authed
      ? { machineId, sid: visibleParentSid, space, engine } : null} onOpen={openViewerPage}>
    <RemoteViewerContext.Provider value={visibleParentSid ? openViewerLink : null}>
    <div className={"shell" + (sidebarOpen ? " sidebar-open" : "") + (visibleRightPanel !== null ? " panel-open" : "")} onTouchStart={onTouchStart} onTouchEnd={onTouchEnd}>
      <Suspense fallback={null}><SessionsSidebar
        open={sidebarOpen}
        engine={engine}
        space={space}
        profileScopeKey={activeScopeKey}
        claudeProfiles={state.claudeProfiles}
        defaultClaudeProfileId={state.defaultClaudeProfileId}
        codexProfiles={state.codexProfiles}
        defaultCodexProfileId={state.defaultCodexProfileId}
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
        machineId={machineId}
        readDsh={engine === "dsh" ? dshApi.read : undefined}
        onDshSearch={(sid, query) => {
          if (!focusedSid && !state.sessions.length) return;
          setDshPanel({ sid: focusedSid ?? state.sessions[0].session_id, scope: activeScopeKey,
            selection: { kind: "conversation", target: sid, query } });
          if (isMobile()) setSidebarOpen(false);
        }}
        onSelect={(id) => {
          if (!confirmArtifactDiscard()) return false;
          cancelPendingNotificationTarget();
          const selected = state.sessions.find((s) => s.session_id === id);
          if (!selected) return false;
          focusListedSession(selected);
          return true;
        }}
        onNew={(profileId) => { if (!confirmArtifactDiscard()) return; clearForkFocusLease(false); cancelPendingNotificationTarget(); pendingCreateRef.current = null; setCreateError(null); setStatusOpenSid(null); setNewChatAutoFocus(true); setRestoringSurfaceScope(null); wsRef.current?.setFocusedSid(null); dispatch({ type: "enter_new_chat", cwd: "~", cwdSource: "default", claudeProfileId: engine === "claude" ? profileId ?? newChatClaudeProfileId : null, codexProfileId: engine === "codex" ? profileId ?? newChatCodexProfileId : null }); if (isMobile()) setSidebarOpen(false); }}
        onNewInDir={(cwd) => { if (!confirmArtifactDiscard()) return; clearForkFocusLease(false); cancelPendingNotificationTarget(); pendingCreateRef.current = null; setCreateError(null); setStatusOpenSid(null); setNewChatAutoFocus(true); setRestoringSurfaceScope(null); wsRef.current?.setFocusedSid(null); dispatch({ type: "enter_new_chat", cwd, cwdSource: "explicit", claudeProfileId: newChatClaudeProfileId, codexProfileId: newChatCodexProfileId }); if (isMobile()) setSidebarOpen(false); }}
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
          if (forkFocusLeaseRef.current?.childSessionId === id) {
            clearForkFocusLease(false);
          }
          composerDraftsRef.current.delete(composerDraftKey(
            machineId, target.space, target.engine, id,
          ));
          invalidateHistoryPageScopes(id);
          if (focusedSid === id) clearHistoryDetailRequests();
          if (focusedSid === id) dispatch({
            type: "enter_new_chat", cwd: "~", cwdSource: "default",
            claudeProfileId: newChatClaudeProfileId,
            codexProfileId: newChatCodexProfileId,
          });
          wsRef.current?.sendDeleteSession(
            id, target.engine, target.space);
        }}
        onForkWorktree={openForkWorktree}
        onMigrate={openSessionMigration}
      /></Suspense>
      {dirPickerOpen && <Suspense fallback={null}><DirPicker
        open={dirPickerOpen}
        path={state.dirPicker?.path ?? null}
        parent={state.dirPicker?.parent ?? null}
        dirs={state.dirPicker?.dirs ?? []}
        responseRequestId={state.dirPicker?.requestId ?? null}
        onBrowse={(p) => wsRef.current?.sendListDir(p) ?? null}
        onConfirm={(cwd) => { if (state.newChat) dispatch({ type: "set_new_chat_cwd", cwd, cwdSource: "explicit" }); setDirPickerOpen(false); }}
        onClose={() => setDirPickerOpen(false)}
      /></Suspense>}
      {migrateSession !== null && <Suspense fallback={null}><DirPicker
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
      /></Suspense>}
      <section className={`pane ${space}-pane`}>
        <header className={`c-head ${space}-head`}>
          <div className="titlewrap">
            <div className="ttl">
              <button className="surface-head-title" onClick={() => setSidebarOpen(true)}>
                <span className="surface-head-mark"><Icon name={space === "work" ? "work" : "code"} size={18} /></span>
                <span>{space === "work" ? "Work" : "Code"}</span>
              </button>
              {focusedWorkProfile && (
                <span className={`work-profile-owner tone-${focusedWorkProfile.tone}`}
                  title={`${focusedEngine === "codex" ? "Codex" : "Claude"} 账号：${focusedWorkProfile.fullLabel}`}
                  aria-label={`${focusedEngine === "codex" ? "Codex" : "Claude"} 账号：${focusedWorkProfile.fullLabel}`}>
                  <i className="profile-tone" />
                  {focusedWorkProfile.name}
                </span>
              )}
            </div>
            <div className="sub">{space === "work" ? "私有工作区 · " : ""}{focusedNativeSessionId ? `session ${focusedNativeSessionId.slice(0, 8)}` : "connected"}</div>
          </div>
          <span className={`hstat ${effectiveState}`}><span className="sd" />
            <span className="hstat-label">{effectiveState}</span></span>
          {space === "code" && focusedSid && !state.newChat
              && !archivedBrowse && focusedEngine !== "dsh" && (
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
          <span className="engine-selector"><select className="engine-toggle" value={engine}
            onPointerDown={() => { harnessPointerSelectionRef.current = true; }}
            onKeyDown={() => { harnessPointerSelectionRef.current = false; }}
            onBlur={() => { harnessPointerSelectionRef.current = false; }}
            onChange={event => {
              toggleEngine(event.target.value as Engine);
              if (harnessPointerSelectionRef.current) event.currentTarget.blur();
            }}
            aria-label="切换新会话引擎" title="新建会话使用的引擎">
            <option value="claude">✳ Claude</option>
            <option value="codex">◇ Codex</option>
            <option value="dsh">DSH</option>
          </select><span className="engine-label" aria-hidden="true">
            {engine === "dsh" ? "DSH" : engine === "codex" ? "◇ Codex" : "✳ Claude"}
            <Icon name="chev" size={12} />
          </span></span>
          <HeaderMenu
            engine={engine}
            theme={theme}
            notificationMode={notificationMode}
            notificationBinding={pushBinding.state}
            notificationAvailable={typeof Notification !== "undefined"}
            onNotificationMode={updateNotificationMode}
            onOpenDshTools={engine === "dsh" && focusedSid ? () => setDshPanel({ sid: focusedSid, scope: activeScopeKey, selection: { kind: "subagents" } }) : undefined}
            onOpenUsageActivity={openUsageActivity}
            onOpenViewer={visibleParentSid ? () => openViewer() : undefined}
            onOpenFiles={visibleParentSid && !archivedBrowse && state.wrapperOnline ? () => openFiles() : undefined}
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

        {restoringSurfaceScope === activeScopeKey ? (
          <div className="empty" role="status" aria-live="polite">
            <span className="spinner" aria-hidden="true" />
            <span className="loading-tx">正在恢复会话</span>
          </div>
        ) : state.newChat ? (
          <Suspense fallback={null}><NewChatView cwd={state.newChat.cwd}
            dshPresets={state.dshPresets}
            dshError={state.dshError}
            dshPreset={dshPresetSelection?.scope === activeScopeKey ? dshPresetSelection.value : null}
            onPickDshPreset={value => setDshPresetSelection({ scope: activeScopeKey, value })}
            controlScopeKey={`${activeScopeKey}\u0000${newChatProfileId ?? "__default__"}`}
            space={space}
            createError={createError}
            autoFocus={newChatAutoFocus}
            engine={engine}
            catalog={newChatCatalog}
            model={state.newChat.model}
            effort={state.newChat.effort}
            autoCompact={{
              mode: state.newChat.autoCompactMode,
              thresholdTokens: state.newChat.autoCompactThresholdTokens,
            }}
            defaultModel={newChatDefaults.model}
            defaultEffort={newChatDefaults.effort}
            claudeProfiles={state.claudeProfiles}
            defaultClaudeProfileId={state.defaultClaudeProfileId}
            claudeProfileId={newChatClaudeProfileId}
            codexProfiles={state.codexProfiles}
            defaultCodexProfileId={state.defaultCodexProfileId}
            codexProfileId={newChatCodexProfileId}
            workDashboard={activeWorkDashboard}
            selectedProjectId={activeWorkProjectId}
            onSelectProject={setWorkProjectId}
            onManageWork={() => setWorkManagerOpen(true)}
            onPickCwd={() => setDirPickerOpen(true)}
            onPickModel={pickNewChatModel}
            onPickEffort={pickNewChatEffort}
            onPickAutoCompact={pickNewChatAutoCompact}
            onPickClaudeProfile={pickNewChatClaudeProfile}
            onPickCodexProfile={pickNewChatCodexProfile}
            permissionProfiles={
              newChatPermissionCatalog?.machineId === machineId
                && newChatPermissionCatalog.cwd === state.newChat.cwd
                && newChatPermissionCatalog.codexProfileId
                  === newChatCodexProfileId
                ? newChatPermissionCatalog.profiles
                : null
            }
            onGetPermissionProfiles={(cwd) => {
              wsRef.current?.sendGetPermissionProfiles(
                cwd, newChatCodexProfileId);
            }}
            onSend={sendFirstMessage} /></Suspense>
        ) : (
          <>
            <ChatView key={`${activeScopeKey}\u0000${focusedSid ?? ""}`}
              sid={focusedSid} turns={historyView.turns}
              loading={!!rt.loading || historyView.recovering}
              surface={space}
              engine={focusedEngine} forkingPointId={forkingPointId}
              hasMore={historyView.hasMore}
              historyPagingReady={historyView.pagingReady}
              historyRevision={rt.historyRevision}
              historyViewRevision={historyView.viewRevision}
              historyGeneration={historyView.generation}
              historyViewId={historyView.viewId}
              historyScopeKey={historyView.scopeKey ?? activeScopeKey}
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
              asyncReplyMode={rt.state === "running" ? "steer"
                : rt.state === "idle" ? "query" : undefined}
              onReplyAsyncQuestion={focusedEngine !== "codex"
                || historyView.recovering || !state.wrapperOnline
                || state.connState !== "connected"
                || focusedSession?.tag === "archived"
                || (rt.control ? sessionControlLocksInput(rt.control) : rt.external)
                || rt.acceptancePending || (rt.state !== "idle" && rt.state !== "running")
                ? undefined : (prompt) => {
                  const current = stateRef.current;
                  if (!focusedSid || current.focusedSid !== focusedSid
                      || previousMachineRef.current !== machineId) return false;
                  return replyAsyncQuestion(focusedSid, prompt, sendQuery, sendSteer);
                }}
              onGetDiff={historyView.recovering ? undefined : getDiff}
              onOpenTurnDiff={historyView.recovering
                ? undefined : openTurnDiff}
              onOpenArchivedDiff={historyView.recovering ? undefined : openArchivedDiff}
              onLoadFilePage={historyView.recovering ? undefined : loadTurnFilePage}
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
              onAuthorizeImage={historyView.recovering
                ? undefined : authorizeMessageImage}
              historyImageAssets={historyImageAssets}
              onLoadHistoryImage={historyView.recovering
                ? undefined : loadHistoryImage}
              onTextSelectionGuardChange={updateTextSelectionGuard}
              externalPlanProgress={planProgress ? {
                turnId: planProgress.turnId,
                itemId: planProgress.block.item_id,
              } : null}
              backgroundProcesses={focusedEngine === "claude"
                ? rt.backgroundProcesses : []}
              activeTurnId={activeTurnId}
              ambiguousActiveTurnIds={ambiguousActiveTurnIds}
              onOpenAgent={focusedEngine === "claude" && space === "code"
                ? openAgentDetail : undefined}
              onFork={!historyView.recovering && !archivedBrowse
                  && space === "code"
                ? forkFromTurn : undefined} />

            <Suspense fallback={((!archivedBrowse
                && ((goalUi?.revealed && !completedGoalRetired)
                  || goalUi?.open)) || planProgress)
              ? <span className="goal-suspense" role="status"
                  aria-label={planProgress ? "正在加载计划进度" : "正在加载 Goal"} />
              : null}>
              {focusedEngine === "dsh" && rt.dsh?.goal && <DshGoalPanel key={focusedSid}
                goal={rt.dsh.goal} disabled={!state.wrapperOnline || !rt.dsh.connected}
                onCommand={line => { if (focusedSid) wsRef.current?.sendDshControl(focusedSid, "command", line); }} />}
              <GoalPanel engine={focusedEngine} goal={rt.goal}
                revealed={!archivedBrowse && !!goalUi?.revealed}
                open={!archivedBrowse && !!goalUi?.open}
                loading={!!goalUi?.loading}
                completedGoalRetired={completedGoalRetired}
                plan={planProgress}
                onLoadPlanDetail={planProgress?.needsDetail
                    && !planProgress.detailLoading && !historyView.recovering
                  ? () => {
                      if (planProgressSource === "history") {
                        loadHistoryTurnDetail(planProgress.turnId);
                      } else {
                        loadRuntimeTurnDetail(planProgress.turnId);
                      }
                    }
                  : undefined}
                onOpen={() => {
                  rememberFocusedGoalUi();
                  requestFocusedGoal();
                  setGoalUi({ revealed: true, open: true, loading: !rt.goal });
                }}
                onClose={() => setGoalUi({ open: false })}
                onDismiss={() => {
                  if (focusedSid && rt.goalId) {
                    wsRef.current?.sendDismissGoalTo(
                      focusedSid, rt.goalId);
                  }
                  if (focusedGoalScopeKey) {
                    persistGoalUiPreferences(dismissGoalUi(
                      goalUiPreferencesRef.current,
                      focusedGoalScopeKey,
                      rt.goal,
                    ));
                  }
                  setGoalUi({ revealed: false, open: false, loading: false });
                }}
                onSave={(objective, status, budget) => {
                  rememberFocusedGoalUi();
                  wsRef.current?.sendSetGoal(
                    objective, status,
                    focusedEngine === "codex" ? budget : null);
                  setGoalUi({ revealed: true, open: false, loading: false });
                }}
                onClear={() => {
                  rememberFocusedGoalUi();
                  wsRef.current?.sendClearGoal();
                  setGoalUi({ revealed: false, open: false, loading: false });
                }} />
            </Suspense>

            <Composer
          dsh={rt.dsh}
          dshSid={focusedSid ?? undefined}
          readDsh={dshApi.read}
          dshCommandResult={rt.dshCommandResult}
          onDshCommand={(value, images, files) => focusedSid ? wsRef.current?.sendDshControl(focusedSid, "command", value, images, files) ?? null : null}
          draftKey={focusedComposerDraftKey}
          draftStore={composerDraftsRef.current}
          surface={space}
          state={rt.state}
          catalog={focusedCatalog}
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
          autoCompact={rt.autoCompact}
          perm={rt.perm}
          permissionProfile={rt.permissionProfile}
          permissionProfilePending={!!permissionProfileRequests[
            JSON.stringify([machineId, space, focusedEngine, focusedSid])]}
          permissionProfiles={rt.permissionProfiles}
          webSearch={rt.webSearch}
          collaborationMode={rt.collaborationMode}
          fast={rt.fast}
          control={rt.control}
          external={rt.external}
          takeoverPending={rt.takeoverPending}
          takeoverMessage={rt.takeoverMessage}
          engine={focusedEngine}
          archived={focusedSession?.tag === "archived"}
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
          onSetAutoCompact={setAutoCompact}
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
            claudeProfileId: focusedClaudeProfileId ?? newChatClaudeProfileId,
            codexProfileId: focusedCodexProfileId ?? newChatCodexProfileId,
          })}
          onContext={requestContext}
          onOpenBtw={openBtw}
          onDiff={() => getDiff("")}
          onPreview={previewMarkdown}
          onOpenFiles={openFiles}
          codexContext={rt.codexContext}
          onSetCodexContext={focusedSid && space === "code" && focusedEngine === "codex"
            ? (maxTokens) => wsRef.current?.sendCodexContext(focusedSid, maxTokens) ?? false : undefined}
          onGoal={runGoal}
          onStatus={openStatus}
          onRefreshUsage={refreshStatus}
          onReview={(target, value) => {
            if (focusedSid) wsRef.current?.sendStartReview(focusedSid, target, value);
          }}
          onCompact={() => {
            if (focusedSid) {
              wsRef.current?.sendCompactSession(focusedSid, focusedEngine);
            }
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
              claudeProfileId: focusedClaudeProfileId,
              codexProfileId: focusedCodexProfileId,
              sid: focusedSid,
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
              claudeProfileId: focusedClaudeProfileId,
              codexProfileId: focusedCodexProfileId,
              sid: focusedSid,
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
          contextExactReport={rt.contextExactReport}
          contextLoading={rt.contextRequestId !== null}
          contextDeferred={rt.contextRefreshDeferred}
          contextError={rt.contextError}
          statusReport={rt.statusReport}
          rateLimits={rt.rateLimits}
          statusError={rt.statusError}
          statusLoading={rt.statusRequestId !== null}
        />
          </>
        )}
        {/* context usage now lives in the composer's ring popover (see Composer) */}
      </section>
      {dshPanel?.scope === activeScopeKey && <Suspense fallback={null}>
        <DshPanel key={`${dshPanel.sid}:${dshPanel.selection.target ?? ""}:${dshPanel.selection.query ?? ""}`}
          sid={dshPanel.sid} selection={dshPanel.selection} api={dshApi}
          state={state.runtimes[dshPanel.sid]?.dsh} onClose={() => setDshPanel(null)}
          onOpenFile={(path, sid, line) => {
            if (previewFileForSid(sid ?? dshPanel.sid, path, line)) setDshPanel(null);
          }} onOpenSession={sid => {
            const selected = state.sessions.find(session => session.session_id === sid);
            if (selected && confirmArtifactDiscard()) { focusListedSession(selected); setDshPanel(null); }
          }} />
      </Suspense>}
      {/* Share the layout's selection: retained hidden chats reserve no space. */}
      {(() => {
        if (visibleRightPanel === "files" && fileBrowser) {
          const closeFiles = () => {
            if (!confirmArtifactDiscard()) return;
            setFileBrowser(null);
            dispatch({ type: "clear_artifact" });
          };
          return <Suspense fallback={<div className="artifact-panel" role="status">加载目录…</div>}>
            <SessionFilesPanel key={fileBrowser.id} showingPreview={fileBrowser.preview}
              browser={{ sid: fileBrowser.sid, initialPath: fileBrowser.path, ws: wsRef.current,
                hidden: fileBrowser.preview, onListen: listenFiles, onClose: closeFiles,
                onOpenFile: (path) => {
                  if (previewFileForSid(fileBrowser.sid, path, undefined, true))
                    setFileBrowser({ ...fileBrowser, preview: true });
                } }}
              onBack={() => {
                if (!confirmArtifactDiscard()) return;
                dispatch({ type: "clear_artifact" });
                setFileBrowser({ ...fileBrowser, preview: false });
              }}
              preview={{ artifact: state.artifact, theme,
                onRefresh: (path, line) => { previewFileForSid(fileBrowser.sid, path, line, true); },
                onOpenFile: (path, line) => { previewFileForSid(fileBrowser.sid, path, line, true); },
                onLoadPreviewAsset: loadPreviewAsset, onAuthorizePreview: authorizePreview,
                onSaveMarkdown: saveMarkdown, onDirtyChange: setArtifactDirty, onClose: closeFiles }} />
          </Suspense>;
        }
        if (visibleRightPanel === "viewer" && visibleParentSid && viewerKey) {
          return <Suspense fallback={<div className="artifact-panel empty" role="status"><p>加载预览…</p></div>}>
            <RemoteViewerPanel key={`${viewerKey}:${viewerUrl?.key === viewerKey ? viewerUrl.openId : ""}`}
              scope={{ machineId, sid: visibleParentSid, space, engine }}
              selection={viewerSelections[viewerKey] ?? null}
              requestedUrl={viewerUrl?.key === viewerKey ? viewerUrl.href : undefined}
              devices={remoteDevices} onSelect={selectViewer} onClose={closeViewer} />
          </Suspense>;
        }
        if (visibleRightPanel === "agent" && agentPanel) {
          return <Suspense fallback={null}>
            <AgentDetailController
              key={`${agentPanel.sid}:${agentPanel.revision}:${agentPanel.runId}`}
              selection={agentPanel} ws={wsRef.current}
              onListen={setAgentDetailListener}
              onClose={() => setAgentPanel(null)}
              onOpenFile={previewAgentFile} />
          </Suspense>;
        }
        if (visibleRightPanel === "btw")
          return <Suspense fallback={null}>
            <BtwPanel sid={activeBtwSid ?? undefined} rt={activeBtwSid ? state.runtimes[activeBtwSid] : undefined}
            engine={activeBtw?.engine ?? focusedEngine} opening={btwOpening}
            chats={(activeBtwGroup?.chats ?? []).map((chat, index) => {
              const runtime = state.runtimes[chat.sid];
              const prompt = runtime?.turns.find(
                (turn) => turn.prompt.trim().length > 0)?.prompt.trim();
              return {
                ...chat,
                title: prompt ? prompt.slice(0, 36) : `侧聊 ${index + 1}`,
                state: runtime?.state ?? "idle",
                needsAnswer: !!runtime?.pendingQuestion,
              };
            })}
            active="btw" hasArtifact={!!state.artifact}
            artifactKind={state.artifact?.kind}
            onTab={switchRight}
            onNew={createBtw}
            onSelect={selectBtw}
            onCloseChat={closeBtw}
            catalog={focusedCatalog}
            draftKey={activeBtwDraftKey} draftStore={btwDraftsRef.current}
            sendMode={activeBtwSendMode}
            unconfirmedQueued={unconfirmedQueued}
            unconfirmedReplaceable={btwUnconfirmedReplaceable}
            queueCapacity={queueCapacity}
            replaceQueueCapacity={btwReplaceQueueCapacity}
            onSend={sendBtw}
            onSteer={steerBtw}
            onReplyAsyncQuestion={state.connState !== "connected" || !state.wrapperOnline
              ? undefined : (prompt) => {
                if (!activeBtwSid || stateRef.current.newChat
                    || stateRef.current.focusedSid !== visibleParentSid
                    || previousMachineRef.current !== machineId) return false;
                return replyAsyncQuestion(activeBtwSid, prompt, sendBtw, steerBtw);
              }}
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
            onSetAutoCompact={(selection) => activeBtwSid
              ? setBtwAutoCompact(activeBtwSid, selection) : false}
            onOpenFile={previewBtwFile} onCollapse={collapseBtw}
            imageAssets={btwInlineImageAssets}
            onLoadImage={loadBtwMessageImage}
            onAuthorizeImage={authorizeMessageImage}
            onAnswerQuestion={(askId, answer) => {
              if (!activeBtwSid) return;
              if (!wsRef.current?.sendAnswerQuestion(
                activeBtwSid, askId, answer)) return;
              dispatch({
                type: "answer_question",
                sid: activeBtwSid,
                ask_id: askId,
              });
            }}
            onDismissNotice={(noticeId) => {
              if (activeBtwSid) dispatch({ type: "dismiss_notice", sid: activeBtwSid, noticeId });
            }} />
          </Suspense>;
        if (visibleRightPanel === "diff" && state.artifact)
          return <Suspense fallback={
            <div className="artifact-panel empty" role="status">
              <div className="spinner" aria-hidden="true" />
              <p className="loading-tx">加载预览…</p>
            </div>
          }>
            <ArtifactPanel artifact={state.artifact} active="diff"
              hasBtw={!!activeBtwGroup}
              theme={theme}
              onTab={switchRight} onRefresh={previewArtifactFile}
              onOpenFile={previewArtifactFile} onLoadPreviewAsset={loadPreviewAsset}
              onAuthorizePreview={authorizePreview}
              onSaveMarkdown={saveMarkdown} onDirtyChange={setArtifactDirty}
              onClose={() => dispatch({ type: "clear_artifact" })} />
          </Suspense>;
        return null;
      })()}
      {queuedQueryEditor && <Suspense fallback={null}><QueuedQueryDialog
        key={queuedQueryEditor
          ? `${queuedQueryEditor.sid}:${queuedQueryEditor.msgId}`
          : "closed"}
        editor={queuedQueryEditor}
        onClose={() => {
          if (!queuedQueryEditor?.saving) setQueuedQueryEditor(null);
        }}
        onSave={updateQueuedQuery}
        onRetry={retryQueuedQuery} /></Suspense>}
      {rt.pendingQuestion && !activeBtwQuestionVisible && (
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
      {shouldOpenCodexStatus(statusOpenSid, focusedSid, focusedEngine)
        && <Suspense fallback={null}>
          <StatusSheet open report={rt.statusReport}
            notices={rt.notices}
            error={rt.statusError}
            resetCreditLoading={rt.statusRequestId !== null}
            resetCreditResult={rt.resetCreditResult}
            resetCreditDisabled={
              rt.state !== "idle"
              || rt.statusRequestId !== null
              || !!rt.statusError
              || state.connState !== "connected"
              || !state.wrapperOnline
            }
            onClose={() => setStatusOpenSid(null)}
            onRefresh={openStatus}
            onConsumeResetCredit={consumeResetCredit}
            onDismissNotice={(noticeId) => {
              if (focusedSid) dispatch({
                type: "dismiss_notice", sid: focusedSid, noticeId,
              });
            }} />
        </Suspense>}
      {usageActivityOpen && engine === "codex" && <Suspense fallback={null}>
        <UsageActivitySheet
          open
          report={rt.statusReport}
          error={rt.statusError}
          loading={rt.statusRequestId !== null}
          hasSession={!!focusedSid && focusedEngine === "codex" && !state.newChat}
          onClose={() => setUsageActivityOpen(false)}
          onRefresh={refreshStatus}
        />
      </Suspense>}
      {forkWorktreeSession !== null && <Suspense fallback={null}>
        <ForkWorktreeSheet open session={forkWorktreeSession}
          creating={forkWorktreeCreating} error={forkWorktreeError}
          onConfirm={submitForkWorktree} onClose={closeForkWorktree} />
      </Suspense>}
      <WorkDashboardSheet
        key={sessionScopeKey(machineId, engine, "work")}
        open={workManagerOpen && space === "work"}
        scopeKey={sessionScopeKey(machineId, engine, "work")}
        dashboard={activeWorkDashboard}
        claudeProfiles={state.claudeProfiles}
        defaultClaudeProfileId={state.defaultClaudeProfileId}
        claudeProfileId={state.newChat
          ? newChatClaudeProfileId : focusedClaudeProfileId}
        codexProfiles={state.codexProfiles}
        defaultCodexProfileId={state.defaultCodexProfileId}
        codexProfileId={state.newChat
          ? newChatCodexProfileId : focusedCodexProfileId}
        selectedProjectId={activeWorkProjectId}
        onSelectProject={setWorkProjectId}
        onClose={() => setWorkManagerOpen(false)}
        onCreateProject={(name, description) => !!wsRef.current?.sendCreateWorkProject(engine, name, description)}
        onDeleteProject={(projectId) => !!wsRef.current?.sendDeleteWorkProject(engine, projectId)}
        onAddSource={(projectId, kind, title, uri, file) => !!wsRef.current?.sendAddWorkSource(engine, projectId, kind, title, uri, file)}
        onDeleteSource={(sourceId) => !!wsRef.current?.sendDeleteWorkSource(engine, sourceId)}
        onCreateSchedule={(title, prompt, nextRunAt, repeatSeconds, projectId,
          profileId) => !!wsRef.current?.sendCreateWorkSchedule(
          engine, title, prompt, nextRunAt, repeatSeconds, projectId,
          engine === "codex" ? profileId : undefined,
          engine === "claude" ? profileId : undefined)}
        onDeleteSchedule={(scheduleId) => !!wsRef.current?.sendDeleteWorkSchedule(engine, scheduleId)}
        onCreatePlugin={(name, instructions, projectId) => !!wsRef.current?.sendCreateWorkPlugin(engine, name, instructions, projectId)}
        onDeletePlugin={(pluginId) => !!wsRef.current?.sendDeleteWorkPlugin(engine, pluginId)} />
      {workArtifactsOpen && space === "work" && !state.newChat
        && currentWorkArtifacts.length > 0 && <Suspense fallback={null}>
          <WorkArtifactsSheet open artifacts={currentWorkArtifacts}
            onOpen={(path) => {
              setWorkArtifactsOpen(false);
              previewFile(path);
            }}
            onClose={() => setWorkArtifactsOpen(false)} />
        </Suspense>}
      <Suspense fallback={null}>
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
            claudeProfileId: focusedClaudeProfileId,
            codexProfileId: focusedCodexProfileId,
            sid: focusedSid,
          }, true);
        }}
        onManagePlugin={(item, action) => {
          const verb = action === "install" ? "安装" : "卸载";
          if (!window.confirm(`${verb}插件「${item.name}」将修改本机 ${focusedEngine === "codex" ? "Codex" : "Claude"} 配置，确定继续吗？`)) return;
          setCapabilitiesLoading(true);
          const requestId = wsRef.current?.sendManageEnginePlugin(
            focusedEngine, space, action, item.id,
            capabilityCwd, focusedCodexProfileId, focusedClaudeProfileId);
          trackCapabilityMutation(requestId, {
            key: focusedSkillCatalogKey, engine: focusedEngine, space,
            cwd: capabilityCwd, skillsOnly: false,
            claudeProfileId: focusedClaudeProfileId,
            codexProfileId: focusedCodexProfileId,
          });
        }}
        onManageSkill={(item: EngineCapabilityItem, action) => {
          const labels = { enable: "启用", disable: "停用", remove: "删除" } as const;
          if (!window.confirm(`${labels[action]} Skill「${item.name}」？${action === "remove" ? "删除会移动到本机可恢复回收目录。" : ""}`)) return;
          setCapabilitiesLoading(true);
          const requestId = wsRef.current?.sendManageEngineSkill(
            focusedEngine, space, action, { skillId: item.id },
            capabilityCwd, focusedCodexProfileId, focusedClaudeProfileId);
          trackCapabilityMutation(requestId, {
            key: focusedSkillCatalogKey, engine: focusedEngine, space,
            cwd: capabilityCwd, skillsOnly: false,
            claudeProfileId: focusedClaudeProfileId,
            codexProfileId: focusedCodexProfileId,
          });
        }}
        onCreateSkill={(draft: SkillDraft) => {
          setCapabilitiesLoading(true);
          const requestId = wsRef.current?.sendManageEngineSkill(
            focusedEngine, space, "create", draft,
            capabilityCwd, focusedCodexProfileId, focusedClaudeProfileId);
          trackCapabilityMutation(requestId, {
            key: focusedSkillCatalogKey, engine: focusedEngine, space,
            cwd: capabilityCwd, skillsOnly: false,
            claudeProfileId: focusedClaudeProfileId,
            codexProfileId: focusedCodexProfileId,
          });
        }}
        onRemoveHook={(item: EngineCapabilityItem) => {
          if (!window.confirm(`删除 Hook「${item.name}」？配置文件中的其他内容会原样保留。`)) return;
          setCapabilitiesLoading(true);
          const requestId = wsRef.current?.sendManageEngineHook(
            focusedEngine, space, "remove", { hookId: item.id },
            capabilityCwd, focusedCodexProfileId, focusedClaudeProfileId);
          trackCapabilityMutation(requestId, {
            key: focusedSkillCatalogKey, engine: focusedEngine, space,
            cwd: capabilityCwd, skillsOnly: false,
            claudeProfileId: focusedClaudeProfileId,
            codexProfileId: focusedCodexProfileId,
          });
        }}
        onCreateHook={(draft: HookDraft) => {
          setCapabilitiesLoading(true);
          const requestId = wsRef.current?.sendManageEngineHook(
            focusedEngine, space, "create", draft,
            capabilityCwd, focusedCodexProfileId, focusedClaudeProfileId);
          trackCapabilityMutation(requestId, {
            key: focusedSkillCatalogKey, engine: focusedEngine, space,
            cwd: capabilityCwd, skillsOnly: false,
            claudeProfileId: focusedClaudeProfileId,
            codexProfileId: focusedCodexProfileId,
          });
        }}
        onClose={() => setCapabilitiesOpen(false)} />
      </Suspense>
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
    </RemoteViewerContext.Provider>
    </ViewerPagesProvider>
  );
}
