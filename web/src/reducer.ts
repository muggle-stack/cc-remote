// Turn/block state model for the chat UI.
//
// Multi-session: AppState holds a `runtimes` map keyed by session id (or a
// wrapper-assigned temp key for a brand-new session until its real id is
// captured). Each SessionRuntime has its own turns/state/model/perm/queue/etc.
// `focusedSid` selects the viewed one. Switching sessions is a pure view change
// (session_focus) — background turns keep streaming into their own runtime.
//
// Inbound frames carry `sid`; narrative events route to runtimes[msg.sid]
// (unknown sid → drop; null sid → focused). Control frames (session_list,
// session_focus, wrapper_reconnected, diff_report, ...) are global.
import type { ConnState, EventOwnership } from "./ws";
import type {
  ServerEvent, SessionInfo, State, ContextReport, StatusReport,
  RateLimitResetResult, ThreadGoal,
  QueryImg, QueryFile, DirEntry, AssistantChannel, ProcessStatus,
  CollaborationModeName, Notice, RateLimitUpdate,
  StatusRateLimit, StatusRateWindow, SessionControl, PermissionProfileInfo,
  PreviewAuthorizationOperation, ClaudeProfileInfo, CodexProfileInfo,
  CodexTerminalFence, AutoCompact, AutoCompactMode,
  BackgroundProcessItem, CodexContext,
} from "./protocol";
import type { SendMode } from "./composer-submit";
import {
  compareSessionControl, sessionControlLocksInput, sessionControlTargetsSid,
  MAX_BACKGROUND_PROCESS_ITEMS,
} from "./protocol";
import type { Catalog } from "./data";
import type { DiffLine, GitDiffSection } from "./diff";
import { parseGitDiff } from "./diff";
import { matchModelId } from "./data";
import {
  canEnqueueQuery,
  collectUnconfirmedQueries,
  MAX_QUEUED_QUERIES,
  MAX_QUEUED_QUERY_BYTES,
  queuedQueryWireBytes,
  reduceTargetedRuntime,
  type QueueCapacity,
} from "./runtime-drain";
import {
  historyContainsTurn, installAuthoritativeTurnDetailPage,
  mergeAuthoritativeTurnDetail, mergeDetailWithLiveTail, mergeInitialHistory,
  restoreCachedTurnDetails, restoreObservedLiveTurnDetails, restoreTurnInterruptionCauses,
} from "./history-merge";
import { reconcileBoundCompactionOrphanDetailed } from "./compaction-orphans.ts";
import {
  installTurnDetailProjectionPage,
} from "./history-detail-projection";
import {
  advanceHistoryRecovery,
  beginHistoryRecovery,
  historyConfirmsRecovery,
  historyConfirmsRuntimeRecovery,
  historyMatchesRecovery,
  historyMatchesRuntimeRecovery,
  historyNeedsConfirmationRequest,
  isHistoryRecoveryPending,
  isRuntimeHistoryRecoveryPending,
  type HistoryRecoveryProjection,
} from "./history-recovery";
import {
  appendNewerPage,
  canonicalTurnId,
  createHistoryBrowse,
  markBrowseDetail,
  markBrowseDetailLoading,
  markBrowseLatestDirty,
  markBrowseNewerUnavailable,
  prependOlderPage,
  settleBrowsePageRequest,
  type HistoryBrowsePage,
  type HistoryBrowseProjection,
} from "./history-browse";
import type { HistoryDetailRequestContext } from "./history-requests";
import { boundRuntimeTurns, pruneRuntimeMap } from "./runtime-bounds";
import { bumpSessionActivity, setSessionPinned } from "./session-order";
import { normalizeSessionList } from "./session-list";
import { codexTransportInterruption, presentCommandProblem, presentTurnProblem } from "./problem-presentation";
import {
  matchQueryAcceptanceHistory,
  queryAcceptanceDescriptor,
  type QueryAcceptanceHistoryHead,
} from "./outbox";
import {
  UNKNOWN_TERMINAL_ERROR,
  type Block,
  type ProcessBlock,
  type TextBlock,
  type ToolBlock,
  type Turn,
} from "./domain/conversation.ts";

const DETAIL_PARSE_ERROR = "过程解析失败";

export type {
  Block,
  ProcessBlock,
  TextBlock,
  ToolBlock,
  Turn,
  TurnDetailProjection,
  TurnDetailSegment,
} from "./domain/conversation";

/** A single running goal can emit an effectively unbounded number of distinct
 * app-server/SDK items.  Payload fields have their own byte limits, but without
 * an item-count limit every new tool/process/message still permanently grew the
 * active turn in the browser. */
export const MAX_TURN_BLOCKS = 256;
export const MAX_TURN_BLOCK_CHARS = 16 * 1024 * 1024;
export const OMITTED_PROCESS_ITEM_ID = "__cc_remote_earlier_process_omitted__";
const LIVE_SPILL_REFRESH_BLOCKS = 128;
const MAX_LIVE_SPILL_ARCHIVE_BLOCKS = 4_000;
const MAX_LIVE_SPILL_ARCHIVE_CHARS = 16 * 1024 * 1024;
// Notices are ephemeral UI control state, not transcript history.  Eight keeps
// simultaneous startup/config/security warnings available without allowing a
// noisy app-server to grow every resident session indefinitely.
export const MAX_SESSION_NOTICES = 8;

/** Account-sensitive model/default cache key. */
export function modelCatalogScopeKey(
  engine: string,
  profileId?: string | null,
): string {
  return (engine === "codex" || engine === "claude") && profileId
    ? `${engine}\u0000${profileId}`
    : engine;
}

export function nativeProfileSessionId(sessionId: string): string {
  const separator = sessionId.indexOf("@");
  return separator >= 0 ? sessionId.slice(separator + 1) : sessionId;
}

export interface PendingQuery {
  msg_id?: string;
  prompt: string;
  images?: QueryImg[];
  files?: QueryFile[];
  imageCount?: number;
  fileCount?: number;
  queueKind?: "queue" | "replace";
  queueState?: "submitting" | "queued" | "failed";
  retainedBytes?: number;
  replacesRetainedBytes?: number;
  queueError?: string;
  failedAt?: number;
}

export interface PreviewAssetState {
  requestId?: string;
  previewId?: string;
  loading?: boolean;
  mediaType?: string;
  data?: string;
  error?: string;
  authorization?: PreviewAuthorizationState;
}

export interface PreviewAuthorizationState {
  authorizationId: string;
  requestId: string;
  operation: PreviewAuthorizationOperation;
  path: string;
  resolvedPath: string;
  format: "markdown" | "text" | "html" | "image" | "pdf" | "audio";
  previewId?: string;
  status: "required" | "submitting" | "granted";
}

export interface Artifact {
  file: string;
  kind: "diff" | "md" | "file" | "gitdiff" | "html" | "image" | "pdf" | "audio";
  sid?: string | null;
  requestId?: string;
  diff?: DiffLine[];
  content?: string;
  data?: string;
  mediaType?: string;
  convertedFrom?: string;
  sections?: GitDiffSection[];
  loading?: boolean;
  size?: number;
  truncated?: boolean;
  mtimeNs?: string;
  revision?: string;
  writable?: boolean;
  saveRequestId?: string;
  saving?: boolean;
  saveStatus?: "saved" | "conflict" | "error";
  saveError?: string;
  pendingContent?: string;
  line?: number;
  error?: string;
  assets?: Record<string, PreviewAssetState>;
  authorization?: PreviewAuthorizationState;
}

export interface SessionRuntime {
  turns: Turn[];
  state: State;
  // Display-only activity observed from a native/external client. It must not
  // grant Stop/Interrupt semantics to a turn this wrapper does not own.
  mirroredRunning: boolean;
  /** Claude-owned detached work, replaced authoritatively on Hello/reconnect. */
  backgroundProcesses: ProcessBlock[];
  /** True only for an explicit empty level, never inferred from terminal edges. */
  backgroundLevelEmpty: boolean;
  /** Source time of the last authoritative level, used to settle cached detail. */
  backgroundLevelTs?: number;
  model: string;
  effort: string;
  autoCompact: AutoCompact | null;
  codexContext: CodexContext | null;
  perm: string;
  permissionProfile: string | null;
  permissionProfiles: PermissionProfileInfo[] | null;
  webSearch: "cached" | "live" | null;
  collaborationMode: CollaborationModeName;
  fast: boolean | null;   // null until the wrapper reports the real service tier
  replaying: boolean;
  // True only after this connection has received this sid's Snapshot or
  // ReplayEnd. Prevents stale local "idle" state from draining work early.
  syncReady: boolean;
  truncated: boolean;
  // A replayable marker announced a destructive transcript rewrite. Until a
  // fresh non-pagination History arrives, never hydrate/merge an older tail.
  historyInvalidated: boolean;
  // Revision attached to the last authoritative non-pagination History and,
  // while a rollback barrier is pending, the exact revision allowed to clear it.
  historyRevision: string | null;
  historyContinuityRevision?: string | null;
  pendingHistoryRevision: string | null;
  // Ordering watermark for newest-page History builds within one wrapper
  // generation. Pagination never advances it.
  historyGeneration: string | null;
  // Wrapper generation whose replay gap is awaiting a first authoritative
  // History page. This is metadata only; the single display copy lives on
  // AppState so background sessions never duplicate their bounded transcripts.
  pendingHistoryGeneration: string | null;
  // First matching newest-page build observed after a replay gap. Unlike the
  // focused display projection this lightweight barrier survives navigation,
  // so a background/returned session still requires a strictly newer build.
  pendingHistoryCandidateBuildSeq: number | null;
  historyBuildSeq: number;
  historyLiveSeq: number;
  // Sequence fence captured by the newest authoritative History head. An
  // explicit TurnSteered owner may supersede that head only when its own
  // downstream sequence is strictly newer. null means the producer supplied
  // no ordering proof, so the exact History head remains authoritative.
  historyFence: number | null;
  // Latest ordered live-row owner and the downstream sequence which established
  // it. This stays runtime-only and is never persisted as conversation content.
  liveOwner: { turnId: string; seq: number } | null;
  // Exact logical/native owner announced by TurnBinding but not yet consumed by
  // a narrative frame. Keeping this runtime-only prevents a wrapper restart
  // from manufacturing a second prompt-less row while preserving the sequence
  // and generation fences which make delayed replay safe.
  pendingLiveBinding: {
    msgId: string;
    turnId: string;
    seq: number;
    generation: string | null;
  } | null;
  // Source/revision-bound Codex terminals which arrived before their exact
  // native turn identity was present in the current narrative projection.
  // These never use array position or the generic "last open turn" fallback.
  pendingTerminalFences: {
    revision: string;
    generation: string | null;
    fences: CodexTerminalFence[];
  } | null;
  // A wrapper restart invalidated every unscoped liveTaskId fallback still
  // present in the visible projection. New-generation events establish an
  // ordered owner; old legacy identities remain ineligible for this generation.
  legacyLiveFallbackBlocked: boolean;
  // A browser-triggered older-page response has been installed for the current
  // history revision/generation. Subsequent newest-page refreshes are only a
  // moving head window and must not discard those already-loaded older pages.
  // This is deliberately distinct from IndexedDB hydration: an authoritative
  // first page must still be able to replace stale cached rows.
  hasLoadedOlderHistory: boolean;
  // Greatest downstream sequence that confirmed a turn on this connection.
  // A History captured before it may merge rows but cannot delete the live tail.
  lastLiveSeq: number;
  // Greatest direct lifecycle state sequence. A positive running bit captured
  // by older History may recover from unrelated live settings races, but must
  // never overwrite a newer explicit idle state.
  lastLifecycleSeq: number;
  // IDs painted from IndexedDB before authoritative History arrives. They are
  // not a genuine live tail, even when an old cache row happens to be marked
  // unfinished (for example a tab closed halfway through streaming).
  hydratedCacheTurnIds: string[];
  // Completed turns whose heavyweight process was painted from this
  // connection's live stream. A same-revision summary may omit those blocks;
  // this bounded, memory-only identity list preserves only that observed
  // detail without retaining arbitrary completed transcript rows.
  liveDetailTurnIds: string[];
  // Native newest turn id from the last authoritative first History page. A
  // query freezes this together with revision/build/live watermarks so a later
  // materialized page can prove acceptance even when its UserMsg id is native.
  historyNewestId: string | null;
  // Whether the current revision/generation has supplied an authoritative
  // newest page. Together with `loading` this distinguishes unknown/loading/
  // ready without treating the default `hasMore=false` as proof that a cold
  // session is already at the beginning of history.
  historyHeadKnown: boolean;
  // true while we've switched to a session but its history hasn't arrived yet
  // (no cache hit + waiting on the wrapper's cold spawn/replay) — drives a spinner.
  loading?: boolean;
  // pagination: older turns exist beyond what's loaded, and the oldest loaded
  // turn id — the cursor the "load more" button pages back from.
  hasMore?: boolean;
  oldestId?: string | null;
  // v15 authoritative control. Once populated, legacy external/takeover frames
  // are ignored; revision ordering owns every subsequent write-state decision.
  control: SessionControl | null;
  // Wrapper generation that owns control.revision. Kept even when an epoch
  // switch clears control so a delayed old-generation direct event is rejected.
  controlGeneration: string | null;
  // Sticky within this browser runtime: once revisioned control has been
  // accepted, unrevisioned compatibility frames can never regain authority,
  // including during the short gap between a generation switch and its seed.
  hasRevisionedControl: boolean;
  // Legacy compatibility/derived lock consumed by queue draining. When v15
  // control exists this is derived from it, never independently authoritative.
  external?: boolean;
  takeoverPending: boolean;
  takeoverMessage: string | null;
  ccSessionId?: string;
  pendingQuestion: { ask_id: string; header?: string | null; question: string; options: { label: string; ds?: string }[]; allow_text?: boolean; secret?: boolean; multi_select?: boolean } | null;
  contextReport: ContextReport | null;
  contextExactReport: ContextReport | null;
  contextRequestId: string | null;
  contextRefreshDeferred: boolean;
  contextError: string | null;
  goal: ThreadGoal | null;
  goalId: string | null;
  goalDismissed: boolean;
  completion: {
    id: string | null;
    unread: boolean;
    revision: number;
  } | null;
  statusReport: StatusReport | null;
  // Engine-neutral quota projection. Codex seeds it from StatusReport and
  // sparse app-server events; Claude can populate it from SDK events without
  // fabricating a Codex-only status snapshot.
  rateLimits: StatusRateLimit[];
  statusRequestId: string | null;
  statusError: string | null;
  resetCreditResult: RateLimitResetResult | null;
  notices: Notice[];
  // Busy-send choice belongs to this session's composer. A choice made while
  // reading one Codex task must not turn another session away from the default
  // native steer behavior.
  sendMode: SendMode;
  queue: PendingQuery[];
  pendingSend: PendingQuery | null;
  // Rejected reliable submissions retain their complete browser payload until
  // the user retries or dismisses them.  This collection is globally bounded
  // by the same 32-item / 64-MiB budget as the wrapper queue.
  failedDeferred: PendingQuery[];
  // Browser query accepted into the reliable outbox but not yet confirmed by
  // its exact user echo / native turn binding / correlated terminal Error.
  acceptancePending: string | null;
  acceptanceKind: "query" | "steer" | "steer_unknown" | null;
  acceptanceHistoryBaseline: QueryAcceptanceHistoryHead | null;
}

export interface AppState {
  // connection / global UI
  connState: ConnState;
  wrapperOnline: boolean;
  banner?: string;
  artifact: Artifact | null;
  dirPicker: {
    path: string;
    parent: string | null;
    dirs: DirEntry[];
    requestId: string | null;
  } | null;
  cwdByScope: Record<string, string>;
  // new-chat welcome page (global; only one new-chat flow at a time). model/effort
  // are the pre-selected values (null = use the wrapper's engine default).
  newChat: {
    cwd: string;
    cwdSource: "default" | "inherited" | "explicit";
    model: string | null;
    effort: string | null;
    autoCompactMode: AutoCompactMode;
    autoCompactThresholdTokens: number | null;
    claudeProfileId: string | null;
    codexProfileId: string | null;
  } | null;
  // Public labels/errors only; CLAUDE_CONFIG_DIR never crosses the wire.
  claudeProfiles: ClaudeProfileInfo[];
  defaultClaudeProfileId: string | null;
  claudeProfileByScope: Record<string, string>;
  // Public labels/errors only; CODEX_HOME never crosses the wire. Selection is
  // scoped like cwd so Code accounts cannot leak across devices or surfaces.
  codexProfiles: CodexProfileInfo[];
  defaultCodexProfileId: string | null;
  codexProfileByScope: Record<string, string>;
  // sessions + multi-session runtimes
  sessions: SessionInfo[];
  focusedSid: string | null;
  runtimes: Record<string, SessionRuntime>;
  // Exact wrapper-global queue accounting. Prompt previews cannot be used for
  // this because they omit attachment bodies and all text after 512 chars.
  queryQueueCount: number;
  queryQueueBytes: number;
  // At most one current, display-only transcript projection survives a replay
  // gap. It never participates in control, queue draining, or cache writes.
  historyRecovery: HistoryRecoveryProjection | null;
  // Explicit older-history browsing is a focused, display-only projection.
  // Live turns, control, queue draining and query acceptance remain owned by
  // the per-session runtime even while this older window is visible.
  historyBrowse: HistoryBrowseProjection | null;
  // A disconnected/rebuilding browse window remains paintable but owns no
  // request authority. Only a matching authoritative head may reactivate it.
  retainedHistoryBrowse: HistoryBrowseProjection | null;
  // /btw ephemeral side-forks are owned by their parent sessions. A parent can
  // keep several resident chats; exactly one is selected in the side panel.
  btwByParentSid: Record<string, BtwGroup>;
  // Orders the authoritative reconnect catalog and subsequent open/close
  // mutations. It is scoped to one Wrapper generation (which resets state).
  btwRevision: number;
  // Model catalogs the engine reported (currently Codex only). Claude still sends
  // an empty catalog plus its cwd-aware defaults; data.ts keeps the static list.
  catalog: Catalog;
  // engine -> the model a NEW no-override session starts on.
  // Never the focused session's model — that one is per-session.
  catalogDefault: Record<string, string>;
  // engine -> effective reasoning strength for a no-override NEW session.
  catalogDefaultEffort: Record<string, string>;
  // engine -> cwd those defaults were resolved for. Claude defaults are only
  // rendered when this still matches the new-chat form's directory.
  catalogDefaultCwd: Record<string, string>;
}

export interface BtwChat {
  sid: string;
  engine: "claude" | "codex";
  createdAt: number;
  state: "idle" | "running" | "interrupting" | "draining";
}

export interface BtwGroup {
  chats: BtwChat[];
  activeSid: string;
}

export function createRuntime(): SessionRuntime {
  return {
    // These are authoritative engine settings.  A newly-created browser runtime
    // has not heard them yet, so keep them unknown instead of briefly claiming a
    // model, effort, or permission policy that may not match the native CLI.
    turns: [], state: "idle", mirroredRunning: false,
    backgroundProcesses: [],
    backgroundLevelEmpty: false,
    backgroundLevelTs: undefined,
    model: "", effort: "", autoCompact: null, codexContext: null, perm: "",
    permissionProfile: null, permissionProfiles: null, webSearch: null,
    collaborationMode: "default",
    fast: null,
    control: null, controlGeneration: null, hasRevisionedControl: false,
    takeoverPending: false, takeoverMessage: null,
    replaying: false, syncReady: false, truncated: false,
    historyInvalidated: false,
    historyRevision: null, pendingHistoryRevision: null,
    historyContinuityRevision: null,
    historyGeneration: null, pendingHistoryGeneration: null,
    pendingHistoryCandidateBuildSeq: null,
    historyBuildSeq: 0, historyLiveSeq: 0,
    historyFence: null, liveOwner: null, pendingLiveBinding: null,
    pendingTerminalFences: null,
    legacyLiveFallbackBlocked: false,
    lastLiveSeq: 0, lastLifecycleSeq: 0,
    hasLoadedOlderHistory: false,
    hydratedCacheTurnIds: [],
    liveDetailTurnIds: [],
    historyNewestId: null,
    historyHeadKnown: false,
    pendingQuestion: null, contextReport: null, contextExactReport: null,
    contextRequestId: null, contextRefreshDeferred: false,
    contextError: null, goal: null,
    goalId: null, goalDismissed: false, completion: null,
    statusReport: null, rateLimits: [], statusRequestId: null, statusError: null,
    resetCreditResult: null,
    notices: [], sendMode: "steer",
    queue: [], pendingSend: null, failedDeferred: [],
    acceptancePending: null,
    acceptanceKind: null,
    acceptanceHistoryBaseline: null,
  };
}

export type Action =
  | { type: "reset" }
  | { type: "event"; event: ServerEvent; ownership?: EventOwnership }
  | { type: "query_sent"; sid: string; prompt: string; msg_id: string; images?: QueryImg[]; files?: QueryFile[]; ts: number }
  | { type: "steer_sent"; sid: string; prompt: string; msg_id: string; images?: QueryImg[]; files?: QueryFile[]; ts: number }
  | { type: "conn"; connState: ConnState; detail?: string }
  | { type: "command_error"; detail: string }
  | { type: "dismiss_banner"; banner: string }
  | { type: "enqueue"; sid?: string; query: PendingQuery }
  | { type: "dequeue_at"; sid: string; i: number }
  | { type: "set_send_mode"; sid: string; mode: SendMode }
  | { type: "set_pending"; sid?: string; query: PendingQuery }
  | { type: "clear_pending"; sid: string }
  | { type: "remove_deferred"; sid: string; msgId: string }
  | { type: "update_failed_deferred"; sid: string; msgId: string; prompt: string }
  | { type: "set_model"; model: string }
  | { type: "set_effort"; effort: string }
  | { type: "set_perm"; perm: string }
  | { type: "set_collaboration_mode"; mode: CollaborationModeName }
  | { type: "set_context"; report: ContextReport }
  | { type: "clear_context" }
  | { type: "begin_context_request"; sid: string; requestId: string }
  | { type: "defer_context_request"; sid: string }
  | { type: "begin_status_request"; sid: string; requestId: string }
  | { type: "set_turns"; sid: string; turns: Turn[] }
  | { type: "set_artifact"; artifact: Artifact }
  | { type: "open_artifact_loading"; file: string; sid: string | null; requestId: string }
  | { type: "open_file_loading"; file: string; sid: string | null; requestId: string; kind: "md" | "file"; line?: number }
  | { type: "begin_preview_asset"; sid: string; path: string; previewId: string; requestId: string }
  | { type: "submit_preview_authorization"; sid: string; authorizationId: string; requestId: string }
  | { type: "preview_authorization_retry_started"; sid: string; authorizationId: string; requestId: string }
  | { type: "preview_authorization_retry_failed"; sid: string; authorizationId: string; requestId: string }
  | { type: "start_file_save"; requestId: string; content: string }
  | { type: "clear_artifact" }
  | { type: "select_btw"; parentSid: string; btwSid: string }
  | { type: "clear_btw"; parentSid: string; btwSid: string }
  | { type: "clear_all_btw" }
  | { type: "clear_session_list" }
  | { type: "restore_session_list"; sessions: SessionInfo[] }
  | { type: "drop_fork_placeholder"; sid: string; parentSid: string }
  | { type: "set_session_pinned"; sid: string; pinned: boolean }
  | { type: "focus_session"; sid: string }
  | { type: "turn_detail_requested"; sid: string; turnId: string; before?: string | null; autoLoad?: boolean }
  | { type: "begin_history_browse"; sid: string; scopeKey: string; revision: string; generation?: string | null; viewId: string; basePageKey: string }
  | { type: "install_history_browse_page"; sid: string; scopeKey: string; revision: string; generation?: string | null; viewId: string; windowEpoch: number; before: string; page: HistoryBrowsePage; protectedTurnIds?: string[]; prepared?: { from: HistoryBrowseProjection; to: HistoryBrowseProjection } }
  | { type: "install_history_browse_newer"; sid: string; scopeKey: string; revision: string; generation?: string | null; viewId: string; windowEpoch: number; page: HistoryBrowsePage; protectedTurnIds?: string[]; prepared?: { from: HistoryBrowseProjection; to: HistoryBrowseProjection } }
  | { type: "history_browse_newer_settled"; sid: string; scopeKey: string; revision: string; generation?: string | null; viewId: string; windowEpoch: number; pageKey: string }
  | { type: "history_browse_newer_unavailable"; sid: string; scopeKey: string; revision: string; generation?: string | null; viewId: string; windowEpoch: number }
  | { type: "history_browse_page_failed"; sid: string; scopeKey: string; revision: string; generation?: string | null; viewId: string; windowEpoch: number; before: string }
  | { type: "history_browse_detail_requested"; sid: string; scopeKey: string; revision: string; viewId: string; windowEpoch: number; turnId: string; before?: string | null }
  | { type: "history_browse_detail"; sid: string; scopeKey: string; revision: string; viewId: string; windowEpoch: number; turnId: string; events: ServerEvent[]; error?: string | null; resetRequired?: boolean; before?: string | null; hasMore?: boolean; oldestCursor?: string | null; hasNewer?: boolean; newerCursor?: string | null }
  | { type: "history_detail_reset_requested"; context: HistoryDetailRequestContext }
  | { type: "history_detail_cancelled"; context: HistoryDetailRequestContext }
  | { type: "return_to_latest"; sid: string }
  | { type: "hydrate_cache"; sid: string; turns: Turn[]; revision: string | null; generation?: string | null; control?: SessionControl | null; historyAtStart?: boolean }
  | { type: "prune_runtimes"; protectedSids: string[] }
  | { type: "answer_question"; sid: string; ask_id: string }
  | { type: "dismiss_notice"; sid: string; noticeId: string }
  | { type: "enter_new_chat"; cwd: string; cwdSource?: "default" | "inherited" | "explicit"; model?: string | null; effort?: string | null; autoCompactMode?: AutoCompactMode; autoCompactThresholdTokens?: number | null; claudeProfileId?: string | null; codexProfileId?: string | null }
  | { type: "set_new_chat_cwd"; cwd: string; cwdSource?: "default" | "inherited" | "explicit" }
  | { type: "set_new_chat_codex_profile"; scopeKey: string; profileId: string }
  | { type: "set_new_chat_claude_profile"; scopeKey: string; profileId: string }
  | { type: "clear_scope_cwd"; scopeKey: string }
  | { type: "set_new_chat_model"; model: string | null }
  | { type: "set_new_chat_effort"; effort: string | null }
  | { type: "set_new_chat_auto_compact"; mode: AutoCompactMode; thresholdTokens: number | null }
  | { type: "set_new_chat_selection"; model: string | null; effort: string | null }
  | { type: "exit_new_chat" };

export const initialState: AppState = {
  connState: "connecting",
  // Require a wrapper-originated frame before draining queued work. A relay
  // socket can be connected while the machine-side wrapper is still offline.
  wrapperOnline: false,
  artifact: null,
  dirPicker: null,
  cwdByScope: {},
  newChat: null,
  claudeProfiles: [],
  defaultClaudeProfileId: null,
  claudeProfileByScope: {},
  codexProfiles: [],
  defaultCodexProfileId: null,
  codexProfileByScope: {},
  sessions: [],
  focusedSid: null,
  runtimes: {},
  queryQueueCount: 0,
  queryQueueBytes: 0,
  historyRecovery: null,
  historyBrowse: null,
  retainedHistoryBrowse: null,
  btwByParentSid: {},
  btwRevision: 0,
  catalog: {},
  catalogDefault: {},
  catalogDefaultEffort: {},
  catalogDefaultCwd: {},
};

export function deferredQueueCapacity(
  state: Pick<
    AppState, "queryQueueCount" | "queryQueueBytes" | "runtimes"
  >,
  replacingSid?: string | null,
): QueueCapacity {
  const pending = replacingSid
    ? state.runtimes[replacingSid]?.pendingSend : null;
  const replacingBytes = pending?.queueState === "queued"
    ? pending.retainedBytes ?? 0
    : pending?.queueState === "submitting"
      ? pending.replacesRetainedBytes ?? 0
      : 0;
  return {
    authoritativeCount: state.queryQueueCount,
    authoritativeBytes: state.queryQueueBytes,
    replacingCount: replacingBytes > 0 ? 1 : 0,
    replacingBytes,
  };
}

function boundFailedDeferred(
  runtimes: Record<string, SessionRuntime>,
): Record<string, SessionRuntime> {
  const retained = Object.entries(runtimes).flatMap(([sid, runtime]) =>
    runtime.failedDeferred.map((query) => ({ sid, query })));
  let bytes = retained.reduce(
    (total, entry) => total + queuedQueryWireBytes(entry.query), 0);
  if (
    retained.length <= MAX_QUEUED_QUERIES
    && bytes <= MAX_QUEUED_QUERY_BYTES
  ) return runtimes;

  retained.sort((left, right) =>
    (left.query.failedAt ?? 0) - (right.query.failedAt ?? 0));
  const remove = new Map<string, Set<string>>();
  while (
    retained.length > MAX_QUEUED_QUERIES
    || (bytes > MAX_QUEUED_QUERY_BYTES && retained.length > 1)
  ) {
    const oldest = retained.shift();
    if (!oldest) break;
    bytes -= queuedQueryWireBytes(oldest.query);
    if (!oldest.query.msg_id) continue;
    const ids = remove.get(oldest.sid) ?? new Set<string>();
    ids.add(oldest.query.msg_id);
    remove.set(oldest.sid, ids);
  }
  if (!remove.size) return runtimes;
  const next = { ...runtimes };
  for (const [sid, ids] of remove) {
    const runtime = next[sid];
    if (!runtime) continue;
    next[sid] = {
      ...runtime,
      failedDeferred: runtime.failedDeferred.filter(
        (query) => !query.msg_id || !ids.has(query.msg_id)),
    };
  }
  return next;
}

function cloneTurns(turns: Turn[]): Turn[] {
  return turns.map((t) => ({
    ...t,
    blocks: t.blocks.map((b) => ({ ...b })),
    liveSpillBlocks: t.liveSpillBlocks?.map((b) => ({ ...b })),
    detailProjection: t.detailProjection
      ? {
          ...t.detailProjection,
          blocks: t.detailProjection.blocks.map((block) => ({ ...block })),
        }
      : undefined,
  }));
}

function mutableTurnBlocks(turn: Turn): Block[] {
  return turn.liveSpillBlocks?.length
    ? [...turn.liveSpillBlocks, ...turn.blocks]
    : turn.blocks;
}

function openTurn(turns: Turn[], fallbackId: string, ts?: number): Turn {
  let turn = turns[turns.length - 1];
  if (!turn || turn.done) {
    turn = { id: fallbackId, prompt: "", blocks: [], done: false, ts };
    turns.push(turn);
  }
  return turn;
}

function allocateLiveOrder(turn: Turn): number {
  let next = turn.nextLiveBlockOrder;
  if (next == null) {
    next = turn.blocks.reduce(
      (maximum, candidate) => Math.max(maximum, candidate.liveOrder ?? -1),
      -1,
    ) + 1;
  }
  turn.nextLiveBlockOrder = next + 1;
  return next;
}

function appendLiveBlock<T extends Block>(turn: Turn, block: T): T {
  if (block.liveOrder == null) block.liveOrder = allocateLiveOrder(turn);
  turn.blocks.push(block);
  return block;
}

function findTurnOwningMessage(
  turns: Turn[], messageId: string | null | undefined,
): Turn | undefined {
  if (!messageId) return undefined;
  return [...turns].reverse().find((turn) => mutableTurnBlocks(turn).some((block) =>
    block.kind === "text"
      ? block.message_id === messageId
      : block.kind === "tool"
        ? block.message_id === messageId
        : false));
}

function turnIdentityAliases(
  turn: Pick<Turn, "id" | "clientMsgId" | "historyTurnId">,
): string[] {
  return [turn.id, turn.clientMsgId, turn.historyTurnId]
    .filter((value): value is string => !!value);
}

function turnHasIdentityAlias(
  turn: Pick<Turn, "id" | "clientMsgId" | "historyTurnId">,
  id: string | null | undefined,
): boolean {
  return !!id && turnIdentityAliases(turn).includes(id);
}

function turnsShareIdentityAlias(
  first: Pick<Turn, "id" | "clientMsgId" | "historyTurnId">,
  second: Pick<Turn, "id" | "clientMsgId" | "historyTurnId">,
): boolean {
  const firstAliases = new Set(turnIdentityAliases(first));
  return turnIdentityAliases(second).some((alias) =>
    firstAliases.has(alias));
}

function reconcileBoundCompactionOrphan(
  runtime: SessionRuntime,
  turns: Turn[],
  msgIds: readonly (string | null | undefined)[],
  nativeTurnId: string | readonly string[] | null | undefined,
): Turn[] {
  if (!nativeTurnId || (Array.isArray(nativeTurnId)
      && nativeTurnId.length === 0)) return turns;
  const aliases = msgIds.filter((value): value is string => !!value);
  const reconciliation = reconcileBoundCompactionOrphanDetailed(
    turns, aliases, nativeTurnId);
  const repaired = reconciliation.turns;
  if (repaired.length === turns.length) return turns;
  const owner = reconciliation.owner;
  const orphan = reconciliation.orphan;
  if (!owner || !orphan) return turns;
  const orphanAliases = new Set(turnIdentityAliases(orphan));
  // liveOwner stores an identity, not an object reference. If a distinct
  // retained row shares the orphan's display id, that id is still a valid
  // owner and must not be moved merely because the removed object used it too.
  // A later ordered binding can supersede it normally. Transfer only when the
  // identity disappeared with the exact orphan object.
  if (runtime.liveOwner && !repaired.some((turn) =>
    turnHasIdentityAlias(turn, runtime.liveOwner!.turnId))) {
    runtime.liveOwner = { ...runtime.liveOwner, turnId: owner.id };
  }
  const hydratedIds = runtime.hydratedCacheTurnIds.flatMap((id) => {
    const retained = repaired.some((turn) => turnHasIdentityAlias(turn, id));
    // Hydrated ids gate provisional detail restoration just like live-detail
    // ids below. Transfer the exact removed orphan's cache provenance to its
    // surviving owner, while preserving a distinct row whose display id only
    // happens to collide with that orphan.
    if (!orphanAliases.has(id)) return retained ? [id] : [];
    return retained ? [id, owner.id] : [owner.id];
  });
  runtime.hydratedCacheTurnIds = [...new Set(hydratedIds)].filter((id) =>
    repaired.some((turn) => turnHasIdentityAlias(turn, id)));
  const detailIds = runtime.liveDetailTurnIds.flatMap((id) => {
    const retained = repaired.some((turn) => turnHasIdentityAlias(turn, id));
    // Display ids can collide across cache migrations. Preserve a still-valid
    // colliding id, but also transfer the removed live orphan's observation to
    // its exact surviving owner instead of trying to infer deletion by id.
    if (!orphanAliases.has(id)) return retained ? [id] : [];
    return retained ? [id, owner.id] : [owner.id];
  });
  runtime.liveDetailTurnIds = [...new Set(detailIds)].filter((id) =>
    repaired.some((turn) => turnHasIdentityAlias(turn, id)));
  return repaired;
}

function pendingOptimisticSteerIndex(
  runtime: SessionRuntime, turns: Turn[],
): number {
  if ((runtime.acceptanceKind !== "steer"
      && runtime.acceptanceKind !== "steer_unknown")
      || !runtime.acceptancePending) return -1;
  return turns.findIndex((turn) =>
    turn.id === runtime.acceptancePending
    && turn.clientMsgId === runtime.acceptancePending
    && !turn.liveTaskId);
}

function preSteerTurn(
  runtime: SessionRuntime, turns: Turn[],
): Turn | undefined {
  const pendingIndex = pendingOptimisticSteerIndex(runtime, turns);
  if (pendingIndex < 0) return undefined;
  return [...turns.slice(0, pendingIndex)]
    .reverse().find((turn) => !turn.done);
}

function eventTimestampMs(ts: number | null | undefined): number | undefined {
  return typeof ts === "number" ? Math.round(ts * 1000) : undefined;
}

function findTurnByEngineId(turns: Turn[], id: string | null | undefined): Turn | undefined {
  if (!id) return undefined;
  return [...turns].reverse().find((turn) =>
    turnHasIdentityAlias(turn, id) || turn.liveTaskId === id
    || turn.forkPointId === id || turn.codexTurnId === id
    || mutableTurnBlocks(turn).some((block) => block.kind === "process"
      && block.turn_id === id));
}

function findMessageEventOwner(
  turns: Turn[], messageId: string, turnId: string | null | undefined,
): Turn | undefined {
  const owner = findTurnOwningMessage(turns, messageId);
  // A delayed item/completed or replay delta can name the same native task as
  // a newer steer. Its immutable item id is the stronger ownership evidence.
  if (owner) {
    if (!turnId || findTurnByEngineId([owner], turnId)) return owner;
    // The accepted steer deliberately retires its predecessor's live task and
    // fork aliases. Retiring those controls must not retire item ownership.
    if (!owner.liveTaskId && !owner.forkPointId && !owner.codexTurnId) return owner;
    // Conflicting explicit ownership is not permission to copy an existing
    // immutable item into another row.
    return undefined;
  }
  return findTurnByEngineId(turns, turnId);
}

function boundedTerminalFences(
  values: readonly CodexTerminalFence[],
): CodexTerminalFence[] {
  const byTurn = new Map<string, CodexTerminalFence>();
  for (const fence of values) {
    byTurn.delete(fence.turn_id);
    byTurn.set(fence.turn_id, fence);
    if (byTurn.size > 16) {
      const oldest = byTurn.keys().next().value;
      if (oldest !== undefined) byTurn.delete(oldest);
    }
  }
  return [...byTurn.values()];
}

function turnHasExactNativeTerminalId(turn: Turn, turnId: string): boolean {
  return turn.id === turnId
    || turn.forkPointId === turnId
    || turn.liveTaskId === turnId
    || turn.codexTurnId === turnId
    || mutableTurnBlocks(turn).some((block) =>
      block.kind === "process" && block.turn_id === turnId);
}

function applyTerminalFence(turn: Turn, fence: CodexTerminalFence): Turn {
  const next: Turn = {
    ...turn,
    blocks: turn.blocks.map((block) => ({ ...block })),
    liveSpillBlocks: turn.liveSpillBlocks?.map((block) => ({ ...block })),
    detailProjection: turn.detailProjection
      ? {
          ...turn.detailProjection,
          blocks: turn.detailProjection.blocks.map((block) => ({ ...block })),
        }
      : undefined,
    done: true,
    progress: undefined,
  };
  if (typeof fence.duration_ms === "number" && fence.duration_ms > 0) {
    next.durationMs = fence.duration_ms;
  }
  if (typeof fence.completed_at === "number"
      && Number.isFinite(fence.completed_at)
      && fence.completed_at >= 0
      && fence.completed_at <= Math.floor(Number.MAX_SAFE_INTEGER / 1000)) {
    next.doneTs = Math.round(fence.completed_at * 1000);
  }
  if (fence.status === "completed") {
    next.interrupted = undefined;
    next.error = undefined;
    delete next.terminalSource;
    finishOpenBlocks(next, "succeeded", false);
  } else if (fence.status === "interrupted") {
    next.interrupted = true;
    next.error = codexTransportInterruption(turn.error) ? turn.error : undefined;
    next.terminalSource = "unexpected_interrupt";
    finishOpenBlocks(next, "interrupted", true);
  } else {
    next.interrupted = undefined;
    next.error ??= "本次回复未完成，请重试。";
    next.terminalSource = "failed";
    finishOpenBlocks(next, "failed", true);
  }
  return next;
}

function installCodexTerminalFences(
  runtime: SessionRuntime,
  incoming: readonly CodexTerminalFence[] | undefined,
  options: {
    revision: string;
    generation: string | null;
    continuationTurnIds?: readonly string[];
    // A current newest-page History carries a complete bounded snapshot. A
    // stale build may add an exact terminal which the browser has not seen, but
    // must not erase a newer pending fence merely because its older snapshot
    // omitted it.
    replaceSnapshot?: boolean;
    // A complete current History can prove that a completed matching row is
    // the final narrative segment for this native turn. Live/stale paths cannot:
    // Codex steer segments may share the same native turn id and arrive later.
    consumeSettledMatches?: boolean;
  },
): void {
  const { revision, generation } = options;
  const continuationTurnIds = options.continuationTurnIds ?? [];
  const previous = runtime.pendingTerminalFences;
  const sameScope = !!previous
    && previous.revision === revision
    && previous.generation === generation;
  if (incoming === undefined && !sameScope) return;
  const candidates = boundedTerminalFences(
    incoming === undefined
      ? (sameScope ? previous!.fences : [])
      : options.replaceSnapshot === false && sameScope
        ? [...previous!.fences, ...incoming]
        : incoming,
  );
  const continuations = new Set(continuationTurnIds);
  const unresolved: CodexTerminalFence[] = [];
  let turns = runtime.turns;
  let copied = false;
  for (const fence of candidates) {
    if (fence.status === "interrupted"
        && continuations.has(fence.turn_id)) {
      continue;
    }
    const matches = turns.flatMap((turn, index) =>
      turnHasExactNativeTerminalId(turn, fence.turn_id) ? [index] : []);
    const openMatches = matches.filter((index) => !turns[index].done);
    const targetIndex = openMatches.length === 1 ? openMatches[0] : -1;
    if (targetIndex < 0) {
      if (matches.length === 0 || !options.consumeSettledMatches) {
        unresolved.push(fence);
      }
      continue;
    }
    // A fence only repairs an unfinished narrative row. If the exact turn is
    // already terminal, its richer live/history outcome wins; never let an
    // older recovery hint rewrite a completed error or interruption.
    if (!copied) {
      turns = [...turns];
      copied = true;
    }
    turns[targetIndex] = applyTerminalFence(turns[targetIndex], fence);
    if (!options.consumeSettledMatches) {
      // A stale/running page can arrive before an ordered TurnSteered frame.
      // Close the only exact row we can currently see, but retain the fence so
      // a later segment sharing this native turn id cannot reopen forever.
      unresolved.push(fence);
    }
  }
  if (copied) runtime.turns = turns;
  runtime.pendingTerminalFences = unresolved.length > 0
    ? { revision, generation, fences: unresolved }
    : null;
}

function mergePendingTerminalFences(
  source: SessionRuntime["pendingTerminalFences"],
  target: SessionRuntime["pendingTerminalFences"],
): SessionRuntime["pendingTerminalFences"] {
  if (!source) return target;
  if (!target) return source;
  if (source.revision !== target.revision
      || source.generation !== target.generation) return source;
  return {
    revision: source.revision,
    generation: source.generation,
    fences: boundedTerminalFences([...target.fences, ...source.fences]),
  };
}

function applyPendingCodexTerminalFences(runtime: SessionRuntime): void {
  const pending = runtime.pendingTerminalFences;
  if (!pending) return;
  installCodexTerminalFences(runtime, undefined, {
    revision: pending.revision,
    generation: pending.generation,
  });
}

function discardPendingCodexTerminalFence(
  runtime: SessionRuntime,
  turnId: string | null | undefined,
): void {
  const pending = runtime.pendingTerminalFences;
  if (!pending || !turnId) return;
  const fences = pending.fences.filter((fence) => fence.turn_id !== turnId);
  runtime.pendingTerminalFences = fences.length > 0
    ? { ...pending, fences }
    : null;
}

function findExplicitLiveTaskOwner(
  runtime: SessionRuntime,
  turns: Turn[],
  newerThan?: number | null,
): Turn | undefined {
  // An ordered live row is the post-fence owner even when an early History
  // snapshot has also painted an aliasless active shell. Message deltas carry
  // no turn id, so prefer this binding before the provisional History head.
  // Existing message/item ownership still wins at each call site, preserving
  // delayed pre-fence updates on their old segment.
  const owner = runtime.liveOwner;
  const orderedOwner = owner
    ? [...turns].reverse().find((turn) =>
        !turn.done && turnHasIdentityAlias(turn, owner.turnId))
    : undefined;
  if (orderedOwner && (newerThan === undefined
      || (newerThan != null && owner!.seq > newerThan))) {
    return orderedOwner;
  }
  if (newerThan !== undefined) return undefined;
  // After a wrapper restart, liveTaskId values left in the visible projection
  // belong to the previous generation. Only the ordered owner established by
  // a new-generation event is eligible for unbound narrative routing.
  if (runtime.legacyLiveFallbackBlocked) return undefined;
  return [...turns].reverse().find((turn) =>
    !turn.done && !!turn.liveTaskId);
}

function runtimeOrderingGeneration(runtime: SessionRuntime): string | null {
  return runtime.controlGeneration ?? runtime.historyGeneration;
}

function activatePendingLiveBinding(
  runtime: SessionRuntime,
  turns: Turn[],
  eventSeq: number | null | undefined,
  nativeTurnId?: string | null,
  create = true,
): Turn | undefined {
  const binding = runtime.pendingLiveBinding;
  if (!binding) return undefined;
  if (binding.generation !== runtimeOrderingGeneration(runtime)) {
    runtime.pendingLiveBinding = null;
    return undefined;
  }
  if (typeof eventSeq === "number" && eventSeq < binding.seq) return undefined;
  if (nativeTurnId && nativeTurnId !== binding.turnId) return undefined;
  // A delayed sequenced binding/replay from a completed turn may still be in
  // the ring, so only a current running lifecycle may reopen it. Sequence zero
  // is reserved for the wrapper's client-only reconnect seed: the wrapper emits
  // that frame only while the resident context is non-idle, and it may arrive
  // before a freshly loaded reducer has recovered its lifecycle state. Use it
  // solely for narrative ownership; later State/History remains authoritative
  // for whether the UI itself is running.
  if (runtime.state !== "running" && binding.seq !== 0) return undefined;

  const matches = turns.filter((turn) =>
    turnHasIdentityAlias(turn, binding.msgId));
  if (matches.length > 1) return undefined;
  let owner = matches[0];
  if (!owner) {
    if (!create) return undefined;
    owner = {
      id: binding.msgId,
      clientMsgId: binding.msgId,
      prompt: "",
      blocks: [],
      done: false,
    };
    turns.push(owner);
  }
  if (owner.forkPointId && owner.forkPointId !== binding.turnId
      && owner.liveTaskId !== binding.turnId) return undefined;
  // Resolving an owner for TurnEnd must not reactivate the row already closed
  // by its preceding Error. Only live content may activate a pending binding.
  if (owner.done && create) {
    owner.done = false;
    owner.doneTs = undefined;
    owner.durationMs = undefined;
    owner.interrupted = undefined;
    owner.error = undefined;
    owner.progress = undefined;
    delete owner.terminalSource;
  }
  owner.forkPointId ??= binding.turnId;
  runtime.liveOwner = {
    turnId: owner.id,
    seq: Math.max(binding.seq, eventSeq ?? binding.seq),
  };
  return owner;
}

function bindAuthoritativeActiveHistoryHead(
  runtime: SessionRuntime,
  turns: Turn[],
  msgId: string,
  nativeTurnId: string,
  seq: number,
  newestId: string | null = runtime.historyNewestId,
  assumeRunning = false,
  continuationTurnIds?: string[] | null,
): Turn | undefined {
  if ((!assumeRunning && runtime.state !== "running") || !newestId) {
    return undefined;
  }
  const heads = turns.filter((turn) =>
    !turn.done
    && turnHasIdentityAlias(turn, newestId));
  if (heads.length !== 1) return undefined;
  const head = heads[0];
  const historyStreamId = head.forkPointId;
  if (head.clientMsgId && !turnHasIdentityAlias(head, msgId)) return undefined;
  const logicalOwners = turns.filter((turn) =>
    turnHasIdentityAlias(turn, msgId));
  if (logicalOwners.length > 1) return undefined;
  if (head.historyTurnId !== nativeTurnId
      && findTurnByEngineId([head], nativeTurnId) !== head) {
    if (!historyStreamId
        || !continuationTurnIds?.includes(nativeTurnId)
        || !continuationTurnIds.includes(historyStreamId)) return undefined;
    head.liveTaskId = nativeTurnId;
  }
  head.clientMsgId ??= msgId;
  const logical = logicalOwners[0];
  if (logical && logical !== head) {
    const headIndex = turns.indexOf(head);
    const logicalIndex = turns.indexOf(logical);
    const mergedTurns = mergeInitialHistory(
      [head], [logical], { preserveLiveTailOpen: true });
    if (mergedTurns.length !== 1) return undefined;
    const merged = mergedTurns[0];
    // A recovered Codex control turn can own a distinct rollout stream task.
    // Keep History's exact stream identity as the canonical branch point and
    // record the control id only as a routing alias. Both ids must have arrived
    // in the wrapper's bounded continuation proof above; never infer this from
    // prompt text, timestamps, running state, or row position.
    merged.forkPointId ??= nativeTurnId;
    const first = Math.min(headIndex, logicalIndex);
    const second = Math.max(headIndex, logicalIndex);
    turns.splice(second, 1);
    turns.splice(first, 1, merged);
    runtime.liveOwner = { turnId: merged.id, seq };
    return merged;
  }
  head.forkPointId ??= nativeTurnId;
  runtime.liveOwner = { turnId: head.id, seq };
  return head;
}

function findBoundLiveTaskOwner(
  runtime: SessionRuntime,
  turns: Turn[],
  nativeTurnId: string | null | undefined,
  eventSeq: number | null | undefined,
  create = true,
): Turn | undefined {
  const pending = activatePendingLiveBinding(
    runtime, turns, eventSeq, nativeTurnId, create);
  if (pending) return pending;
  const explicit = findExplicitLiveTaskOwner(runtime, turns);
  return explicit && (!nativeTurnId
      || findTurnByEngineId([explicit], nativeTurnId) === explicit)
    ? explicit : undefined;
}

function remapExplicitLiveTaskOwner(
  owner: SessionRuntime["liveOwner"],
  turns: Turn[],
): SessionRuntime["liveOwner"] {
  if (!owner) return null;
  const aliases = turns.filter((turn) =>
    turnHasIdentityAlias(turn, owner.turnId));
  return aliases.length === 1
    ? { ...owner, turnId: aliases[0].id }
    : null;
}

function remapTurnProvenanceIds(
  ids: readonly string[],
  previousTurns: readonly Turn[],
  turns: readonly Turn[],
): string[] {
  return [...new Set(ids.flatMap((id) => {
    const directOwners = turns.filter((turn) =>
      turnHasIdentityAlias(turn, id));
    if (directOwners.length === 1) return [directOwners[0].id];
    // Legacy cache migrations can leave a colliding display id on two real
    // rows. Preserve that still-valid provenance exactly as before; only an id
    // which disappeared with a collapsed projection needs block-id remapping.
    if (directOwners.length > 1) return [id];
    const source = previousTurns.filter((turn) =>
      turnHasIdentityAlias(turn, id));
    if (source.length !== 1) return [];
    const stableBlockIds = new Set(source[0].blocks.map((block) =>
      block.kind === "text" ? `message:${block.message_id}`
        : block.kind === "tool" ? `tool:${block.tool_use_id}`
          : `process:${block.item_id}`));
    if (stableBlockIds.size === 0) return [];
    const blockOwners = turns.filter((turn) => turn.blocks.some(
      (block) => stableBlockIds.has(
        block.kind === "text" ? `message:${block.message_id}`
          : block.kind === "tool" ? `tool:${block.tool_use_id}`
            : `process:${block.item_id}`)));
    return blockOwners.length === 1 ? [blockOwners[0].id] : [];
  }))].slice(-MAX_LIVE_DETAIL_TURN_IDS);
}

function findCurrentUnownedLiveOwner(
  runtime: SessionRuntime, turns: Turn[],
): Turn | undefined {
  const authoritativeHead = findAuthoritativeActiveHistoryHead(runtime, turns);
  if (!authoritativeHead) return findExplicitLiveTaskOwner(runtime, turns);
  // An ordered live row only supersedes this exact History head when its
  // downstream sequence proves it was created after the head's live fence.
  // Array order is not evidence: a summary row can lack timestamps and sort
  // ahead of an older local owner after Command+R.
  return findExplicitLiveTaskOwner(
    runtime, turns, runtime.historyFence,
  )
    ?? authoritativeHead;
}

function openUnboundLiveTurn(
  runtime: SessionRuntime,
  turns: Turn[],
  fallbackId: string,
  ts?: number,
  eventSeq?: number | null,
): Turn {
  const bound = activatePendingLiveBinding(
    runtime, turns, eventSeq, undefined, true);
  if (bound) return bound;
  const owner = findCurrentUnownedLiveOwner(runtime, turns);
  if (owner) return owner;
  if (!runtime.legacyLiveFallbackBlocked) {
    return openTurn(turns, fallbackId, ts);
  }
  // openTurn deliberately reuses an unfinished tail for generation-less
  // legacy streams. Across a wrapper restart that tail belongs to the old
  // sequence domain, so establish a distinct row even while it remains open.
  const turn = { id: fallbackId, prompt: "", blocks: [], done: false, ts };
  turns.push(turn);
  return turn;
}

function turnHasBoundEngineId(turn: Turn): boolean {
  // Child process rows do not establish terminal ownership. Claude can stamp
  // them with the browser message id while its ResultMessage reveals a distinct
  // assistant UUID only at TurnEnd, which is precisely the legacy fallback
  // this guard must preserve. A process id outside every visible alias is an
  // independent native binding, though, and must reject an unrelated terminal.
  const aliases = turnIdentityAliases(turn);
  return !!(turn.liveTaskId || turn.forkPointId || turn.codexTurnId
    || mutableTurnBlocks(turn).some((block) => block.kind === "process"
      && !!block.turn_id && !aliases.includes(block.turn_id)));
}

function findTurnOwningItem(turns: Turn[], id: string | null | undefined): Turn | undefined {
  if (!id) return undefined;
  return [...turns].reverse().find((turn) => mutableTurnBlocks(turn).some((block) =>
    block.kind === "tool" ? block.tool_use_id === id
      : block.kind === "process" ? block.item_id === id
        : block.message_id === id));
}

function resolvedChannel(current: AssistantChannel | undefined, next: AssistantChannel): AssistantChannel {
  return next === "unknown" ? (current ?? "unknown") : next;
}

function terminalProcessStatus(status: ProcessStatus): boolean {
  return status === "succeeded" || status === "failed" || status === "declined"
    || status === "cancelled" || status === "interrupted";
}

function backgroundProcessBlock(item: BackgroundProcessItem): ProcessBlock {
  const startedTs = eventTimestampMs(item.started_at);
  const updatedTs = eventTimestampMs(item.updated_at);
  return {
    kind: "process",
    item_id: item.item_id,
    processKind: item.kind,
    phase: "snapshot",
    status: item.status,
    turn_id: item.turn_id,
    parent_id: item.parent_id,
    title: item.title,
    summary: item.summary,
    progress: item.progress,
    command: item.command,
    cwd: item.cwd,
    background: true,
    done: false,
    startedTs,
    updatedTs,
  };
}

function applyBackgroundProcessEdge(
  runtime: SessionRuntime,
  event: Extract<ServerEvent, { type: "process" }>,
): void {
  if (event.background !== true
      || (event.kind !== "agent" && event.kind !== "task")) return;
  const current = runtime.backgroundProcesses;
  const index = current.findIndex((block) => block.item_id === event.item_id);
  const terminal = event.phase === "end" || terminalProcessStatus(event.status);
  if (terminal) {
    if (index >= 0) {
      runtime.backgroundProcesses = [
        ...current.slice(0, index), ...current.slice(index + 1),
      ];
    }
    return;
  }
  // Only a fresh level can prove that no background work remains. A start edge
  // invalidates a previously empty level; a terminal edge deliberately does
  // not turn the locally-derived empty list into authority.
  runtime.backgroundLevelEmpty = false;
  if (index < 0 && current.length >= MAX_BACKGROUND_PROCESS_ITEMS) return;
  const prior = index >= 0 ? current[index] : undefined;
  const stamp = eventTimestampMs(event.ts);
  const block: ProcessBlock = {
    kind: "process",
    item_id: event.item_id,
    processKind: event.kind,
    phase: event.phase,
    status: event.status,
    turn_id: event.turn_id ?? prior?.turn_id,
    parent_id: event.parent_id ?? prior?.parent_id,
    title: event.title || prior?.title || "后台任务",
    summary: event.summary ?? prior?.summary,
    progress: event.progress ?? prior?.progress,
    command: event.command ?? prior?.command,
    cwd: event.cwd ?? prior?.cwd,
    background: true,
    done: false,
    startedTs: prior?.startedTs ?? stamp,
    updatedTs: stamp ?? prior?.updatedTs,
  };
  runtime.backgroundProcesses = index >= 0
    ? current.map((candidate, candidateIndex) =>
        candidateIndex === index ? block : candidate)
    : [...current, block];
}

function isOmissionBlock(block: Block): boolean {
  return block.kind === "process" && block.item_id === OMITTED_PROCESS_ITEM_ID;
}

function isFinalTextBlock(block: Block): boolean {
  return block.kind === "text" && block.channel === "final";
}

function boundedString(value: string | null | undefined, maxChars: number) {
  if (value == null || value.length <= maxChars) return value;
  return value.slice(0, maxChars);
}

function boundedRecord(
  value: Record<string, unknown> | null | undefined,
  maxChars = MAX_LIVE_DETAIL_CHARS,
): Record<string, unknown> | null | undefined {
  if (value == null) return value;
  try {
    if (JSON.stringify(value).length <= maxChars) return value;
  } catch {
    // Cache values should be JSON-compatible, but fail closed if an extension
    // or a future migration hands us a recursive object.
  }
  return { _truncated: true, summary: "输入过大，已省略" };
}

function limitedBlockPayload(block: Block): Block {
  if (block.kind === "text") {
    const text = boundedString(block.text, MAX_LIVE_TEXT_CHARS) ?? "";
    return text === block.text ? block : { ...block, text };
  }
  if (block.kind === "tool") {
    const input = boundedRecord(block.input) ?? {};
    return {
      ...block,
      input,
      progress: boundedString(block.progress, MAX_LIVE_PROGRESS_CHARS) ?? undefined,
      output: boundedString(block.output, MAX_LIVE_TOOL_OUTPUT_CHARS) ?? undefined,
      diff: boundedString(block.diff, MAX_LIVE_DIFF_CHARS) ?? undefined,
      result: block.result ? {
        ...block.result,
        content: boundedString(
          block.result.content, MAX_LIVE_TOOL_OUTPUT_CHARS) ?? "",
        summary: boundedString(
          block.result.summary, MAX_LIVE_PROGRESS_CHARS),
        diff: boundedString(block.result.diff, MAX_LIVE_DIFF_CHARS),
      } : undefined,
    };
  }
  return {
    ...block,
    title: boundedString(block.title, 1024) || "处理事件",
    summary: boundedString(block.summary, MAX_LIVE_PROGRESS_CHARS),
    detail: boundedString(block.detail, MAX_LIVE_DETAIL_CHARS),
    input: boundedRecord(block.input),
    output: boundedString(block.output, MAX_LIVE_TOOL_OUTPUT_CHARS),
    diff: boundedString(block.diff, MAX_LIVE_DIFF_CHARS),
    progress: boundedString(block.progress, MAX_LIVE_PROGRESS_CHARS),
    server: boundedString(block.server, 1024),
    tool: boundedString(block.tool, 1024),
    command: boundedString(block.command, MAX_LIVE_DETAIL_CHARS),
    cwd: boundedString(block.cwd, 16 * 1024),
    explanation: boundedString(block.explanation, MAX_LIVE_PROGRESS_CHARS),
    plan: block.plan?.slice(0, 128).map((entry) => ({
      ...entry, step: boundedString(entry.step, 16 * 1024) || "（空步骤）",
    })),
  };
}

function jsonChars(value: unknown): number {
  try { return JSON.stringify(value)?.length ?? 0; }
  catch { return MAX_TURN_BLOCK_CHARS + 1; }
}

function blockPayloadChars(block: Block): number {
  if (block.kind === "text") {
    return 128 + block.message_id.length + block.text.length
      + (block.questions ? jsonChars(block.questions) : 0);
  }
  if (block.kind === "tool") {
    return 256 + block.message_id.length + block.tool_use_id.length
      + block.tool.length + (block.title?.length ?? 0) + (block.parent_id?.length ?? 0)
      + (block.server?.length ?? 0) + (block.progress?.length ?? 0)
      + (block.output?.length ?? 0) + (block.diff?.length ?? 0)
      + (block.result?.content.length ?? 0) + (block.result?.summary?.length ?? 0)
      + (block.result?.diff?.length ?? 0) + jsonChars(block.input);
  }
  return 256 + block.item_id.length + block.title.length
    + (block.turn_id?.length ?? 0) + (block.parent_id?.length ?? 0)
    + (block.summary?.length ?? 0) + (block.detail?.length ?? 0)
    + (block.output?.length ?? 0) + (block.diff?.length ?? 0)
    + (block.progress?.length ?? 0) + (block.server?.length ?? 0)
    + (block.tool?.length ?? 0) + (block.command?.length ?? 0)
    + (block.cwd?.length ?? 0) + (block.explanation?.length ?? 0)
    + (block.plan?.reduce((sum, entry) => sum + entry.step.length + 16, 0) ?? 0)
    + jsonChars(block.input);
}

function turnBlockPayloadChars(blocks: Block[]): number {
  let total = 0;
  for (const block of blocks) {
    total += blockPayloadChars(block);
    if (total > MAX_TURN_BLOCK_CHARS) return total;
  }
  return total;
}

function archiveLiveSpillBlocks(turn: Turn, spilled: Block[]): void {
  if (spilled.length === 0) return;
  const merged = mergeDetailWithLiveTail(
    turn.liveSpillBlocks ?? [],
    spilled,
  ).sort((left, right) => {
    if (left.liveOrder == null || right.liveOrder == null) return 0;
    return left.liveOrder - right.liveOrder;
  });
  let start = merged.length;
  let chars = 0;
  while (start > 0
      && merged.length - start < MAX_LIVE_SPILL_ARCHIVE_BLOCKS) {
    const size = blockPayloadChars(merged[start - 1]);
    if (start < merged.length
        && chars + size > MAX_LIVE_SPILL_ARCHIVE_CHARS) break;
    start -= 1;
    chars += size;
  }
  turn.liveSpillBlocks = merged.slice(start);
}

function ensureLiveBlockOrder(turn: Turn): void {
  const archive = turn.liveSpillBlocks ?? [];
  const allBlocks = [...archive, ...turn.blocks];
  if (allBlocks.every((block) => block.liveOrder != null)) return;

  if (!turn.liveBlocksSpilled && archive.length === 0) {
    // The first spill establishes source order for history/cache blocks which
    // predate liveOrder. Normalize the whole visible sequence so an older
    // cached block cannot sort after a newly streamed block that already has
    // order zero.
    turn.blocks.forEach((block, index) => { block.liveOrder = index; });
    turn.nextLiveBlockOrder = turn.blocks.length;
    return;
  }

  let next = allBlocks.reduce(
    (maximum, block) => Math.max(maximum, block.liveOrder ?? -1), -1) + 1;
  for (const block of allBlocks) {
    if (block.liveOrder != null) continue;
    block.liveOrder = next;
    next += 1;
  }
  turn.nextLiveBlockOrder = Math.max(turn.nextLiveBlockOrder ?? 0, next);
}

function continuedLiveSpillRefreshDue(turn: Turn): boolean {
  if (!turn.liveBlocksSpilled) return false;
  return (turn.liveSpilledBlockCount ?? 0)
    - (turn.liveSpillRefreshCount ?? 0) >= LIVE_SPILL_REFRESH_BLOCKS;
}

/** Mutate one cloned turn into a fixed-size live tail.
 *
 * Keep at least the newest known final answer and newest live activity, then
 * prefer the remaining final blocks, remaining live blocks, and newest
 * completed process records in that order. Evicted records remain available
 * through source-backed TurnDetail pages; do not turn the memory boundary into
 * a visible "history omitted" product state. */
function limitTurnBlocks(turn: Turn): void {
  const markerCount = turn.blocks.reduce(
    (count, block) => count + (isOmissionBlock(block) ? 1 : 0), 0);
  if (turn.blocks.length <= MAX_TURN_BLOCKS && markerCount === 0
      && turnBlockPayloadChars(turn.blocks) <= MAX_TURN_BLOCK_CHARS) return;

  ensureLiveBlockOrder(turn);
  const candidates = turn.blocks.filter((block) => !isOmissionBlock(block))
    .map(limitedBlockPayload);
  const capacity = MAX_TURN_BLOCKS;
  const keep = new Set<number>();
  let retainedChars = 0;
  const keepNewest = (
    predicate: (block: Block) => boolean,
    one = false,
    required = false,
  ) => {
    for (let index = candidates.length - 1;
      index >= 0 && keep.size < capacity; index -= 1) {
      if (keep.has(index) || !predicate(candidates[index])) continue;
      const size = blockPayloadChars(candidates[index]);
      if (!required && retainedChars + size > MAX_TURN_BLOCK_CHARS) continue;
      keep.add(index);
      retainedChars += size;
      if (one) break;
    }
  };

  // Reserve one slot for each of the two user-critical classes before either
  // class is allowed to consume the rest of the window.
  keepNewest(isFinalTextBlock, true, true);
  keepNewest((block) => !block.done, true, true);
  keepNewest(isFinalTextBlock);
  keepNewest((block) => !block.done);
  keepNewest(() => true);

  const retained = candidates.filter((_, index) => keep.has(index));
  const spilled = candidates.filter((_, index) => !keep.has(index));
  archiveLiveSpillBlocks(turn, spilled);
  const newlySpilled = spilled.length;
  if (newlySpilled > 0 || markerCount > 0) {
    const firstSpill = turn.liveBlocksSpilled !== true;
    turn.liveBlocksSpilled = true;
    turn.liveSpilledBlockCount =
      (turn.liveSpilledBlockCount ?? 0) + newlySpilled;
    turn.detailEventCount = Math.max(
      turn.detailEventCount ?? 0,
      retained.length + (turn.liveSpilledBlockCount ?? 0),
    );
    if (spilled.some(isStateVisibleProcessBlock)) {
      turn.processDetailState = "present";
      ensureTurnDetailReason(turn, "process");
    }
    turn.detailLoaded = false;
    if ((firstSpill || continuedLiveSpillRefreshDue(turn))
        && turn.detailLoading !== true) {
      turn.detailRestorePending = true;
      turn.detailRestoreIncomplete = true;
    }
  }
  turn.blocks = retained;
}

function withLimitedTurnBlocks(turn: Turn): Turn {
  if (turn.blocks.length <= MAX_TURN_BLOCKS
      && turn.blocks.filter(isOmissionBlock).length === 0
      && turnBlockPayloadChars(turn.blocks) <= MAX_TURN_BLOCK_CHARS) return turn;
  const limited = { ...turn, blocks: [...turn.blocks] };
  limitTurnBlocks(limited);
  return limited;
}

/** Close every still-open stream owned by a turn when its enclosing request
 * reaches a terminal state.  The relay can report an Error without a trailing
 * TurnEnd, so leaving child tools/processes open here would keep the process
 * timeline stuck on "running" forever. */
function finishOpenBlocks(
  turn: Turn,
  status: "succeeded" | "failed" | "interrupted",
  isError: boolean,
  preserveOpenPlans = false,
  preserveBackground = false,
): void {
  finishOpenBlockList(
    mutableTurnBlocks(turn), status, isError,
    preserveOpenPlans, preserveBackground);
  if (turn.detailProjection) {
    finishOpenBlockList(
      turn.detailProjection.blocks, status, isError,
      preserveOpenPlans, preserveBackground);
  }
}

/** Settle stale children only at a projection boundary with no active task. */
function finishCompletedTurnChildren(
  turn: Turn,
  preserveOpenPlans = false,
  preserveBackground = false,
): void {
  if (!turn.done) return;
  const status = turn.interrupted
    ? "interrupted" : turn.error ? "failed" : "succeeded";
  finishOpenBlocks(
    turn, status, status !== "succeeded",
    preserveOpenPlans, preserveBackground);
}

/** Close only one newly-installed detail projection. A completed turn may have
 * acquired real background process events after TurnEnd; a late old
 * TurnDetail response must not close those live blocks again. */
function finishOpenDetailBlocks(
  turn: Turn,
  status: "succeeded" | "failed" | "interrupted",
  isError: boolean,
): void {
  if (turn.detailProjection) {
    // Exact terminal translations already close their Plan. Preserve an open
    // one because it identifies a neutral Codex steer segment.
    finishOpenBlockList(
      turn.detailProjection.blocks, status, isError, true);
  }
}

function finishOpenBlockList(
  blocks: Block[],
  status: "succeeded" | "failed" | "interrupted",
  isError: boolean,
  preserveOpenPlans = false,
  preserveBackground = false,
): void {
  for (const block of blocks) {
    if (block.kind === "text") {
      block.done = true;
    } else if (block.kind === "process" && !block.done) {
      if ((preserveOpenPlans && block.processKind === "plan")
          || (preserveBackground && block.background === true)) continue;
      block.done = true;
      if (!terminalProcessStatus(block.status)) block.status = status;
    } else if (block.kind === "tool" && !block.done) {
      block.done = true;
      block.result ??= {
        content: block.output ?? "",
        is_error: isError,
        status,
      };
    }
  }
}

const UNKNOWN_STEER_TERMINAL_ERROR =
  "任务已结束，但本次引导是否生效未得到确认。";

function clearAcceptance(runtime: SessionRuntime): void {
  runtime.acceptancePending = null;
  runtime.acceptanceKind = null;
  runtime.acceptanceHistoryBaseline = null;
}

function finishTurnWithoutTerminal(
  turn: Turn, doneTs: number,
  message: string | null = UNKNOWN_TERMINAL_ERROR,
): void {
  if (turn.done) return;
  turn.done = true;
  turn.doneTs ??= doneTs;
  turn.progress = undefined;
  turn.interrupted = true;
  if (message) turn.error ??= message;
  finishOpenBlocks(turn, "interrupted", true);
}

function finishTurnFromIdleHistory(turn: Turn, doneTs: number): void {
  if (turn.done) return;
  const completedFinal = turn.blocks.some((block) =>
    block.kind === "text"
    && block.channel === "final"
    && block.done
    && block.text.trim().length > 0);
  const allBlocksClosed = turn.blocks.every((block) => block.done);
  if (!turn.error && (!completedFinal || !allBlocksClosed)) {
    finishTurnWithoutTerminal(turn, doneTs);
    turn.terminalSource = "idle_history_recovery";
    return;
  }
  turn.done = true;
  turn.doneTs ??= doneTs;
  turn.progress = undefined;
  // Idle plus a source-complete final block is enough to repair a lost live
  // TurnEnd. Idle by itself is not: a crash can leave partial text or running
  // tools in the transcript, and those must remain visibly distinguishable
  // from success.
  turn.interrupted = false;
  finishOpenBlocks(turn, turn.error ? "failed" : "succeeded", !!turn.error);
}

function finishTurnAtSteerFence(
  turn: Turn, nativeTurnId: string, doneTs: number,
): void {
  if (turn.done) return;
  turn.done = true;
  // A steer fence is a narrative boundary, not an engine terminal carrying a
  // measured duration. The optimistic start may use the browser clock while
  // doneTs comes from the wrapper, so manufacturing 0 (or subtracting the two)
  // produces a false "已处理 0s". Keep the duration unknown until an
  // authoritative terminal supplies one.
  turn.durationMs = undefined;
  turn.doneTs = doneTs;
  turn.progress = undefined;
  if (turn.forkPointId === nativeTurnId) {
    turn.forkPointId = undefined;
  }
  turn.liveTaskId = undefined;
  finishOpenBlocks(turn, "succeeded", false);
}

function reconcileAcceptedSteerHistory(
  turns: Turn[], pendingId: string, doneTs: number,
): void {
  const acceptedIndex = turns.findIndex((turn) =>
    turnHasIdentityAlias(turn, pendingId));
  if (acceptedIndex < 0) return;
  const accepted = turns[acceptedIndex];
  const previous = [...turns.slice(0, acceptedIndex)]
    .reverse().find((turn) => !turn.done);
  if (!previous) return;
  // History can confirm the client/native user alias before the live
  // TurnSteered frame arrives. Move the active app-server task identity across
  // the same narrative fence so later process/TurnEnd events target the new
  // segment rather than resurrecting its predecessor.
  const nativeTurnId = previous.liveTaskId ?? previous.forkPointId;
  finishTurnAtSteerFence(previous, nativeTurnId ?? "", doneTs);
  if (nativeTurnId && !accepted.done) {
    accepted.liveTaskId ??= nativeTurnId;
  }
}

function resolveUnknownPendingSteer(
  runtime: SessionRuntime, turns: Turn[], doneTs: number,
): void {
  if (runtime.acceptanceKind !== "steer_unknown"
      || !runtime.acceptancePending) return;
  const pending = turns.find((turn) =>
    turn.id === runtime.acceptancePending
    && turn.clientMsgId === runtime.acceptancePending);
  if (pending) {
    finishTurnWithoutTerminal(
      pending, doneTs, UNKNOWN_STEER_TERMINAL_ERROR);
  }
  clearAcceptance(runtime);
}

/** Reconcile an unfinished browser tail against a current authoritative History
 * snapshot which explicitly says the session is idle. This is a lost-terminal
 * recovery path: keep already-rendered text/process detail, but never leave its
 * timer and child blocks running forever. */
function finishOpenTurnsFromIdleHistory(
  turns: Turn[], interrupted: boolean, doneTs: number,
  preserveTurnId?: string | null,
): Turn[] {
  return turns.map((turn) => {
    if (turn.done || turn.id === preserveTurnId) return turn;
    const next = { ...turn, blocks: turn.blocks.map((block) => ({ ...block })) };
    if (interrupted) finishTurnWithoutTerminal(next, doneTs, null);
    else finishTurnFromIdleHistory(next, doneTs);
    return next;
  });
}

const MAX_LIVE_TEXT_CHARS = 4 * 1024 * 1024;
const MAX_LIVE_TOOL_OUTPUT_CHARS = 2 * 1024 * 1024;
const MAX_LIVE_DIFF_CHARS = 2 * 1024 * 1024;
const MAX_LIVE_DETAIL_CHARS = 256 * 1024;
const MAX_LIVE_PROGRESS_CHARS = 64 * 1024;

function appendField(
  current: string | null | undefined,
  delta: string,
  maxChars: number,
): string {
  const value = current ?? "";
  if (value.length >= maxChars) return value;
  return value + delta.slice(0, maxChars - value.length);
}

function replaceWithBoundedTurns(runtime: SessionRuntime, turns: Turn[]): void {
  const itemBounded = turns.map(withLimitedTurnBlocks);
  const bounded = boundRuntimeTurns(itemBounded);
  if (bounded.length < itemBounded.length) {
    runtime.truncated = true;
    // Deep history is rendered in a separate sliding projection. The live
    // runtime may therefore remain newest-biased while still exposing the
    // first retained native turn as a valid server-before cursor.
    runtime.hasMore = true;
    runtime.oldestId = bounded[0]?.historyTurnId ?? bounded[0]?.id ?? null;
  }
  runtime.turns = bounded;
}

/** Translate one or more ordered detail pages without applying the live
 * Turn.blocks count/byte window. The wrapper already bounds every wire page;
 * the independent detail projection applies its own cumulative 4000/32 MiB
 * guard after all retained pages have been decoded together. */
function decodeTurnDetailEvents(
  state: AppState,
  sid: string,
  turnId: string,
  events: ServerEvent[],
): Turn | undefined {
  let scratch: AppState = {
    ...state,
    banner: undefined,
    historyBrowse: null,
    retainedHistoryBrowse: null,
    runtimes: { [sid]: createRuntime() },
  };
  for (const event of events) {
    scratch = reduceEvent(scratch, event, false);
  }
  return (scratch.runtimes[sid]?.turns ?? []).find(
    (turn) => canonicalTurnId(turn) === turnId || turn.id === turnId,
  );
}

function turnHasUnfinishedWork(turn: Turn): boolean {
  return !turn.done || turn.blocks.some((block) => !block.done);
}

/** Keep only work that still belongs to the live connection.
 *
 * Completed cache rows are never evidence that a turn still exists after an
 * authoritative History response: retaining them is what resurrected messages
 * removed by rollback.  An unfinished optimistic/streaming tail is different;
 * merge it by id/prompt so a history read racing the active turn stays smooth. */
function unfinishedLiveTail(turns: Turn[], hydratedCacheTurnIds: string[]): Turn[] {
  const cached = new Set(hydratedCacheTurnIds);
  return turns.filter((turn) => !cached.has(turn.id) && turnHasUnfinishedWork(turn));
}

/** Preserve the one browser-owned row which a later replay gap cannot rebuild.
 *
 * A truncated suffix is authoritative for neither completed history nor the
 * user boundary which may already have fallen out of the ring. The browser's
 * still-pending Query id, or the wrapper's same-generation TurnBinding, is an
 * exact connection-local proof for that one unfinished row. Carrying anything
 * else across the destructive recovery boundary would resurrect rolled-back
 * cache rows or conflate separate steer segments which share a native task. */
function recoverableSubmittedTurn(
  runtime: SessionRuntime,
  generation: string | null | undefined,
): Turn | undefined {
  const currentGeneration = runtimeOrderingGeneration(runtime);
  if (generation && currentGeneration && generation !== currentGeneration) {
    return undefined;
  }
  let ownerId = runtime.acceptancePending;
  if (!ownerId) {
    const binding = runtime.pendingLiveBinding;
    const expectedGeneration = generation ?? currentGeneration;
    if (!binding || runtime.state === "idle"
        || binding.generation !== expectedGeneration
        || binding.generation !== currentGeneration) {
      return undefined;
    }
    ownerId = binding.msgId;
  }
  const owners = runtime.turns.filter((turn) =>
    !turn.done && turnHasIdentityAlias(turn, ownerId));
  if (owners.length !== 1) return undefined;
  const owner = owners[0];
  if (!owner.prompt && !owner.images?.length && !owner.files?.length) {
    return undefined;
  }
  return cloneTurns([owner])[0];
}

/** Reopen only the exact newest row named by a current authoritative History.
 *
 * Codex 0.147 can persist ``interrupted`` for a native turn at a context
 * compaction boundary while that same turn keeps producing items. Only the
 * wrapper's live compact-fence ids plus the exact newest row are authoritative;
 * `in_progress`, recency, prompt text and timestamps are not. */
function reopenAuthoritativeActiveHistoryHead(
  runtime: SessionRuntime,
  turns: Turn[],
  newestId: string | null | undefined,
  ownerFenceSeq: number | null | undefined,
  compactionContinuationTurnIds: string[] | null | undefined,
): Turn[] {
  if (!newestId) return turns;
  const matches = turns.filter((turn) =>
    turnHasIdentityAlias(turn, newestId));
  if (matches.length !== 1) return turns;
  const turn = matches[0];
  const index = turns.indexOf(turn);
  if (findExplicitLiveTaskOwner(
    runtime, turns, ownerFenceSeq ?? null,
  )) {
    // The active native task is already owned by a newer ordered steer row.
    // Until block identity proves both aliases are the same visible segment,
    // leave this History shell terminal instead of displaying two spinners.
    return turns;
  }
  // Only the wrapper's live compact fence can reopen a persisted interrupted
  // row. ``in_progress``, a native forkPointId, recency, and browser-local
  // terminalSource are all insufficient on a cold refresh: they also describe
  // real user interrupts and crashes.
  if (!turn.forkPointId
      || !compactionContinuationTurnIds?.includes(turn.forkPointId)
      || !turn.done || !turn.interrupted) {
    return turns;
  }
  const next = [...turns];
  const reopened: Turn = {
    ...turn,
    done: false,
    doneTs: undefined,
    durationMs: undefined,
    interrupted: undefined,
    terminalSource: "compact_continuation",
  };
  delete reopened.error;
  next[index] = reopened;
  return next;
}

function findAuthoritativeActiveHistoryHead(
  runtime: SessionRuntime, turns: Turn[],
): Turn | undefined {
  if ((!runtime.mirroredRunning && runtime.state === "idle")
      || !runtime.historyNewestId) return undefined;
  const matches = turns.filter((turn) =>
    !turn.done
    && turnHasIdentityAlias(turn, runtime.historyNewestId));
  return matches.length === 1 ? matches[0] : undefined;
}

function markTurnAsLive(
  runtime: SessionRuntime, turnId: string, liveEvent: boolean,
  eventSeq?: number | null,
): void {
  if (!liveEvent) return;
  if (runtime.legacyLiveFallbackBlocked) {
    runtime.liveOwner = {
      turnId,
      seq: eventSeq != null
        ? eventSeq
        : runtime.liveOwner?.turnId === turnId
          ? runtime.liveOwner.seq : 0,
    };
  }
  if (eventSeq != null) {
    runtime.lastLiveSeq = Math.max(runtime.lastLiveSeq, eventSeq);
  }
  if (runtime.hydratedCacheTurnIds.length > 0) {
    runtime.hydratedCacheTurnIds = runtime.hydratedCacheTurnIds.filter(
      (cachedId) => cachedId !== turnId);
  }
}

const MAX_LIVE_DETAIL_TURN_IDS = 128;

function isStateVisibleProcessBlock(block: Block): boolean {
  if (block.kind === "tool") return true;
  if (block.kind === "text") {
    return block.channel === "commentary" && block.text.length > 0;
  }
  if (block.processKind === "reasoning") return false;
  if (block.processKind !== "hook") return true;
  return ["failed", "declined", "cancelled", "interrupted"].includes(
    block.status,
  );
}

function ensureTurnDetailReason(
  turn: Turn,
  reason: NonNullable<Turn["detailReasons"]>[number],
): void {
  if (turn.detailReasons?.includes(reason)) return;
  turn.detailReasons = [...(turn.detailReasons ?? []), reason];
}

function markVisibleProcessStarted(
  turn: Turn,
  stamp: number | undefined,
): void {
  turn.processDetailState = "present";
  ensureTurnDetailReason(turn, "process");
  if (stamp != null) {
    turn.processStartedTs = turn.processStartedTs == null
      ? stamp : Math.min(turn.processStartedTs, stamp);
  }
  // A later visible item reopens the process interval. It settles only after
  // every currently visible item reaches a terminal state.
  turn.processDoneTs = undefined;
}

function settleVisibleProcessIfComplete(
  turn: Turn,
  stamp: number | undefined,
): void {
  if (turn.processDetailState !== "present"
      || turn.processStartedTs == null || turn.processDoneTs != null
      || stamp == null) return;
  const visible = mutableTurnBlocks(turn).filter(isStateVisibleProcessBlock);
  if (visible.length === 0 || visible.some((block) => !block.done)) return;
  turn.processDoneTs = Math.max(turn.processStartedTs, stamp);
}

function settleOrphanedBackgroundProcessBlocks(
  turns: Turn[], stamp: number | undefined,
): Turn[] {
  const next = cloneTurns(turns);
  let changed = false;
  for (const turn of next) {
    let turnChanged = false;
    const blockLists: Block[][] = [turn.blocks];
    if (turn.liveSpillBlocks) blockLists.push(turn.liveSpillBlocks);
    if (turn.detailProjection) blockLists.push(turn.detailProjection.blocks);
    for (const blocks of blockLists) {
      for (const block of blocks) {
        if (block.kind !== "process"
            || block.background !== true
            || (block.processKind !== "agent" && block.processKind !== "task")
            || block.done) continue;
        block.done = true;
        block.phase = "end";
        // The empty native level proves only that the process is no longer
        // active. Do not fabricate success, failure, or cancellation.
        block.status = "unknown";
        block.updatedTs = stamp ?? block.updatedTs;
        block.terminalTs ??= stamp ?? block.updatedTs;
        turnChanged = true;
        changed = true;
      }
    }
    if (turnChanged) settleVisibleProcessIfComplete(turn, stamp);
  }
  return changed ? next : turns;
}

function reconcileAuthoritativeBackgroundProcessLevel(
  runtime: SessionRuntime,
): void {
  if (!runtime.backgroundLevelEmpty) return;
  runtime.turns = settleOrphanedBackgroundProcessBlocks(
    runtime.turns, runtime.backgroundLevelTs);
}

function markTurnDetailAsLive(
  runtime: SessionRuntime, turnId: string, liveEvent: boolean,
): void {
  if (!liveEvent || runtime.liveDetailTurnIds.includes(turnId)) return;
  runtime.liveDetailTurnIds = [
    ...runtime.liveDetailTurnIds,
    turnId,
  ].slice(-MAX_LIVE_DETAIL_TURN_IDS);
}

// Patch a runtime by sid (explicit sid wins; null/undefined → focused). `create`
// creates the runtime if missing (used by snapshot for a session we haven't
// seen). Unknown sid with create=false → no-op (drop the frame: it's for a
// non-resident session the client doesn't track yet).
function patch(state: AppState, sid: string | null | undefined,
               fn: (rt: SessionRuntime) => void, create = false): AppState {
  const key = sid ?? state.focusedSid;
  if (!key) return state;
  let rt = state.runtimes[key];
  if (!rt) {
    if (!create) return state;
    rt = createRuntime();
  } else {
    rt = { ...rt };
  }
  fn(rt);
  return { ...state, runtimes: { ...state.runtimes, [key]: rt } };
}

function applyCompletionProjection(
  runtime: SessionRuntime,
  incoming: { id: string | null; unread: boolean; revision: number },
): void {
  if (!runtime.completion
      || incoming.revision > runtime.completion.revision) {
    runtime.completion = incoming;
    return;
  }
  if (incoming.revision === runtime.completion.revision
      && incoming.id === runtime.completion.id
      && incoming.unread === runtime.completion.unread) {
    return;
  }
  // A same-revision conflict is malformed; retain the state already accepted
  // from this wrapper instead of allowing delivery order to toggle a badge.
}

/** Install one authoritative control value without allowing an older or
 * same-revision conflicting snapshot to resurrect a lock. */
function clearSessionControl(runtime: SessionRuntime): void {
  runtime.control = null;
  runtime.external = false;
  runtime.takeoverPending = false;
  runtime.takeoverMessage = null;
}

function switchControlGeneration(
  runtime: SessionRuntime, generation: string | null | undefined,
): void {
  if (!generation) return;
  const orderingGeneration = runtime.controlGeneration
    ?? runtime.historyGeneration;
  if (orderingGeneration != null && orderingGeneration !== generation) {
    runtime.historyFence = null;
    runtime.liveOwner = null;
    runtime.pendingLiveBinding = null;
    runtime.pendingTerminalFences = null;
    runtime.legacyLiveFallbackBlocked = true;
    runtime.backgroundProcesses = [];
    runtime.backgroundLevelEmpty = false;
    runtime.backgroundLevelTs = undefined;
  }
  if (runtime.historyGeneration !== null
      && generation !== runtime.historyGeneration) {
    runtime.liveDetailTurnIds = [];
    runtime.historyHeadKnown = false;
    // An empty projection from the previous wrapper is no longer proof that
    // this session is genuinely empty. Keep non-empty cached history painted,
    // but make an empty runtime wait for the new generation's History page.
    if (runtime.turns.length === 0) runtime.loading = true;
  }
  if (generation === runtime.controlGeneration) return;
  clearSessionControl(runtime);
  runtime.controlGeneration = generation;
  // History pages are scoped to one wrapper generation. A cursor/page loaded
  // from the previous process must never be merged across a restart boundary.
  runtime.hasLoadedOlderHistory = false;
}

function applySessionControl(
  runtime: SessionRuntime, incoming: SessionControl,
): boolean {
  const incomingGeneration = incoming.generation ?? null;
  if (runtime.controlGeneration !== null) {
    // Generation-less migration frames are accepted only by a runtime which is
    // itself still generation-less. A delayed event from another wrapper epoch
    // cannot compete on numeric revision.
    if (incomingGeneration !== runtime.controlGeneration) return false;
  } else if (incomingGeneration !== null) {
    clearSessionControl(runtime);
    runtime.controlGeneration = incomingGeneration;
  }
  const disposition = compareSessionControl(runtime.control, incoming);
  if (disposition !== "newer") return false;
  runtime.control = incoming;
  runtime.hasRevisionedControl = true;
  runtime.external = sessionControlLocksInput(incoming);
  runtime.takeoverPending = incoming.write_state === "takeover_pending";
  runtime.takeoverMessage = runtime.takeoverPending
    ? (incoming.reason ?? null) : null;
  return true;
}

function newestSessionControl(
  current: SessionControl | null, candidate: SessionControl | null,
): SessionControl | null {
  if (!candidate) return current;
  return compareSessionControl(current, candidate) === "newer"
    ? candidate : current;
}

function mergeNotices(...groups: Notice[][]): Notice[] {
  const merged: Notice[] = [];
  for (const notice of groups.flat()) {
    const prior = merged.findIndex((item) => item.notice_id === notice.notice_id);
    if (prior >= 0) merged.splice(prior, 1);
    merged.push(notice);
  }
  return merged.slice(-MAX_SESSION_NOTICES);
}

const RATE_RESET_JITTER_SECONDS = 60;

function mergeRateWindow(
  current: StatusRateWindow | null | undefined,
  update: StatusRateWindow | null | undefined,
  replace = false,
): StatusRateWindow | null | undefined {
  if (!update) return current;
  if (replace) return { ...update };
  const currentDuration = current?.window_duration_mins;
  const updateDuration = update.window_duration_mins;
  if (currentDuration != null && updateDuration != null
      && currentDuration !== updateDuration) {
    return { ...update };
  }
  const currentReset = current?.resets_at;
  const updateReset = update.resets_at;
  // Provider reset timestamps can jitter by a second across responses. A
  // genuinely new quota period advances by hours or days, so tolerate one
  // minute and reject late snapshots from the previous period.
  if (currentReset != null && updateReset != null
      && updateReset < currentReset - RATE_RESET_JITTER_SECONDS) {
    return current;
  }
  const newPeriod = currentReset != null && updateReset != null
    && updateReset > currentReset + RATE_RESET_JITTER_SECONDS;
  const next = { ...(current ?? {}) };
  if (newPeriod && update.used_percent == null) {
    delete next.used_percent;
  } else if (update.used_percent != null
      && (newPeriod || current?.used_percent == null
        || update.used_percent >= current.used_percent)) {
    next.used_percent = update.used_percent;
  }
  if (updateReset != null) {
    next.resets_at = currentReset == null || newPeriod
      ? updateReset : Math.max(currentReset, updateReset);
  }
  if (update.window_duration_mins != null) {
    next.window_duration_mins = update.window_duration_mins;
  }
  return next;
}

function mergeRateLimitCollection(
  source: readonly StatusRateLimit[], update: RateLimitUpdate,
): StatusRateLimit[] {
  const limits = source.map((limit) => ({ ...limit }));
  let index = update.limit_id
    ? limits.findIndex((limit) => limit.limit_id === update.limit_id)
    : limits.length === 1 ? 0 : -1;
  if (index < 0) {
    index = limits.length;
    limits.push({});
  }
  const current = limits[index];
  const next: StatusRateLimit = { ...current };
  const clearsReachedLimit = !!current.rate_limit_reached_type
    && update.reached_type === "";
  if (update.limit_id != null) next.limit_id = update.limit_id;
  if (update.name != null) next.limit_name = update.name;
  if (update.plan_type != null) next.plan_type = update.plan_type;
  if (update.reached_type != null) {
    next.rate_limit_reached_type = update.reached_type;
  }
  next.primary = mergeRateWindow(
    current.primary, update.primary, clearsReachedLimit);
  next.secondary = mergeRateWindow(
    current.secondary, update.secondary, clearsReachedLimit);
  limits[index] = next;
  return limits.slice(-16);
}

function mergeRateLimitUpdate(
  report: StatusReport | null, limits: StatusRateLimit[],
): StatusReport | null {
  return report ? { ...report, rate_limits: limits } : null;
}

export function reduce(state: AppState, action: Action): AppState {
  switch (action.type) {
    case "reset":
      return {
        ...initialState,
        sessions: [], runtimes: {}, artifact: null, dirPicker: null,
        newChat: null, btwByParentSid: {}, btwRevision: 0,
        catalog: {}, catalogDefault: {},
        catalogDefaultEffort: {}, catalogDefaultCwd: {}, claudeProfiles: [],
        defaultClaudeProfileId: null, claudeProfileByScope: {}, codexProfiles: [],
        defaultCodexProfileId: null, codexProfileByScope: {},
        retainedHistoryBrowse: null,
      };
    case "conn": {
      let banner = state.banner;
      if (action.connState === "connected") banner = undefined;
      else if (action.connState === "reconnecting") banner = action.detail || "正在重新连接…";
      else if (action.connState === "connecting") banner = "正在连接…";
      else if (action.connState === "disconnected" && action.detail) {
        banner = action.detail;
      }
      const runtimes = action.connState === "connected"
        ? state.runtimes
        : Object.fromEntries(Object.entries(state.runtimes).map(
            ([sid, runtime]) => [sid, {
              ...runtime, syncReady: false, replaying: false,
            }]));
      return {
        ...state,
        runtimes,
        connState: action.connState,
        // A reconnect may land on a restarted relay with no wrapper. Wait for
        // replay/snapshot proof before allowing background queue removal.
        wrapperOnline: action.connState === "connected" ? state.wrapperOnline : false,
        // Pagination waiters are frozen to one underlying socket generation.
        // Keep the authoritative live runtime, but reopen deep history from its
        // newest page after reconnect instead of accepting a delayed old page.
        historyBrowse: action.connState === "connected"
          ? state.historyBrowse : null,
        retainedHistoryBrowse: action.connState === "connected"
          ? state.retainedHistoryBrowse
          : state.historyBrowse ?? state.retainedHistoryBrowse,
        banner,
      };
    }
    case "command_error":
      return { ...state, banner: action.detail };
    case "dismiss_banner":
      return state.banner === action.banner
        ? { ...state, banner: undefined }
        : state;
    case "query_sent":
    case "steer_sent": {
      const current = state.runtimes[action.sid];
      if (!current || (current.acceptancePending
          && current.acceptancePending !== action.msg_id)) return state;
      const acceptanceHistoryBaseline = !current.historyInvalidated
          && !isHistoryRecoveryPending(state.historyRecovery, action.sid)
          && current.historyRevision
          && current.historyBuildSeq > 0
        ? {
          revision: current.historyRevision,
          generation: current.historyGeneration,
          buildSeq: current.historyBuildSeq,
          liveSeq: Math.max(current.historyLiveSeq, current.lastLiveSeq),
          newestId: current.historyNewestId,
        }
        : null;
      const turn: Turn = {
        id: action.msg_id, prompt: action.prompt, blocks: [], done: false,
        clientMsgId: action.type === "steer_sent"
          ? action.msg_id : undefined,
        images: action.images,
        files: action.files?.map((file) => ({ filename: file.filename, data: "" })),
        ts: action.ts,
      };
      let runtimes = reduceTargetedRuntime(
        state.runtimes, action.sid, { type: "query_sent", turn });
      const submittedRuntime = runtimes[action.sid];
      // A sequenced binding can arrive after the previous lifecycle already
      // became idle. It is deliberately dormant while idle, but must not wake
      // up when this newly submitted turn transitions the runtime to running.
      // Preserve only an early binding for this exact optimistic message.
      const clearsStaleBinding = action.type === "query_sent"
        && !!submittedRuntime?.pendingLiveBinding
        && submittedRuntime.pendingLiveBinding.msgId !== action.msg_id;
      if (submittedRuntime?.acceptancePending !== action.msg_id
          || submittedRuntime?.acceptanceKind == null
          || clearsStaleBinding) {
        runtimes = {
          ...runtimes,
          [action.sid]: {
            ...submittedRuntime,
            pendingLiveBinding: clearsStaleBinding
              ? null : submittedRuntime.pendingLiveBinding,
            acceptancePending: action.msg_id,
            acceptanceKind: action.type === "steer_sent"
              ? "steer" : "query",
            acceptanceHistoryBaseline,
          },
        };
      }
      const sessions = bumpSessionActivity(state.sessions, action.sid, action.ts);
      // Sending changes the live runtime, not the user's reading viewport.
      // Only an explicit return-to-latest/navigation action leaves history.
      if (runtimes === state.runtimes && sessions === state.sessions) {
        return state;
      }
      return { ...state, runtimes, sessions };
    }
    case "enqueue": {
      const targetSid = action.sid ?? state.focusedSid;
      const unconfirmed = collectUnconfirmedQueries(state.runtimes);
      if (!canEnqueueQuery(
        unconfirmed, action.query, deferredQueueCapacity(state),
      )) return state;
      const optimistic: PendingQuery = {
        ...action.query,
        queueKind: "queue",
        queueState: action.query.queueState ?? "submitting",
      };
      return patch(state, targetSid, (rt) => {
        rt.queue = [...rt.queue, optimistic];
      });
    }
    case "dequeue_at": {
      const runtimes = reduceTargetedRuntime(
        state.runtimes, action.sid, { type: "dequeue_at", i: action.i });
      return runtimes === state.runtimes ? state : { ...state, runtimes };
    }
    case "set_send_mode":
      return patch(state, action.sid, (rt) => { rt.sendMode = action.mode; });
    case "set_pending": {
      const targetSid = action.sid ?? state.focusedSid;
      const unconfirmed = collectUnconfirmedQueries(
        state.runtimes, targetSid);
      if (!canEnqueueQuery(
        unconfirmed,
        action.query,
        deferredQueueCapacity(state, targetSid),
      )) return state;
      const current = targetSid
        ? state.runtimes[targetSid]?.pendingSend : null;
      const replacesRetainedBytes = current?.queueState === "queued"
        ? current.retainedBytes
        : current?.queueState === "submitting"
          ? current.replacesRetainedBytes
          : undefined;
      const optimistic: PendingQuery = {
        ...action.query,
        queueKind: "replace",
        queueState: action.query.queueState ?? "submitting",
        replacesRetainedBytes:
          action.query.replacesRetainedBytes ?? replacesRetainedBytes,
      };
      return patch(
        state, targetSid, (rt) => { rt.pendingSend = optimistic; });
    }
    case "clear_pending": {
      const runtimes = reduceTargetedRuntime(
        state.runtimes, action.sid, { type: "clear_pending" });
      return runtimes === state.runtimes ? state : { ...state, runtimes };
    }
    case "remove_deferred":
      return patch(state, action.sid, (rt) => {
        rt.queue = rt.queue.filter(
          (query) => query.msg_id !== action.msgId);
        if (rt.pendingSend?.msg_id === action.msgId) {
          rt.pendingSend = null;
        }
        rt.failedDeferred = rt.failedDeferred.filter(
          (query) => query.msg_id !== action.msgId);
      });
    case "update_failed_deferred":
      return patch(state, action.sid, (rt) => {
        rt.failedDeferred = rt.failedDeferred.map((query) =>
          query.msg_id === action.msgId
            ? {
                ...query,
                prompt: action.prompt,
                retainedBytes: queuedQueryWireBytes({
                  ...query, prompt: action.prompt,
                }),
              }
            : query);
      });
    case "set_model":
      return patch(state, state.focusedSid, (rt) => { rt.model = action.model; });
    case "set_effort":
      return patch(state, state.focusedSid, (rt) => { rt.effort = action.effort; });
    case "set_perm":
      return patch(state, state.focusedSid, (rt) => { rt.perm = action.perm; });
    case "set_collaboration_mode":
      return patch(state, state.focusedSid, (rt) => {
        rt.collaborationMode = action.mode;
      });
    case "set_turns":
      return patch(state, action.sid, (rt) => {
        replaceWithBoundedTurns(rt, action.turns);
      }, true);
    case "set_context":
      return patch(state, state.focusedSid, (rt) => {
        rt.contextReport = action.report;
        if (action.report.available !== false
            && action.report.source !== "recent_turn") {
          rt.contextExactReport = action.report;
        }
      });
    case "clear_context":
      return patch(state, state.focusedSid, (rt) => {
        rt.contextReport = null;
        rt.contextExactReport = null;
      });
    case "begin_context_request":
      return patch(state, action.sid, (rt) => {
        rt.contextRequestId = action.requestId;
        rt.contextRefreshDeferred = false;
        rt.contextError = null;
      });
    case "defer_context_request":
      return patch(state, action.sid, (rt) => {
        if (rt.contextRequestId === null) {
          rt.contextRefreshDeferred = true;
          rt.contextError = null;
        }
      });
    case "begin_status_request":
      return patch(state, action.sid, (rt) => {
        rt.statusRequestId = action.requestId;
        rt.statusError = null;
        rt.resetCreditResult = null;
      });
    case "set_artifact":
      return { ...state, artifact: action.artifact };
    case "open_artifact_loading":
      // optimistic: show the diff panel (with a spinner) instantly on click; the
      // diff_report event replaces it with the real sections when it arrives.
      return { ...state, artifact: {
        file: action.file, sid: action.sid, requestId: action.requestId,
        kind: "gitdiff", sections: [], loading: true,
      } };
    case "open_file_loading":
      return { ...state, artifact: {
        file: action.file, sid: action.sid, requestId: action.requestId,
        kind: action.kind, line: action.line, content: "", assets: {}, loading: true,
      } };
    case "begin_preview_asset": {
      const artifact = state.artifact;
      if (!artifact || artifact.kind !== "md"
          || artifact.sid !== action.sid
          || artifact.requestId !== action.previewId) return state;
      return { ...state, artifact: {
        ...artifact,
        assets: {
          ...artifact.assets,
          [action.path]: {
            requestId: action.requestId,
            previewId: action.previewId,
            loading: true,
          },
        },
      } };
    }
    case "submit_preview_authorization":
    case "preview_authorization_retry_started":
    case "preview_authorization_retry_failed": {
      const artifact = state.artifact;
      if (!artifact || artifact.sid !== action.sid) return state;
      const update = (
        authorization: PreviewAuthorizationState | undefined,
      ): PreviewAuthorizationState | undefined => {
        if (!authorization
            || authorization.authorizationId !== action.authorizationId
            || authorization.requestId !== action.requestId) {
          return authorization;
        }
        if (action.type === "submit_preview_authorization") {
          return { ...authorization, status: "submitting" };
        }
        return undefined;
      };
      if (artifact.authorization) {
        const authorization = update(artifact.authorization);
        if (authorization === artifact.authorization) return state;
        return { ...state, artifact: {
          ...artifact,
          authorization,
          loading: action.type === "preview_authorization_retry_started",
          error: action.type === "preview_authorization_retry_failed"
            ? "授权成功，但读取请求未能排队，请刷新文件重试。"
            : artifact.error,
        } };
      }
      for (const [path, asset] of Object.entries(artifact.assets ?? {})) {
        if (!asset.authorization) continue;
        const authorization = update(asset.authorization);
        if (authorization === asset.authorization) continue;
        return { ...state, artifact: {
          ...artifact,
          assets: {
            ...artifact.assets,
            [path]: {
              ...asset,
              authorization,
              loading:
                action.type === "preview_authorization_retry_started",
              error: action.type === "preview_authorization_retry_failed"
                ? "授权成功，但图片读取请求未能排队，请刷新文件重试。"
                : asset.error,
            },
          },
        } };
      }
      return state;
    }
    case "start_file_save":
      if (!state.artifact || state.artifact.kind !== "md") return state;
      return { ...state, artifact: {
        ...state.artifact,
        saveRequestId: action.requestId,
        saving: true,
        saveStatus: undefined,
        saveError: undefined,
        pendingContent: action.content,
      } };
    case "clear_artifact":
      return { ...state, artifact: null };
    case "select_btw": {
      const group = state.btwByParentSid[action.parentSid];
      if (!group || group.activeSid === action.btwSid
          || !group.chats.some((chat) => chat.sid === action.btwSid)) {
        return state;
      }
      return {
        ...state,
        btwByParentSid: {
          ...state.btwByParentSid,
          [action.parentSid]: { ...group, activeSid: action.btwSid },
        },
      };
    }
    case "clear_btw": {
      const group = state.btwByParentSid[action.parentSid];
      if (!group || !group.chats.some((chat) => chat.sid === action.btwSid)) {
        return state;
      }
      const runtimes = { ...state.runtimes };
      delete runtimes[action.btwSid];
      const chats = group.chats.filter((chat) => chat.sid !== action.btwSid);
      const btwByParentSid = { ...state.btwByParentSid };
      if (chats.length === 0) {
        delete btwByParentSid[action.parentSid];
      } else {
        const closedIndex = group.chats.findIndex(
          (chat) => chat.sid === action.btwSid);
        const fallback = chats[Math.min(closedIndex, chats.length - 1)];
        btwByParentSid[action.parentSid] = {
          chats,
          activeSid: group.activeSid === action.btwSid
            ? fallback.sid : group.activeSid,
        };
      }
      return { ...state, btwByParentSid, runtimes };
    }
    case "clear_all_btw": {
      const groups = Object.values(state.btwByParentSid);
      if (groups.length === 0) {
        return state.btwRevision === 0
          ? state : { ...state, btwRevision: 0 };
      }
      const runtimes = { ...state.runtimes };
      for (const group of groups) {
        for (const chat of group.chats) delete runtimes[chat.sid];
      }
      return { ...state, btwByParentSid: {}, btwRevision: 0, runtimes };
    }
    case "clear_session_list":
      return {
        ...state, sessions: [], focusedSid: null, historyRecovery: null,
        historyBrowse: null, retainedHistoryBrowse: null,
      };
    case "restore_session_list":
      // Surface switches are view changes. Paint that surface's last accepted
      // list immediately, then let the in-flight authoritative list replace it.
      // Clearing first exposed Codex app-server startup time as a blank/frozen
      // sidebar even though the browser already had the exact rows in memory.
      return {
        ...state, sessions: action.sessions, focusedSid: null,
        historyRecovery: null, historyBrowse: null,
        retainedHistoryBrowse: null,
      };
    case "drop_fork_placeholder": {
      const sessions = state.sessions.filter((session) => !(
        session.provisional_fork
        && session.session_id === action.sid
        && session.forked_from_id === action.parentSid
      ));
      if (sessions.length === state.sessions.length) return state;
      const focused = state.focusedSid === action.sid;
      return {
        ...state,
        sessions,
        focusedSid: focused ? null : state.focusedSid,
        historyRecovery: focused ? null : state.historyRecovery,
        historyBrowse: focused ? null : state.historyBrowse,
        retainedHistoryBrowse: focused ? null : state.retainedHistoryBrowse,
      };
    }
    case "set_session_pinned": {
      const sessions = setSessionPinned(state.sessions, action.sid, action.pinned);
      return sessions === state.sessions ? state : { ...state, sessions };
    }
    case "focus_session": {
      // optimistic view switch: focus the session locally right away (its runtime
      // is usually already in memory) instead of waiting for the round-trip
      // session_focus. The server's session_focus later just re-confirms.
      const sid = action.sid;
      const rt = state.runtimes[sid] ?? createRuntime();
      // if we have no turns yet, mark loading so the UI shows a spinner (not the
      // empty "send a message" prompt) until cache-hydrate or the wrapper replay
      // lands. An authoritative empty head is already a complete projection and
      // must not flash the same loader on every focus round-trip.
      const runtimes = { ...state.runtimes, [sid]: {
        ...rt,
        loading: rt.turns.length === 0 && !rt.historyHeadKnown,
      } };
      return {
        ...state, focusedSid: sid, runtimes, artifact: null,
        historyRecovery: state.historyRecovery?.sid === sid
          ? state.historyRecovery : null,
        // Switching away and back always opens the authoritative latest
        // runtime. A delayed page from the previous viewId is then harmless.
        historyBrowse: null,
        retainedHistoryBrowse: state.retainedHistoryBrowse?.sid === sid
          ? state.retainedHistoryBrowse : null,
      };
    }
    case "turn_detail_requested":
      return patch(state, action.sid, (rt) => {
        rt.turns = rt.turns.map((turn) => (
          turn.id === action.turnId || canonicalTurnId(turn) === action.turnId)
          ? {
              ...turn,
              detailLoading: true,
              detailError: undefined,
              detailRetryBefore: action.before ?? null,
              detailRetryDirection: action.before == null
                ? "initial"
                : action.before === turn.detailNewerCursor
                  ? "newer" : "older",
              detailAutoLoad: action.before == null
                ? (action.autoLoad ?? true) : turn.detailAutoLoad,
              detailRestorePending: false,
              detailRestoreIncomplete: action.before == null
                ? action.autoLoad === false
                : turn.detailRestoreIncomplete,
              liveSpillRefreshCount: action.before == null
                && turn.liveBlocksSpilled
                ? turn.liveSpilledBlockCount
                : turn.liveSpillRefreshCount,
            }
          : turn);
      }, true);
    case "history_detail_reset_requested": {
      const context = action.context;
      if (context.target === "browse") {
        const browse = state.historyBrowse;
        if (!browse || state.focusedSid !== context.sid
            || browse.sid !== context.sid
            || browse.scopeKey !== context.scopeKey
            || browse.revision !== context.revision
            || browse.viewId !== context.viewId) return state;
        const historyBrowse = markBrowseDetailLoading(
          browse, context.turnId, true, {
            expectedScopeKey: context.scopeKey,
            expectedViewId: context.viewId,
          }, undefined, null, {
            before: null,
            direction: "initial",
          }, true);
        return historyBrowse === browse ? state : { ...state, historyBrowse };
      }
      const runtime = state.runtimes[context.sid];
      if (!runtime || runtime.historyRevision !== context.revision) return state;
      return patch(state, context.sid, (rt) => {
        rt.turns = rt.turns.map((turn) => (
          turn.id === context.turnId
            || canonicalTurnId(turn) === context.turnId)
          ? {
              ...turn,
              detailLoading: true,
              detailError: undefined,
              detailRetryBefore: null,
              detailRetryDirection: "initial" as const,
              detailResetPending: true,
            }
          : turn);
      }, true);
    }
    case "begin_history_browse": {
      const runtime = state.runtimes[action.sid];
      if (!runtime || state.focusedSid !== action.sid
          || isHistoryRecoveryPending(state.historyRecovery, action.sid)
          || runtime.historyInvalidated
          || runtime.historyRevision !== action.revision
          || (action.generation != null
            && runtime.historyGeneration !== action.generation)
          || !runtime.hasMore || !runtime.oldestId) return state;
      const mutation = createHistoryBrowse({
        scopeKey: action.scopeKey,
        sid: action.sid,
        revision: action.revision,
        generation: action.generation ?? runtime.historyGeneration,
        viewId: action.viewId,
        // A runtime detail response is frozen to that target. Entering a
        // separate browse projection must not copy its transient spinner: the
        // response will finish the runtime row, while this view can request its
        // own detail page later.
        baseTurns: runtime.turns.map((turn) => turn.detailLoading
          ? { ...turn, detailLoading: false }
          : turn),
        basePageKey: action.basePageKey,
        hasOlder: !!runtime.hasMore,
        olderCursor: runtime.oldestId,
      });
      return {
        ...state,
        historyBrowse: mutation.projection,
        retainedHistoryBrowse:
          state.retainedHistoryBrowse?.sid === action.sid
            ? null : state.retainedHistoryBrowse,
      };
    }
    case "install_history_browse_page": {
      const browse = state.historyBrowse;
      if (!browse || state.focusedSid !== action.sid
          || browse.sid !== action.sid
          || browse.scopeKey !== action.scopeKey
          || browse.revision !== action.revision
          || browse.viewId !== action.viewId
          || browse.windowEpoch !== action.windowEpoch
          || browse.olderCursor !== action.before
          || (action.generation != null
            && browse.generation !== action.generation)) return state;
      // App already needs the pure mutation result to persist evicted pages.
      // Reuse it when no intervening reducer action changed the source object;
      // otherwise recompute from the actual current state to preserve live
      // latestDirty/detail updates queued in the meantime.
      const prepared = action.prepared?.from === browse
          && action.prepared.to.sid === action.sid
          && action.prepared.to.scopeKey === action.scopeKey
          && action.prepared.to.revision === action.revision
          && action.prepared.to.generation === browse.generation
          && action.prepared.to.viewId === action.viewId
          && action.prepared.to.windowEpoch === action.windowEpoch + 1
        ? action.prepared.to : null;
      const mutation = prepared
        ? { projection: prepared, evictedPages: [] }
        : prependOlderPage(browse, action.page, {
            expectedScopeKey: action.scopeKey,
            expectedViewId: action.viewId,
            expectedWindowEpoch: action.windowEpoch,
            expectedOlderCursor: action.before,
            protectedTurnIds: action.protectedTurnIds,
          });
      const historyBrowse = mutation.projection;
      return historyBrowse === browse
        ? state : { ...state, historyBrowse };
    }
    case "install_history_browse_newer": {
      const browse = state.historyBrowse;
      if (!browse || state.focusedSid !== action.sid
          || browse.sid !== action.sid
          || browse.scopeKey !== action.scopeKey
          || browse.revision !== action.revision
          || browse.viewId !== action.viewId
          || browse.windowEpoch !== action.windowEpoch
          || (action.generation != null
            && browse.generation !== action.generation)
          || !browse.hasNewer
          || browse.newerPageKey !== action.page.pageKey) return state;
      const prepared = action.prepared?.from === browse
          && action.prepared.to.sid === action.sid
          && action.prepared.to.scopeKey === action.scopeKey
          && action.prepared.to.revision === action.revision
          && action.prepared.to.generation === browse.generation
          && action.prepared.to.viewId === action.viewId
          && action.prepared.to.windowEpoch === action.windowEpoch + 1
        ? action.prepared.to : null;
      const mutation = prepared
        ? { projection: prepared, evictedPages: [] }
        : appendNewerPage(browse, action.page, {
            expectedScopeKey: action.scopeKey,
            expectedViewId: action.viewId,
            expectedWindowEpoch: action.windowEpoch,
            expectedNewerPageKey: action.page.pageKey,
            protectedTurnIds: action.protectedTurnIds,
          });
      return mutation.projection === browse
        ? state : { ...state, historyBrowse: mutation.projection };
    }
    case "history_browse_newer_unavailable": {
      const browse = state.historyBrowse;
      if (!browse || state.focusedSid !== action.sid
          || browse.sid !== action.sid
          || browse.scopeKey !== action.scopeKey
          || browse.revision !== action.revision
          || browse.viewId !== action.viewId
          || browse.windowEpoch !== action.windowEpoch
          || (action.generation != null
            && browse.generation !== action.generation)) return state;
      const historyBrowse = markBrowseNewerUnavailable(browse, {
        expectedScopeKey: action.scopeKey,
        expectedViewId: action.viewId,
        expectedWindowEpoch: action.windowEpoch,
      });
      return historyBrowse === browse ? state : { ...state, historyBrowse };
    }
    case "history_browse_newer_settled": {
      const browse = state.historyBrowse;
      if (!browse || state.focusedSid !== action.sid
          || browse.sid !== action.sid
          || browse.scopeKey !== action.scopeKey
          || browse.revision !== action.revision
          || browse.viewId !== action.viewId
          || browse.windowEpoch !== action.windowEpoch
          || !browse.hasNewer
          || browse.newerPageKey !== action.pageKey
          || (action.generation != null
            && browse.generation !== action.generation)) return state;
      const historyBrowse = settleBrowsePageRequest(browse, {
        expectedScopeKey: action.scopeKey,
        expectedViewId: action.viewId,
        expectedWindowEpoch: action.windowEpoch,
      });
      return { ...state, historyBrowse };
    }
    case "history_browse_page_failed": {
      const browse = state.historyBrowse;
      if (!browse || state.focusedSid !== action.sid
          || browse.sid !== action.sid
          || browse.scopeKey !== action.scopeKey
          || browse.revision !== action.revision
          || browse.viewId !== action.viewId
          || browse.windowEpoch !== action.windowEpoch
          || browse.olderCursor !== action.before
          || (action.generation != null
            && browse.generation !== action.generation)) return state;
      const historyBrowse = settleBrowsePageRequest(browse, {
        expectedScopeKey: action.scopeKey,
        expectedViewId: action.viewId,
        expectedWindowEpoch: action.windowEpoch,
      });
      return { ...state, historyBrowse };
    }
    case "history_browse_detail_requested": {
      const browse = state.historyBrowse;
      if (!browse || state.focusedSid !== action.sid
          || browse.sid !== action.sid
          || browse.scopeKey !== action.scopeKey
          || browse.revision !== action.revision
          || browse.viewId !== action.viewId) return state;
      // Detail authority belongs to the revisioned browse view, not to one
      // particular page window. Older/newer pagination may legitimately
      // advance windowEpoch while the requested canonical turn remains
      // mounted; rejecting that late response would strand detailLoading.
      const target = browse.turns.find((turn) =>
        canonicalTurnId(turn) === action.turnId
        || turn.id === action.turnId);
      const historyBrowse = markBrowseDetailLoading(
        browse, action.turnId, true, {
          expectedScopeKey: action.scopeKey,
          expectedViewId: action.viewId,
        }, action.before == null ? true : undefined, null, {
          before: action.before ?? null,
          direction: action.before == null
            ? "initial"
            : action.before === target?.detailNewerCursor
              ? "newer" : "older",
        });
      return historyBrowse === browse ? state : { ...state, historyBrowse };
    }
    case "history_browse_detail": {
      const browse = state.historyBrowse;
      if (!browse || state.focusedSid !== action.sid
          || browse.sid !== action.sid
          || browse.scopeKey !== action.scopeKey
          || browse.revision !== action.revision
          || browse.viewId !== action.viewId
          || !browse.turns.some((turn) =>
            canonicalTurnId(turn) === action.turnId
            || turn.id === action.turnId)) return state;
      const target = browse.turns.find((turn) =>
        canonicalTurnId(turn) === action.turnId
        || turn.id === action.turnId);
      if (!target || action.events.length === 0) {
        const resetSucceeded = !!target
          && !!target.detailResetPending
          && action.before == null
          && !action.error;
        const historyBrowse = markBrowseDetailLoading(
          browse, action.turnId, false, {
            expectedScopeKey: action.scopeKey,
            expectedViewId: action.viewId,
          }, false, action.error ?? null, {
            before: action.resetRequired
              ? null : target?.detailRetryBefore ?? action.before ?? null,
            direction: action.resetRequired
              ? "initial"
              : target?.detailRetryDirection
                ?? (action.before == null
                  ? "initial"
                  : action.before === target?.detailNewerCursor
                    ? "newer" : "older"),
          }, action.resetRequired
            ? true
            : resetSucceeded ? false : target?.detailResetPending ?? false);
        return historyBrowse === browse ? state : { ...state, historyBrowse };
      }
      const installed = installTurnDetailProjectionPage(
        target.detailResetPending && action.before == null
          ? undefined : target.detailProjection,
        {
          before: action.before,
          events: action.events,
          hasMore: action.hasMore,
          oldestCursor: action.oldestCursor,
          hasNewer: action.hasNewer,
          newerCursor: action.newerCursor,
        },
        (events) => decodeTurnDetailEvents(
          state, action.sid, action.turnId, events),
      );
      const detailed = installed.detail;
      if (!detailed) {
        const historyBrowse = markBrowseDetailLoading(
          browse, action.turnId, false, {
            expectedScopeKey: action.scopeKey,
            expectedViewId: action.viewId,
          }, false, DETAIL_PARSE_ERROR, {
            before: target.detailRetryBefore ?? action.before ?? null,
            direction: target.detailRetryDirection
              ?? (action.before == null
                ? "initial"
                : action.before === target.detailNewerCursor
                  ? "newer" : "older"),
          }, target.detailResetPending ?? false);
        return historyBrowse === browse ? state : { ...state, historyBrowse };
      }
      const historyBrowse = markBrowseDetail(
        browse, action.turnId, detailed, {
          hasMore: !!action.hasMore,
          oldestCursor: action.oldestCursor,
          hasNewer: !!action.hasNewer,
          newerCursor: action.newerCursor,
        }, {
          expectedScopeKey: action.scopeKey,
          expectedViewId: action.viewId,
        }, installed.projection);
      return historyBrowse === browse ? state : { ...state, historyBrowse };
    }
    case "history_detail_cancelled": {
      const context = action.context;
      if (context.target === "browse") {
        const browse = state.historyBrowse;
        if (!browse || state.focusedSid !== context.sid
            || browse.sid !== context.sid
            || browse.scopeKey !== context.scopeKey
            || browse.revision !== context.revision
            || browse.viewId !== context.viewId) return state;
        const historyBrowse = markBrowseDetailLoading(
          browse, context.turnId, false, {
            expectedScopeKey: context.scopeKey,
            expectedViewId: context.viewId,
          }, false, undefined, null, false);
        return historyBrowse === browse ? state : { ...state, historyBrowse };
      }
      const runtime = state.runtimes[context.sid];
      if (!runtime || runtime.historyRevision !== context.revision) return state;
      return patch(state, context.sid, (rt) => {
        rt.turns = rt.turns.map((turn) => (
          turn.id === context.turnId
            || canonicalTurnId(turn) === context.turnId)
          ? {
              ...turn,
              detailLoading: false,
              detailAutoLoad: false,
              detailRetryBefore: undefined,
              detailRetryDirection: undefined,
              detailResetPending: false,
            }
          : turn);
      }, true);
    }
    case "return_to_latest":
      return state.historyBrowse?.sid === action.sid
          || state.retainedHistoryBrowse?.sid === action.sid
        ? {
            ...state,
            historyBrowse: state.historyBrowse?.sid === action.sid
              ? null : state.historyBrowse,
            retainedHistoryBrowse:
              state.retainedHistoryBrowse?.sid === action.sid
                ? null : state.retainedHistoryBrowse,
          }
        : state;
    case "hydrate_cache":
      // fill a session's turns from the IndexedDB cache for an INSTANT render;
      // only if still empty (never clobber live/streaming or already-replayed turns).
      return patch(state, action.sid, (rt) => {
        if (rt.historyInvalidated) return;
        const control = action.control
          && sessionControlTargetsSid(action.control, action.sid)
          ? action.control : null;
        const cacheGeneration =
          action.generation ?? control?.generation ?? null;
        const runtimeGeneration = runtimeOrderingGeneration(rt);
        const hasCachedProjection = action.revision != null
          || action.turns.length > 0
          || control != null
          || action.historyAtStart === true;
        const cacheGenerationMatches = runtimeGeneration == null
          || (cacheGeneration != null
            && cacheGeneration === runtimeGeneration);
        // IndexedDB is paint-only. Once a live Snapshot/History has established
        // this runtime's wrapper generation, an older (or unscoped) cache row
        // must not roll control/history ordering back to its saved generation.
        // The empty 6s fallback has no cached projection and still clears the
        // loader when both IndexedDB and the wrapper remain silent.
        if (hasCachedProjection && !cacheGenerationMatches) return;
        if (rt.turns.length === 0) {
          switchControlGeneration(
            rt, cacheGeneration);
          if (control) applySessionControl(rt, control);
          if (action.turns.length || action.historyAtStart === true) {
            rt.historyHeadKnown = action.historyAtStart === true;
            rt.hasMore = false;
            rt.oldestId = null;
            rt.historyRevision = action.revision;
            rt.historyGeneration = cacheGeneration;
          }
          if (action.turns.length) {
            replaceWithBoundedTurns(rt, cloneTurns(action.turns).map((turn) => (
              // Cache paint has no current lifecycle authority. Keep a Plan
              // provisionally open until the first accepted History page says
              // whether the enclosing native task is still running.
              finishCompletedTurnChildren(turn, true),
              !turn.forkPointId && turn.codexTurnId
                ? { ...turn, forkPointId: turn.codexTurnId }
                : turn
            )));
            rt.hydratedCacheTurnIds = action.turns.map((turn) => turn.id);
          }
        } else if (rt.turns.length > 0
            && action.revision != null
            && action.revision === rt.historyRevision
            && (cacheGeneration != null
              ? cacheGeneration === rt.historyGeneration
              : rt.historyGeneration == null)) {
          // IndexedDB and authoritative History race on focus. If History won,
          // accept only its exact revision/generation cache as temporary
          // process paint; prompt/final/lifecycle remain server-owned.
          if (control) {
            switchControlGeneration(rt, cacheGeneration);
            applySessionControl(rt, control);
          }
          const newerUnsettledLiveFrame = rt.lastLiveSeq
            > Math.max(rt.historyLiveSeq, rt.lastLifecycleSeq);
          const cacheRestoreAuthority = rt.state !== "idle"
              || rt.mirroredRunning
              || newerUnsettledLiveFrame
            ? "running" as const
            : "idle" as const;
          const activeCacheOwnerId = cacheRestoreAuthority === "running"
            ? rt.acceptanceKind === "query" && rt.acceptancePending
              ? rt.acceptancePending
              : (rt.liveOwner?.turnId ?? null)
            : null;
          rt.turns = restoreCachedTurnDetails(
            rt.turns, action.turns, cacheRestoreAuthority,
            activeCacheOwnerId);
        }
        applyPendingCodexTerminalFences(rt);
        reconcileAuthoritativeBackgroundProcessLevel(rt);
        rt.loading = false;
      }, true);
    case "prune_runtimes": {
      const protectedSids = new Set(action.protectedSids);
      if (state.focusedSid) protectedSids.add(state.focusedSid);
      for (const group of Object.values(state.btwByParentSid)) {
        for (const chat of group.chats) protectedSids.add(chat.sid);
      }
      if (state.artifact?.sid) protectedSids.add(state.artifact.sid);
      const runtimes = pruneRuntimeMap(state.runtimes, protectedSids);
      return runtimes === state.runtimes ? state : { ...state, runtimes };
    }
    case "answer_question":
      return patch(state, action.sid, (rt) => {
        if (rt.pendingQuestion?.ask_id === action.ask_id) {
          rt.pendingQuestion = null;
        }
      });
    case "dismiss_notice":
      return patch(state, action.sid, (rt) => {
        rt.notices = rt.notices.filter(
          (notice) => notice.notice_id !== action.noticeId);
      });
    case "enter_new_chat":
      return {
        ...state,
        historyRecovery: null,
        historyBrowse: null,
        retainedHistoryBrowse: null,
        newChat: {
          cwd: action.cwd,
          cwdSource: action.cwdSource ?? "default",
          model: action.model ?? null,
          effort: action.effort ?? null,
          autoCompactMode: action.autoCompactMode ?? "inherit",
          autoCompactThresholdTokens:
            action.autoCompactMode === "custom"
              ? action.autoCompactThresholdTokens ?? null
              : null,
          claudeProfileId: action.claudeProfileId ?? null,
          codexProfileId: action.codexProfileId ?? null,
        },
      };
    case "set_new_chat_cwd":
      return state.newChat ? { ...state, newChat: {
        ...state.newChat,
        cwd: action.cwd,
        cwdSource: action.cwdSource ?? "explicit",
      } } : state;
    case "set_new_chat_codex_profile": {
      if (!state.newChat
          || !state.codexProfiles.some((profile) => profile.id === action.profileId)) {
        return state;
      }
      return {
        ...state,
        codexProfileByScope: {
          ...state.codexProfileByScope,
          [action.scopeKey]: action.profileId,
        },
        newChat: {
          ...state.newChat,
          codexProfileId: action.profileId,
          // Catalog and execution controls are account-owned. Never carry an
          // explicit choice into another CODEX_HOME before its catalog arrives.
          model: null,
          effort: null,
        },
      };
    }
    case "set_new_chat_claude_profile": {
      if (!state.newChat
          || !state.claudeProfiles.some(
            (profile) => profile.id === action.profileId)) {
        return state;
      }
      return {
        ...state,
        claudeProfileByScope: {
          ...state.claudeProfileByScope,
          [action.scopeKey]: action.profileId,
        },
        newChat: {
          ...state.newChat,
          claudeProfileId: action.profileId,
          model: null,
          effort: null,
        },
      };
    }
    case "clear_scope_cwd": {
      if (!(action.scopeKey in state.cwdByScope)) return state;
      const cwdByScope = { ...state.cwdByScope };
      delete cwdByScope[action.scopeKey];
      return { ...state, cwdByScope };
    }
    case "set_new_chat_model":
      return state.newChat ? { ...state, newChat: { ...state.newChat, model: action.model } } : state;
    case "set_new_chat_effort":
      return state.newChat ? { ...state, newChat: { ...state.newChat, effort: action.effort } } : state;
    case "set_new_chat_auto_compact":
      return state.newChat ? { ...state, newChat: {
        ...state.newChat,
        autoCompactMode: action.mode,
        autoCompactThresholdTokens:
          action.mode === "custom" ? action.thresholdTokens : null,
      } } : state;
    case "set_new_chat_selection":
      return state.newChat ? { ...state, newChat: {
        ...state.newChat,
        model: action.model,
        effort: action.effort,
      } } : state;
    case "exit_new_chat":
      return { ...state, newChat: null };
    case "event":
      return reduceEvent(state, action.event, true, action.ownership);
  }
}

function reduceEvent(
  state: AppState, e: ServerEvent, boundCompletedTurns = true,
  ownership?: EventOwnership,
): AppState {
  // A close can race a reconnect replay. Once the authoritative catalog no
  // longer owns a btw-* sid, late Snapshot/narrative frames must not recreate
  // an empty ghost runtime after BtwClosed removed it.
  if (e.sid?.startsWith("btw-")
      && e.type !== "btw_opened" && e.type !== "btw_closed"
      && !Object.values(state.btwByParentSid).some((group) =>
        group.chats.some((chat) => chat.sid === e.sid))) {
    return state;
  }
  // History is built asynchronously. Any newer replayable frame — including a
  // state/ownership update with no message block — makes an older History
  // envelope stale for control state. Narrative event reducers also advance
  // this watermark via markTurnAsLive; doing it once here covers the non-turn
  // frames which previously let stale `external=true` resurrect read-only mode.
  if (boundCompletedTurns && e.type !== "history"
      && typeof e.seq === "number") {
    state = patch(state, e.sid, (rt) => {
      rt.lastLiveSeq = Math.max(rt.lastLiveSeq, e.seq!);
    });
  }
  if (boundCompletedTurns && e.sid
      && state.historyBrowse?.sid === e.sid
      && [
        "user_msg", "assistant_msg_start", "delta", "tool_use",
        "tool_delta", "tool_result", "assistant_msg_end", "process",
        "turn_end", "error",
      ].includes(e.type)) {
    state = {
      ...state,
      historyBrowse: markBrowseLatestDirty(state.historyBrowse),
    };
  }
  switch (e.type) {
    case "btw_sync": {
      if (e.revision < state.btwRevision) return state;
      const oldChats = new Set(Object.values(state.btwByParentSid).flatMap(
        (group) => group.chats.map((chat) => chat.sid)));
      const grouped: Record<string, BtwChat[]> = {};
      for (const item of e.sessions) {
        const chats = grouped[item.parent_sid] ?? [];
        if (!chats.some((chat) => chat.sid === item.btw_sid)) {
          chats.push({
            sid: item.btw_sid,
            engine: item.engine,
            createdAt: item.created_at,
            state: item.state ?? "idle",
          });
        }
        grouped[item.parent_sid] = chats;
      }
      const btwByParentSid: Record<string, BtwGroup> = {};
      const runtimes = { ...state.runtimes };
      const retained = new Set<string>();
      for (const [parentSid, unsorted] of Object.entries(grouped)) {
        const chats = [...unsorted].sort((a, b) =>
          a.createdAt - b.createdAt || a.sid.localeCompare(b.sid));
        for (const chat of chats) {
          retained.add(chat.sid);
          const runtime = runtimes[chat.sid];
          // A sequenced live State is newer than a catalog snapshot that can
          // race it on an already-synced socket. After disconnect every runtime
          // is marked unsynced, so the same catalog becomes the authoritative
          // reconnect seed until SyncBtw supplies its selected-chat Snapshot.
          runtimes[chat.sid] = runtime
            ? runtime.syncReady ? runtime : { ...runtime, state: chat.state }
            : { ...createRuntime(), state: chat.state };
        }
        const previous = state.btwByParentSid[parentSid];
        const activeSid = previous
          && chats.some((chat) => chat.sid === previous.activeSid)
          ? previous.activeSid : chats[chats.length - 1].sid;
        btwByParentSid[parentSid] = { chats, activeSid };
      }
      for (const oldSid of oldChats) {
        if (!retained.has(oldSid)) delete runtimes[oldSid];
      }
      return {
        ...state,
        btwByParentSid,
        btwRevision: e.revision,
        runtimes,
      };
    }
    case "snapshot": {
      // Per-session: the frame's sid is the runtime key; cc_session_id is the
      // real cc id (may still be null while a brand-new session's id is captured).
      const key = e.sid ?? e.cc_session_id ?? state.focusedSid;
      if (!key) return state;
      // A snapshot may belong to a background engine/space. It hydrates that
      // runtime but never moves focus; the accepted session list drives the
      // initial explicit switch.
      return { ...patch(state, key, (rt) => {
        switchControlGeneration(rt, e.generation);
        rt.state = e.state;
        rt.syncReady = true;
        rt.ccSessionId = e.cc_session_id ?? rt.ccSessionId;
        if (e.control && sessionControlTargetsSid(e.control, key)) {
          applySessionControl(rt, e.control);
        }
      }, true), focusedSid: state.focusedSid, wrapperOnline: true };
    }
    case "session_focus": {
      // NON-destructive, focus-ONLY view change. Runtime key migration on
      // id-capture is handled by session_rekey — keeping it out of here is what
      // stops a background session's id-capture from stealing the user's view.
      const newF = e.session_id;
      // A focus acknowledgement confirms routing, not transcript completeness.
      // Keep a genuinely unknown empty head loading until cache/History arrives;
      // a previously confirmed empty session remains immediately paintable.
      const base = state.runtimes[newF] ?? createRuntime();
      const runtimes = {
        ...state.runtimes,
        [newF]: {
          ...base,
          loading: base.historyInvalidated
            || (base.turns.length === 0 && !base.historyHeadKnown),
          syncReady: true,
        },
      };
      const cwdByScope = ownership && e.cwd
        ? { ...state.cwdByScope, [ownership.scopeKey]: e.cwd }
        : state.cwdByScope;
      const hasSession = state.sessions.some(
        (session) => session.session_id === newF);
      const sessions = e.request_id && ownership && !hasSession
        ? [{
          session_id: newF,
          summary: "新会话",
          last_modified: String(e.ts),
          cwd: e.cwd,
          state: base.state,
          engine: ownership.engine,
          space: ownership.space,
          claude_profile_id: ownership.engine === "claude"
            ? ownership.claudeProfileId ?? undefined
            : undefined,
          codex_profile_id: ownership.engine === "codex"
            ? ownership.codexProfileId ?? undefined
            : undefined,
          native_session_id: newF.includes("@")
            ? nativeProfileSessionId(newF)
            : undefined,
        } satisfies SessionInfo, ...state.sessions]
        : state.sessions;
      return {
        ...state, focusedSid: newF, runtimes, sessions,
        historyRecovery: state.historyRecovery?.sid === newF
          ? state.historyRecovery : null,
        historyBrowse: state.focusedSid === newF
          ? state.historyBrowse : null,
        retainedHistoryBrowse: state.retainedHistoryBrowse?.sid === newF
          ? state.retainedHistoryBrowse : null,
        artifact: state.focusedSid && state.focusedSid !== newF ? null : state.artifact,
        cwdByScope,
      };
    }
    case "session_rekey": {
      // A temp-keyed new session captured its real cc id. Rename the runtime
      // old_key -> session_id; focus follows ONLY if we were viewing old_key
      // (so a BACKGROUND new session's capture never yanks the current view).
      const { old_key, session_id } = e;
      if (old_key === session_id) return state;
      const runtimes = { ...state.runtimes };
      let rekeyGenerationConflict = false;
      if (runtimes[old_key]) {
        const source = runtimes[old_key];
        const target = runtimes[session_id];
        if (target) {
          const sourceOrderingGeneration = source.controlGeneration
            ?? source.historyGeneration;
          const targetOrderingGeneration = target.controlGeneration
            ?? target.historyGeneration;
          const orderingGenerationConflict = sourceOrderingGeneration != null
            && targetOrderingGeneration != null
            && sourceOrderingGeneration !== targetOrderingGeneration;
          rekeyGenerationConflict = orderingGenerationConflict;
          const mergeTarget = orderingGenerationConflict ? source : target;
          // The temp runtime belongs to the generation which captured the real
          // id. A durable runtime from an older wrapper cannot contribute its
          // transcript, wrapper-owned queue or ordering watermarks.
          const mergedTurns = orderingGenerationConflict
            ? [] : [...target.turns];
          const usedTargetIndexes = new Set<number>();
          for (let sourceIndex = 0; sourceIndex < source.turns.length;
            sourceIndex += 1) {
            const sourceTurn = source.turns[sourceIndex];
            const targetIndex = mergedTurns.findIndex((targetTurn, index) =>
              !usedTargetIndexes.has(index)
              && turnsShareIdentityAlias(targetTurn, sourceTurn));
            if (targetIndex < 0) {
              mergedTurns.push(sourceTurn);
              continue;
            }
            const merged = mergeInitialHistory(
              [mergedTurns[targetIndex]],
              [sourceTurn],
              {
                preserveLiveTailOpen:
                  sourceIndex === source.turns.length - 1
                  && source.state !== "idle"
                  && !sourceTurn.done,
              },
            )[0];
            if (merged) mergedTurns[targetIndex] = merged;
            usedTargetIndexes.add(targetIndex);
          }
          const mergedControlGeneration = source.controlGeneration
            ?? mergeTarget.controlGeneration ?? sourceOrderingGeneration;
          const mergedControl = source.controlGeneration
                && source.controlGeneration !== mergeTarget.controlGeneration
            ? source.control
            : newestSessionControl(mergeTarget.control, source.control);
          const mergedHistoryRuntime = source.historyRevision == null
            ? mergeTarget : source;
          const mergedHistoryInvalidated =
            mergeTarget.historyInvalidated || source.historyInvalidated;
          const lifecycleRuntime =
            source.lastLifecycleSeq > mergeTarget.lastLifecycleSeq
              ? source
              : mergeTarget.lastLifecycleSeq > source.lastLifecycleSeq
                ? mergeTarget
                : source.state !== "idle" && mergeTarget.state === "idle"
                  ? source
                  : mergeTarget.state !== "idle" && source.state === "idle"
                    ? mergeTarget
                    : source;
          const sameLiveOrderingScope = sourceOrderingGeneration != null
            && sourceOrderingGeneration === (
              mergeTarget.controlGeneration ?? mergeTarget.historyGeneration);
          const sameHistoryOrderingScope = sameLiveOrderingScope
            && source.historyRevision != null
            && source.historyRevision === mergeTarget.historyRevision;
          const sourceLiveTaskOwner = source.liveOwner;
          const targetLiveTaskOwner = mergeTarget.liveOwner;
          const mergedLiveTaskOwner = sameLiveOrderingScope
              && targetLiveTaskOwner && (!sourceLiveTaskOwner
                || targetLiveTaskOwner.seq > sourceLiveTaskOwner.seq)
            ? targetLiveTaskOwner : sourceLiveTaskOwner ?? targetLiveTaskOwner;
          const sourcePendingBinding = source.pendingLiveBinding;
          const targetPendingBinding = mergeTarget.pendingLiveBinding;
          const mergedPendingBinding = sameLiveOrderingScope
              && targetPendingBinding && (!sourcePendingBinding
                || targetPendingBinding.seq > sourcePendingBinding.seq)
            ? targetPendingBinding
            : sourcePendingBinding ?? targetPendingBinding;
          const mergedRuntime: SessionRuntime = {
            ...target,
            ...source,
            control: null,
            controlGeneration: null,
            hasRevisionedControl:
              orderingGenerationConflict
                ? source.hasRevisionedControl
                : target.hasRevisionedControl || source.hasRevisionedControl,
            state: lifecycleRuntime.state,
            mirroredRunning: lifecycleRuntime.mirroredRunning,
            syncReady: mergeTarget.syncReady || source.syncReady,
            loading: mergedHistoryInvalidated
              ? true : mergedHistoryRuntime.loading,
            historyInvalidated: mergedHistoryInvalidated,
            historyRevision:
              source.historyRevision ?? mergeTarget.historyRevision,
            historyContinuityRevision: mergedHistoryRuntime.historyContinuityRevision,
            pendingHistoryRevision:
              source.pendingHistoryRevision ?? mergeTarget.pendingHistoryRevision,
            historyBuildSeq: source.historyRevision == null
              ? mergeTarget.historyBuildSeq
              : sameHistoryOrderingScope
                ? Math.max(
                    source.historyBuildSeq, mergeTarget.historyBuildSeq)
                : source.historyBuildSeq,
            historyLiveSeq: source.historyRevision == null
              ? mergeTarget.historyLiveSeq
              : sameHistoryOrderingScope
                ? Math.max(
                    source.historyLiveSeq, mergeTarget.historyLiveSeq)
                : source.historyLiveSeq,
            historyFence: source.historyRevision == null
              ? mergeTarget.historyFence
              : sameHistoryOrderingScope
                ? source.historyFence == null
                  ? mergeTarget.historyFence
                  : mergeTarget.historyFence == null
                    ? source.historyFence
                    : Math.max(
                        source.historyFence,
                        mergeTarget.historyFence,
                      )
                : source.historyFence,
            liveOwner: mergedLiveTaskOwner,
            pendingLiveBinding: mergedPendingBinding,
            pendingTerminalFences: orderingGenerationConflict
              ? source.pendingTerminalFences
              : mergePendingTerminalFences(
                  source.pendingTerminalFences,
                  mergeTarget.pendingTerminalFences,
                ),
            historyGeneration: source.historyRevision == null
              ? mergeTarget.historyGeneration : source.historyGeneration,
            pendingHistoryGeneration:
              source.pendingHistoryGeneration
                ?? mergeTarget.pendingHistoryGeneration,
            pendingHistoryCandidateBuildSeq: source.historyInvalidated
              ? source.pendingHistoryCandidateBuildSeq
              : mergeTarget.pendingHistoryCandidateBuildSeq,
            historyNewestId: source.historyRevision == null
              ? mergeTarget.historyNewestId : source.historyNewestId,
            historyHeadKnown: mergedHistoryInvalidated
              ? false : mergedHistoryRuntime.historyHeadKnown,
            lastLiveSeq: Math.max(
              source.lastLiveSeq, mergeTarget.lastLiveSeq),
            lastLifecycleSeq: Math.max(
              source.lastLifecycleSeq, mergeTarget.lastLifecycleSeq),
            hydratedCacheTurnIds: Array.from(new Set([
                  ...mergeTarget.hydratedCacheTurnIds,
                  ...source.hydratedCacheTurnIds,
                ])),
            liveDetailTurnIds: Array.from(new Set([
                  ...mergeTarget.liveDetailTurnIds,
                  ...source.liveDetailTurnIds,
                ])).slice(-MAX_LIVE_DETAIL_TURN_IDS),
            ccSessionId: session_id,
            turns: mergedTurns,
            queue: orderingGenerationConflict
              ? [...source.queue] : [...source.queue, ...target.queue],
            pendingSend: source.pendingSend ?? mergeTarget.pendingSend,
            failedDeferred: [
              ...source.failedDeferred,
              ...mergeTarget.failedDeferred.filter((query) => (
                !source.failedDeferred.some(
                  (sourceQuery) => sourceQuery.msg_id === query.msg_id)
              )),
            ],
            acceptancePending:
              source.acceptancePending ?? mergeTarget.acceptancePending,
            acceptanceKind: source.acceptancePending
              ? source.acceptanceKind : mergeTarget.acceptanceKind,
            acceptanceHistoryBaseline: source.acceptancePending
              ? source.acceptanceHistoryBaseline
              : mergeTarget.acceptanceHistoryBaseline,
            notices: mergeNotices(mergeTarget.notices, source.notices),
          };
          switchControlGeneration(mergedRuntime, mergedControlGeneration);
          if (mergedControl) applySessionControl(mergedRuntime, mergedControl);
          replaceWithBoundedTurns(mergedRuntime, mergedTurns);
          reconcileAuthoritativeBackgroundProcessLevel(mergedRuntime);
          // switchControlGeneration intentionally clears cross-generation
          // sequence evidence. Restore only the owner already filtered to the
          // source generation above, then bind it to a row which survived the
          // bounded merge.
          mergedRuntime.liveOwner = remapExplicitLiveTaskOwner(
            mergedLiveTaskOwner, mergedRuntime.turns);
          if (mergedRuntime.pendingTerminalFences) {
            const pending = mergedRuntime.pendingTerminalFences;
            installCodexTerminalFences(
              mergedRuntime,
              undefined,
              {
                revision: pending.revision,
                generation: pending.generation,
              },
            );
          }
          runtimes[session_id] = mergedRuntime;
        } else {
          runtimes[session_id] = { ...source, ccSessionId: session_id };
        }
        delete runtimes[old_key];
      } else if (!runtimes[session_id]) {
        runtimes[session_id] = createRuntime();
      }
      const wasFocused = state.focusedSid === old_key;
      const sourceSession = state.sessions.find(
        (session) => session.session_id === old_key);
      const targetSession = state.sessions.find(
        (session) => session.session_id === session_id);
      const sessions = sourceSession
        ? [
          ...state.sessions.filter((session) => (
            session.session_id !== old_key
            && session.session_id !== session_id
          )),
          {
            ...sourceSession,
            ...targetSession,
            session_id,
            claude_profile_id: ownership?.engine === "claude"
              ? ownership.claudeProfileId
                ?? targetSession?.claude_profile_id
                ?? sourceSession.claude_profile_id
              : targetSession?.claude_profile_id
                ?? sourceSession.claude_profile_id,
            codex_profile_id: ownership?.engine === "codex"
              ? ownership.codexProfileId
                ?? targetSession?.codex_profile_id
                ?? sourceSession.codex_profile_id
              : targetSession?.codex_profile_id
                ?? sourceSession.codex_profile_id,
            native_session_id: session_id.includes("@")
              ? nativeProfileSessionId(session_id)
              : targetSession?.native_session_id
                ?? sourceSession.native_session_id,
            cwd: e.cwd ?? targetSession?.cwd ?? sourceSession.cwd,
          },
        ]
        : state.sessions;
      const cwdByScope = ownership && e.cwd
        ? { ...state.cwdByScope, [ownership.scopeKey]: e.cwd }
        : state.cwdByScope;
      let btwByParentSid = state.btwByParentSid;
      const parentBtw = btwByParentSid[old_key];
      if (parentBtw) {
        const targetBtw = btwByParentSid[session_id];
        btwByParentSid = { ...btwByParentSid };
        if (!targetBtw) {
          btwByParentSid[session_id] = parentBtw;
        } else {
          const chats = [...targetBtw.chats];
          for (const chat of parentBtw.chats) {
            if (!chats.some((candidate) => candidate.sid === chat.sid)) {
              chats.push(chat);
            }
          }
          chats.sort((a, b) => a.createdAt - b.createdAt
            || a.sid.localeCompare(b.sid));
          btwByParentSid[session_id] = {
            chats,
            activeSid: targetBtw.activeSid || parentBtw.activeSid,
          };
        }
        delete btwByParentSid[old_key];
      }
      const historyRecovery = state.historyRecovery?.sid === old_key
        ? { ...state.historyRecovery, sid: session_id }
        : rekeyGenerationConflict
            && state.historyRecovery?.sid === session_id
          ? null : state.historyRecovery;
      // A new session cannot have deep authoritative history yet. Keeping a
      // temp-keyed browse projection through re-key would also leave its page
      // cache under the wrong durable identity.
      const historyBrowse = state.historyBrowse?.sid === old_key
          || (rekeyGenerationConflict
            && state.historyBrowse?.sid === session_id)
        ? null : state.historyBrowse;
      const retainedHistoryBrowse =
        state.retainedHistoryBrowse?.sid === old_key
          || (rekeyGenerationConflict
            && state.retainedHistoryBrowse?.sid === session_id)
          ? null : state.retainedHistoryBrowse;
      const artifact = state.artifact?.sid === old_key
        ? { ...state.artifact, sid: session_id }
        : state.artifact;
      return {
        ...state,
        runtimes, sessions, historyRecovery, historyBrowse,
        retainedHistoryBrowse,
        focusedSid: wasFocused ? session_id : state.focusedSid,
        btwByParentSid,
        cwdByScope,
        artifact,
      };
    }
    case "session_migrated": {
      let changed = false;
      const sessions = state.sessions.map((session) => {
        if (session.session_id !== e.session_id || session.cwd === e.cwd) {
          return session;
        }
        changed = true;
        return { ...session, cwd: e.cwd };
      });
      const updateFocusedScope = state.focusedSid === e.session_id
        && ownership !== undefined;
      const cwdByScope = updateFocusedScope
        ? { ...state.cwdByScope, [ownership.scopeKey]: e.cwd }
        : state.cwdByScope;
      const artifact = state.artifact?.sid === e.session_id
        ? null : state.artifact;
      if (!changed && cwdByScope === state.cwdByScope
          && artifact === state.artifact) return state;
      return { ...state, sessions, cwdByScope, artifact };
    }
    case "session_list": {
      const normalized = normalizeSessionList(
        state.sessions,
        state.codexProfiles,
        state.defaultCodexProfileId,
        e,
        state.claudeProfiles,
        state.defaultClaudeProfileId,
      );
      const {
        sessions,
        claudeProfiles,
        defaultClaudeProfileId,
        codexProfiles,
        defaultCodexProfileId,
      } = normalized;
      let claudeProfileByScope = state.claudeProfileByScope;
      let codexProfileByScope = state.codexProfileByScope;
      let selectedClaudeProfileId: string | null = null;
      let selectedCodexProfileId: string | null = null;
      if (e.engine === "codex" && ownership && codexProfiles.length > 0) {
        const known = new Set(codexProfiles.map((profile) => profile.id));
        const preferred = state.newChat?.codexProfileId
          ?? state.codexProfileByScope[ownership.scopeKey]
          ?? defaultCodexProfileId;
        // A non-null selection is user/account ownership state, not a catalog
        // fallback hint. Preserve a removed id so the composer can stop and ask
        // for an explicit replacement instead of silently using the default.
        selectedCodexProfileId = preferred
          ? preferred
          : defaultCodexProfileId && known.has(defaultCodexProfileId)
            ? defaultCodexProfileId
            : codexProfiles[0].id;
        if (state.codexProfileByScope[ownership.scopeKey]
            !== selectedCodexProfileId) {
          codexProfileByScope = {
            ...state.codexProfileByScope,
            [ownership.scopeKey]: selectedCodexProfileId,
          };
        }
      }
      if (e.engine === "claude" && ownership && claudeProfiles.length > 0) {
        const known = new Set(claudeProfiles.map((profile) => profile.id));
        const preferred = state.newChat?.claudeProfileId
          ?? state.claudeProfileByScope[ownership.scopeKey]
          ?? defaultClaudeProfileId;
        selectedClaudeProfileId = preferred
          ? preferred
          : defaultClaudeProfileId && known.has(defaultClaudeProfileId)
            ? defaultClaudeProfileId
            : claudeProfiles[0].id;
        if (state.claudeProfileByScope[ownership.scopeKey]
            !== selectedClaudeProfileId) {
          claudeProfileByScope = {
            ...state.claudeProfileByScope,
            [ownership.scopeKey]: selectedClaudeProfileId,
          };
        }
      }
      const focusedMissing = !!state.focusedSid
        && !state.focusedSid.startsWith("tmp-")
        && !sessions.some((session) => session.session_id === state.focusedSid);
      const focusedSession = ownership && state.focusedSid
        ? sessions.find(
          (session) => session.session_id === state.focusedSid)
        : undefined;
      // Another client may migrate the focused session while this tab is
      // offline. The live SessionMigrated frame is then unavailable, so repair
      // the scope default from the authoritative reconnect catalog as well.
      const cwdByScope = ownership && focusedSession?.cwd
          && state.cwdByScope[ownership.scopeKey] !== focusedSession.cwd
        ? { ...state.cwdByScope, [ownership.scopeKey]: focusedSession.cwd }
        : state.cwdByScope;
      // A New Chat cwd is user-owned draft state. In particular, the sidebar
      // intentionally leaves the former focusedSid in memory while the draft
      // is open; a later authoritative list may report that old focus missing.
      // That must not replace an explicitly selected directory with `~`.
      let replacementNewChat = state.newChat;
      if (replacementNewChat && e.engine === "codex"
          && selectedCodexProfileId
          && replacementNewChat.codexProfileId !== selectedCodexProfileId) {
        replacementNewChat = {
          ...replacementNewChat,
          codexProfileId: selectedCodexProfileId,
          model: null,
          effort: null,
        };
      } else if (replacementNewChat && e.engine === "claude"
          && selectedClaudeProfileId
          && replacementNewChat.claudeProfileId
            !== selectedClaudeProfileId) {
        replacementNewChat = {
          ...replacementNewChat,
          claudeProfileId: selectedClaudeProfileId,
          model: null,
          effort: null,
        };
      } else if (focusedMissing && !replacementNewChat) {
        replacementNewChat = {
          cwd: (ownership
            ? state.cwdByScope[ownership.scopeKey] : "") || "~",
          cwdSource: (ownership
            && !!state.cwdByScope[ownership.scopeKey]
              ? "inherited" : "default") as "inherited" | "default",
          model: null,
          effort: null,
          autoCompactMode: "inherit",
          autoCompactThresholdTokens: null,
          claudeProfileId: selectedClaudeProfileId,
          codexProfileId: selectedCodexProfileId,
        };
      }
      let runtimes = state.runtimes;
      for (const session of sessions) {
        if (session.completion_revision == null
            || session.completion_unread == null) continue;
        const current = runtimes[session.session_id];
        // The sidebar reads cold receipts directly from the catalog. Only
        // merge the revision into an already-resident runtime; allocating one
        // placeholder per unread row would either defeat the memory bound or
        // let pruning make durable badges disappear immediately.
        if (!current) continue;
        const runtime = { ...current };
        applyCompletionProjection(runtime, {
          id: session.completion_id ?? null,
          unread: session.completion_unread,
          revision: session.completion_revision,
        });
        if (runtime.completion !== current?.completion) {
          if (runtimes === state.runtimes) runtimes = { ...state.runtimes };
          runtimes[session.session_id] = runtime;
        }
      }
      return {
        ...state,
        sessions,
        runtimes,
        cwdByScope,
        claudeProfiles,
        defaultClaudeProfileId,
        claudeProfileByScope,
        codexProfiles,
        defaultCodexProfileId,
        codexProfileByScope,
        focusedSid: focusedMissing ? null : state.focusedSid,
        historyRecovery: focusedMissing ? null : state.historyRecovery,
        historyBrowse: focusedMissing ? null : state.historyBrowse,
        retainedHistoryBrowse: focusedMissing
          ? null : state.retainedHistoryBrowse,
        newChat: replacementNewChat,
      };
    }
    case "session_activity": {
      let changed = false;
      const sessions = state.sessions.map((session) => {
        if (session.session_id !== e.session_id || session.state === e.state) {
          return session;
        }
        changed = true;
        return { ...session, state: e.state };
      });
      return changed ? { ...state, sessions } : state;
    }
    case "work_dashboard":
    case "work_artifacts":
      // Work dashboard state is owned by App because it is engine-scoped and
      // artifact inventories are owned by App because both are intentionally
      // independent from the focused conversation runtime.
      return state;
    case "history_invalidated": {
      let next = patch(state, e.session_id, (rt) => {
        // This small frame is replayable even when the authoritative History
        // replacement is too large for the bounded ring. Empty stale turns
        // immediately; the following live/history refresh rebuilds from the
        // engine transcript without resurrecting removed messages.
        rt.turns = [];
        rt.pendingQuestion = null;
        rt.hasMore = false;
        rt.oldestId = null;
        rt.truncated = false;
        rt.historyInvalidated = true;
        rt.historyContinuityRevision = null;
        rt.pendingHistoryRevision = e.revision;
        rt.historyNewestId = null;
        rt.historyHeadKnown = false;
        // Keep the accepted generation until replacement arrives: a slow
        // pre-rollback build from that same generation must remain rejectable.
        rt.historyBuildSeq = 0;
        rt.historyLiveSeq = 0;
        rt.historyFence = null;
        rt.liveOwner = null;
        rt.pendingLiveBinding = null;
        rt.pendingTerminalFences = null;
        rt.hasLoadedOlderHistory = false;
        rt.hydratedCacheTurnIds = [];
        rt.liveDetailTurnIds = [];
        rt.loading = true;
      }, true);
      if (next.historyRecovery?.sid === e.session_id) {
        next = { ...next, historyRecovery: null };
      }
      if (next.historyBrowse?.sid === e.session_id
          || next.retainedHistoryBrowse?.sid === e.session_id) {
        next = {
          ...next,
          historyBrowse: next.historyBrowse?.sid === e.session_id
            ? null : next.historyBrowse,
          retainedHistoryBrowse:
            next.retainedHistoryBrowse?.sid === e.session_id
              ? null : next.retainedHistoryBrowse,
        };
      }
      return next.artifact?.sid === e.session_id
        ? { ...next, artifact: null }
        : next;
    }
    case "artifact_invalidated":
      return state.artifact?.sid === e.session_id
        ? { ...state, artifact: null }
        : state;
    case "history": {
      // Bulk on-demand history (one frame, read from the transcript — like a web
      // chat's GET /conversation). Rebuild this session's COMPLETED turns by
      // running the events through a throwaway empty runtime: this reuses the
      // per-event reduce logic verbatim so deltas accumulate EXACTLY ONCE (never
      // double-appending over cache-hydrated or live text). Any not-yet-done turn
      // already in the runtime (an in-flight turn still streaming live, not yet in
      // the transcript) is preserved and appended after the rebuilt history.
      const sid = e.session_id;
      if (e.before) {
        // Older pages are requester-correlated, display-only browse data.
        // App freezes machine/surface/view authority at request time and routes
        // them through install_history_browse_page. Letting an uncorrelated
        // page reach this generic path would mutate live/control watermarks and
        // reintroduce the very history/runtime coupling the sliding window
        // removes.
        return state;
      }
      const preControlBase = state.runtimes[sid] ?? createRuntime();
      if (preControlBase.pendingHistoryGeneration
          && e.generation !== preControlBase.pendingHistoryGeneration) {
        return state;
      }
      if (e.authoritative !== false
          && isHistoryRecoveryPending(state.historyRecovery, sid)
          && !historyMatchesRecovery(state.historyRecovery, e)) {
        return state;
      }
      const sameBuildGeneration = e.generation != null
        ? preControlBase.historyGeneration === e.generation
        : preControlBase.historyGeneration == null
          && preControlBase.historyRevision === e.revision;
      const staleHistoryBuild = e.build_seq != null
        && sameBuildGeneration
        && e.build_seq < preControlBase.historyBuildSeq;
      const runtimeRecoveryPending =
        isRuntimeHistoryRecoveryPending(preControlBase);
      if (e.authoritative !== false && runtimeRecoveryPending) {
        if (!historyMatchesRuntimeRecovery(preControlBase, e)) return state;
        if (historyNeedsConfirmationRequest(preControlBase, e)) {
          let next = patch(state, sid, (rt) => {
            rt.pendingHistoryCandidateBuildSeq = e.build_seq ?? 0;
          }, true);
          if (isHistoryRecoveryPending(next.historyRecovery, sid)) {
            next = {
              ...next,
              historyRecovery: advanceHistoryRecovery(next.historyRecovery, e),
            };
          }
          // The first matching build after ReplayStart may itself have started
          // before the replay gap was known. It advances only lightweight
          // confirmation state. Do not install narrative/control state or clear
          // loading/invalidation: navigation can release the display copy, and
          // the canonical runtime must still be unmistakably unconfirmed.
          return next;
        }
        if (!historyConfirmsRuntimeRecovery(preControlBase, e)) return state;
      }
      if (e.authoritative !== false
          && isHistoryRecoveryPending(state.historyRecovery, sid)
          && state.historyRecovery!.candidateBuildSeq != null
          && !historyConfirmsRecovery(state.historyRecovery, e)) {
        // A duplicate/equal build cannot be installed behind a sampled-prefix
        // preview. Only the wrapper's strictly newer exact refresh is canonical.
        return state;
      }
      // Control has its own monotonic revision and remains authoritative even
      // when this History page later loses a transcript build/live race. Apply
      // it before any narrative early-return.
      if (e.control && sessionControlTargetsSid(e.control, sid)) {
        state = patch(state, sid, (rt) => {
          switchControlGeneration(rt, e.generation);
          applySessionControl(rt, e.control!);
        }, true);
      } else if (e.generation) {
        state = patch(state, sid, (rt) => {
          switchControlGeneration(rt, e.generation);
        }, true);
      }
      if (e.terminal_fences !== undefined
          && (!preControlBase.pendingHistoryRevision
            || e.revision === preControlBase.pendingHistoryRevision)
          && (!staleHistoryBuild
            || preControlBase.historyRevision === e.revision)) {
        // Terminal authority is independent from narrative freshness. Even a
        // sampled/stale content page may close an exact already-painted native
        // turn; unmatched fences remain revision-scoped until that identity
        // arrives. No lifecycle/state or notification receipt is changed here.
        state = patch(state, sid, (rt) => {
          installCodexTerminalFences(
            rt,
            e.terminal_fences,
            {
              revision: e.revision,
              generation: e.generation ?? rt.historyGeneration,
              continuationTurnIds:
                e.compaction_continuation_turn_ids ?? [],
              replaceSnapshot: !staleHistoryBuild,
            },
          );
        }, true);
      }
      if (staleHistoryBuild) return state;
      const base = state.runtimes[sid] ?? createRuntime();
      // build_seq orders newest-page reads only within the same boot-scoped
      // revision. A restart legitimately resets the sequence while changing
      // revision. Pagination remains revision/cursor based: another client's
      // targeted newest-page read can advance the wrapper's build sequence
      // without ever being routed to this browser.
      let scratch: AppState = {
        ...state, banner: undefined, runtimes: { [sid]: createRuntime() },
      };
      for (const ev of e.events) {
        scratch = reduceEvent(scratch, ev as ServerEvent, false);
      }
      const built = scratch.runtimes[sid] ?? createRuntime();
      if (e.detail === "summary" && Array.isArray(e.turns)) {
        built.turns = e.turns.map((turn) => ({
          ...turn,
          fileChangesTurnId: turn.fileChanges ? turn.id : undefined,
          clientMsgId: turn.clientMsgId ?? undefined,
          blocks: turn.blocks as Turn["blocks"],
          forkPointId: turn.forkPointId ?? undefined,
          checkpointId: turn.checkpointId ?? undefined,
          interrupted: turn.interrupted ?? undefined,
          error: turn.error ?? undefined,
          images: turn.images ?? undefined,
          imageRefs: turn.imageRefs ?? undefined,
          files: turn.files ?? undefined,
          ts: turn.ts ?? undefined,
          doneTs: turn.doneTs ?? undefined,
          durationMs: turn.durationMs ?? undefined,
          processDetailState: turn.processDetailState ?? undefined,
          detailReasons: turn.detailReasons
            ? [...turn.detailReasons] : undefined,
          processStartedTs: turn.processStartedTs ?? undefined,
          processDoneTs: turn.processDoneTs ?? undefined,
        }));
      }
      if (e.authoritative === false) {
        const provisional = !e.error && built.turns.length > 0;
        const pendingRecovery = isHistoryRecoveryPending(
          state.historyRecovery, sid);
        const coldPreview = provisional
          && base.turns.length === 0
          && state.focusedSid === sid
          && !state.newChat;
        let next = patch(state, sid, (rt) => {
          // A failed read stops an ordinary loading attempt. A populated stale
          // append-prefix may keep an otherwise empty first screen readable,
          // but a trusted current runtime remains fully interactive while the
          // wrapper's already-scheduled exact refresh runs in the background.
          rt.loading = pendingRecovery || coldPreview;
        }, true);
        if (coldPreview) {
          // Never install a sampled prefix in the authoritative runtime. It is
          // useful only for an empty cold first paint, and cannot drive cache,
          // acceptance, pagination, detail reads, or replace a newer replay
          // recovery view. Any existing canonical turn is strictly preferred
          // without creating a recovery lock.
          const previewRuntime: SessionRuntime = {
            ...base,
            turns: built.turns,
            hasMore: e.has_more,
            oldestId: e.oldest_id ?? null,
            historyRevision: e.revision,
          };
          applyPendingCodexTerminalFences(previewRuntime);
          next = {
            ...next,
            historyRecovery: pendingRecovery
              ? state.historyRecovery!
              : beginHistoryRecovery(
                  state.historyRecovery,
                  sid,
                  previewRuntime,
                  e.generation,
                  e.build_seq ?? 0,
                ),
          };
        }
        return next;
      }
      // A pre-rollback first page can arrive after the replayable marker. Only
      // the marker's exact revision may cross the destructive boundary; older
      // pagination was already routed out through the display-only path above.
      if (base.pendingHistoryRevision
          && e.revision !== base.pendingHistoryRevision) return state;
      const pendingAcceptanceTurn = base.acceptancePending
        ? base.turns.find((turn) =>
            turnHasIdentityAlias(turn, base.acceptancePending))
        : undefined;
      const baselineAcceptedNativeTurnId = pendingAcceptanceTurn
          && base.acceptanceHistoryBaseline
        ? matchQueryAcceptanceHistory(
          queryAcceptanceDescriptor(
            pendingAcceptanceTurn.id,
            pendingAcceptanceTurn.prompt,
            pendingAcceptanceTurn.images,
            pendingAcceptanceTurn.files,
          ),
          base.acceptanceHistoryBaseline,
          e,
        )
        : null;
      const acceptedHistoryTurn = base.acceptancePending
        ? built.turns.find((turn) =>
            turnHasIdentityAlias(turn, base.acceptancePending))
        : undefined;
      const acceptedNativeTurnId = acceptedHistoryTurn?.id
        ?? baselineAcceptedNativeTurnId;
      const acceptanceConfirmed = !!base.acceptancePending && (
        !!acceptedNativeTurnId
        || e.events.some((ev) => {
          if (ev.type === "user_msg"
              && ev.client_msg_id === base.acceptancePending) return true;
          return (
            ev.type === "user_msg" || ev.type === "turn_steered"
              || ev.type === "turn_binding"
              || (ev.type === "error" && ev.code !== "wrapper_offline")
          ) && ev.msg_id === base.acceptancePending;
        })
      );
      if (acceptedNativeTurnId && base.acceptancePending
          && acceptedNativeTurnId !== base.acceptancePending) {
        // The materialized transcript owns a native user id while live UI owns
        // the browser msg_id. The frozen-head proof above is the missing
        // TurnBinding: normalize only that exact newest row so normal history
        // merging preserves the optimistic identity and never renders twice.
        built.turns = built.turns.map((turn) => turn.id === acceptedNativeTurnId
          ? {
              ...turn,
              id: base.acceptancePending!,
              historyTurnId: acceptedNativeTurnId,
            }
          : turn);
      }
      const racedLiveEvent = e.live_seq != null
        && base.lastLiveSeq > e.live_seq;
      const runningGeneration = preControlBase.controlGeneration
        ?? preControlBase.historyGeneration;
      const currentRunningHistory = e.in_progress === true
        && e.live_seq != null
        && base.lastLiveSeq <= e.live_seq
        && base.lastLifecycleSeq <= e.live_seq
        && (runningGeneration == null || e.generation === runningGeneration);
      const preserveProjectionOpenPlans = racedLiveEvent
        || e.in_progress === true
        || (e.in_progress == null && base.state !== "idle");
      const settledHistory = !racedLiveEvent
        && e.in_progress === false;
      const isCodexHistory = ownership?.engine === "codex"
        || state.sessions.some((session) =>
          session.session_id === sid && session.engine === "codex");
      const isClaudeHistory = ownership?.engine === "claude"
        || state.sessions.some((session) =>
          session.session_id === sid && session.engine === "claude");
      const settledCodexHistory = settledHistory && isCodexHistory;
      // Never infer Claude merely because Codex ownership has not arrived yet.
      // Session lists and first History can race on reconnect; an unknown
      // engine must retain its projection until explicit ownership exists.
      const settledClaudeHistory = settledHistory && isClaudeHistory;
      const resolveUnknownSteerFromIdle = settledHistory
        && base.acceptanceKind === "steer_unknown"
        && !!base.acceptancePending
        && !acceptanceConfirmed;
      const acceptanceRuntime = { ...base };
      if (acceptanceConfirmed) clearAcceptance(acceptanceRuntime);
      const aliasRevisionChanged = base.historyRevision !== e.revision
        && !base.historyInvalidated && !e.reset
        && e.generation != null && base.historyGeneration === e.generation
        && e.continuity_revision != null
        && e.continuity_revision === (base.historyContinuityRevision ?? base.historyRevision);
      const preserveStableHeadHistory = base.turns.length > 0
        && (base.hasLoadedOlderHistory || e.has_more === true)
        && !base.historyInvalidated
        && (base.historyRevision === e.revision || aliasRevisionChanged)
        && (e.generation != null
          ? base.historyGeneration === e.generation
          : base.historyGeneration == null);
      let turns: Turn[];
      let pendingBound: Turn[] = [];
      if (e.before) {
        // pagination (load older): PREPEND the older turns ahead of what we have,
        // deduped by id — keeps the current view and in-flight turn intact.
        const haveIds = new Set(base.turns.map((t) => t.id));
        turns = [...built.turns.filter((t) => !haveIds.has(t.id)), ...base.turns];
      } else {
        // Every first page is authoritative for completed turns. Merge only the
        // genuinely unfinished local tail; arbitrary completed cache rows may
        // have been removed by rollback while this browser was offline.
        const cached = new Set(base.hydratedCacheTurnIds);
        const unfinished = unfinishedLiveTail(
          base.turns, base.hydratedCacheTurnIds);
        const newestUnfinished = [...unfinished].reverse().find(
          (turn) => turnHasUnfinishedWork(turn));
        const pendingBinding = base.pendingLiveBinding;
        const binding = pendingBinding
          && e.in_progress
          && e.live_seq != null
          && !e.external
          && e.generation
          && pendingBinding.generation === e.generation
          && e.live_seq >= pendingBinding.seq
          ? pendingBinding : null;
        pendingBound = binding
          ? unfinished.filter((turn) =>
              !turn.done
              && turnHasIdentityAlias(turn, binding.msgId)
              && (!turnHasBoundEngineId(turn)
                || findTurnByEngineId([turn], binding.turnId))
              && (turn.prompt || turn.images?.[0] || turn.files?.[0]))
          : [];
        const liveTail = preserveStableHeadHistory
          // A bounded newest page is a moving head window, not the whole
          // conversation. Keep rows already painted from live traffic or from
          // explicit older pages. This is essential when compact makes the
          // current turn itself larger than the backend byte window.
          ? base.turns
          : racedLiveEvent
          // This History started before a live event already painted by the
          // browser. Keep every non-cache local row (including a just-completed
          // TurnEnd); the stale frame may add history but cannot delete it.
          ? base.turns.filter((turn) => !cached.has(turn.id))
          : base.historyInvalidated
          // Replay gaps begin at an arbitrary ring position and can therefore
          // synthesize a prompt-less "turn" from the middle of old output.
          // Current authoritative History validates real replay tails by turn
          // identity; unmatched fragments must not survive at the newest edge.
          // Keep an optimistic query which has not yet received its UserMsg
          // echo so a History read cannot erase an in-flight send. Once
          // TurnBinding clears that acceptance flag, pendingBound preserves
          // only its unique same-generation user row while running History
          // still lags behind compaction. Likewise, an explicitly running
          // snapshot may precede the transcript flush; only its newest
          // unfinished row can be the active unflushed tail.
          ? unfinished.filter((turn) =>
              turn.id === base.acceptancePending
              || historyContainsTurn(built.turns, turn)
              || (turn === pendingBound[0] && !pendingBound[1])
              || (e.in_progress === true && turn === newestUnfinished))
          : unfinished;
        turns = mergeInitialHistory(
          built.turns,
          liveTail, {
          // History's final TurnEnd is synthetic: Claude transcripts do not
          // contain ResultMessage. A newer live event always wins; otherwise an
          // explicit in_progress value is authoritative, and only an older
          // wrapper without that field falls back to the local runtime state.
          preserveLiveTailOpen: preserveProjectionOpenPlans,
          // Codex app-server History reports the native turn's persisted
          // completed/failed status. Once an idle, unraced page arrives it
          // repairs a provisional live terminal (notably after compaction)
          // while leaving Claude's transcript-only lifecycle untouched.
          newestHistoryId: e.newest_id ?? null,
          activeOwnerId: base.liveOwner?.turnId ?? null,
          reconcileReplayOrphans: true,
        }, settledCodexHistory);
        if (acceptanceConfirmed && base.acceptancePending
            && (base.acceptanceKind === "steer"
              || base.acceptanceKind === "steer_unknown")
            && acceptedNativeTurnId) {
          reconcileAcceptedSteerHistory(
            turns,
            base.acceptancePending,
            e.ts ? Math.round(e.ts * 1000) : Date.now(),
          );
        }
        // A current first page which explicitly reports idle is the recovery
        // boundary for a lost TurnEnd. Do not close a merely optimistic local
        // query (base is still idle), or a tail advanced after this History read.
        if (settledHistory) {
          const wasInterrupting = base.state === "interrupting"
            || base.state === "draining";
          const doneTs = e.ts ? Math.round(e.ts * 1000) : Date.now();
          turns = finishOpenTurnsFromIdleHistory(
            turns, wasInterrupting,
            doneTs,
            acceptanceConfirmed ? null : base.acceptancePending);
          if (resolveUnknownSteerFromIdle) {
            resolveUnknownPendingSteer(
              acceptanceRuntime, turns, doneTs);
          }
        }
      }
      if (e.detail === "summary" && !base.historyInvalidated
          && (base.historyRevision === e.revision || aliasRevisionChanged)) {
        const loadedDetail = new Map<string, Turn>();
        for (const turn of base.turns) {
          if (!turn.detailLoaded
              && (turn.detailProjection?.segments.length ?? 0) === 0) continue;
          for (const alias of turnIdentityAliases(turn)) {
            loadedDetail.set(alias, turn);
          }
        }
        turns = turns.map((turn) => {
          const detail = turnIdentityAliases(turn)
            .map((alias) => loadedDetail.get(alias))
            .find((candidate): candidate is Turn => !!candidate);
          if (!detail) return turn;
          const merged = mergeAuthoritativeTurnDetail(turn, detail, built.turns);
          // A completed row may be the neutral-steer segment whose Plan spans
          // the following clarification, but only a current running History
          // (or a newer live frame which raced this page) may keep it open. An
          // exact idle page must settle stale cache/detail Plan state too.
          finishCompletedTurnChildren(
            merged, preserveProjectionOpenPlans, isClaudeHistory);
          return merged;
        });
        const cachedScopeMatches = e.generation != null
          ? base.historyGeneration === e.generation
          : base.historyGeneration == null;
        if (cachedScopeMatches && base.hydratedCacheTurnIds.length > 0) {
          const cachedIds = new Set(base.hydratedCacheTurnIds);
          const activeCacheOwnerId = preserveProjectionOpenPlans
            ? (!racedLiveEvent && e.in_progress === true
                ? (e.newest_id ?? null)
                : base.acceptanceKind === "query" && base.acceptancePending
                  ? base.acceptancePending
                  : (base.liveOwner?.turnId ?? null))
            : null;
          turns = restoreCachedTurnDetails(
            turns,
            base.turns.filter((turn) => cachedIds.has(turn.id)),
            preserveProjectionOpenPlans ? "running" : "idle",
            activeCacheOwnerId,
          );
        }
      }
      const liveDetailScopeMatches = e.detail === "summary"
        && !base.historyInvalidated
        && (base.historyRevision == null
          || base.historyRevision === e.revision || aliasRevisionChanged)
        && (e.generation != null
          ? base.historyGeneration == null
            || base.historyGeneration === e.generation
          : base.historyGeneration == null);
      if (liveDetailScopeMatches) {
        // Explanations are independent from heavyweight detail/cache paint.
        // A later summary refresh must keep a previously restored exact cause.
        turns = restoreTurnInterruptionCauses(turns, base.turns);
      }
      if (liveDetailScopeMatches && base.liveDetailTurnIds.length > 0) {
        const observedIds = new Set(base.liveDetailTurnIds);
        turns = restoreObservedLiveTurnDetails(
          turns,
          base.turns.filter((turn) => observedIds.has(turn.id)),
        );
      }
      // Codex can compact between accepting a browser query and flushing that
      // query back into the bounded History page. Live replay then contains an
      // exact bound user row plus a promptless compaction continuation row.
      // The wrapper-provided continuation set is the only proof that those two
      // native ids belong to one task; never infer this from row order or time.
      let provenanceRuntime = base;
      let compactBridgeOwner: SessionRuntime["liveOwner"] = null;
      const compactBinding = base.pendingLiveBinding;
      const compactIds = e.compaction_continuation_turn_ids;
      if (currentRunningHistory && !e.external && !base.external
          && compactBinding
          && compactBinding.generation === (e.generation
            ?? base.controlGeneration ?? base.historyGeneration)
          && e.live_seq! >= compactBinding.seq
          && compactIds?.includes(compactBinding.turnId)
          && pendingBound.length === 1) {
        const bridge = { ...base, turns };
        const repaired = reconcileBoundCompactionOrphan(
          bridge, turns, [compactBinding.msgId], compactIds);
        if (repaired.length < turns.length) {
          const owner = repaired.find((turn) =>
            turnHasIdentityAlias(turn, compactBinding.msgId))!;
          bridge.turns = repaired;
          bridge.liveOwner = {
            turnId: owner.id,
            seq: e.live_seq!,
          };
          turns = repaired;
          provenanceRuntime = bridge;
          compactBridgeOwner = bridge.liveOwner;
        }
      }
      let boundHistoryOwner: SessionRuntime["liveOwner"] = null;
      if (currentRunningHistory && e.live_seq != null) {
        turns = reopenAuthoritativeActiveHistoryHead(
          provenanceRuntime, turns, e.newest_id, e.live_seq,
          e.compaction_continuation_turn_ids);
        // A current History head can be the only surviving acceptance proof
        // after reconnect. Feed that exact native/client pair through the same
        // strict binding path as a live TurnBinding; the helper still requires
        // one unfinished authoritative head and rejects alias ambiguity.
        const binding = acceptanceConfirmed && base.acceptancePending
            && acceptedNativeTurnId
            && !e.external && !base.external
            && e.generation != null
            && e.generation === (preControlBase.pendingHistoryGeneration
              ?? preControlBase.controlGeneration
              ?? preControlBase.historyGeneration)
          ? {
              msgId: base.acceptancePending,
              turnId: acceptedNativeTurnId,
              seq: e.live_seq,
              generation: e.generation,
            }
          : base.pendingLiveBinding;
        const responseGeneration = e.generation
          ?? base.controlGeneration ?? base.historyGeneration;
        if (binding && binding.generation === responseGeneration
            && e.live_seq >= binding.seq) {
          const bindingRuntime = {
            ...base,
            state: "running" as const,
            liveOwner: null,
          };
          bindAuthoritativeActiveHistoryHead(
            bindingRuntime,
            turns,
            binding.msgId,
            binding.turnId,
            binding.seq,
            e.newest_id ?? null,
            true,
            e.compaction_continuation_turn_ids,
          );
          boundHistoryOwner = bindingRuntime.liveOwner;
        }
      }
      // A later canonical terminal is sufficient to settle a shell which an
      // earlier running History reopened across a compact boundary. Do not
      // depend on a separate State(idle) frame: reconnect can recover through
      // History alone, and persisting this connection-local marker would make
      // the next refresh look like another continuation candidate.
      turns = turns.map((turn) => {
        let settled = turn;
        if (turn.done && turn.terminalSource === "compact_continuation") {
          settled = { ...turn };
          delete settled.terminalSource;
        }
        const terminalProblem = settled.interrupted === true
          || settled.error != null;
        if (settled.done && terminalProblem
            && mutableTurnBlocks(settled).some((block) => !block.done)) {
          settled = {
            ...settled,
            blocks: settled.blocks.map((block) => ({ ...block })),
            liveSpillBlocks: settled.liveSpillBlocks?.map(
              (block) => ({ ...block })),
          };
          const terminalStatus = settled.interrupted
            ? "interrupted" : settled.error ? "failed" : "succeeded";
          finishOpenBlocks(
            settled, terminalStatus, terminalStatus !== "succeeded");
        }
        return settled;
      });
      const terminalRuntime: SessionRuntime = {
        ...base,
        turns,
        pendingTerminalFences: base.pendingTerminalFences,
      };
      installCodexTerminalFences(
        terminalRuntime,
        e.terminal_fences,
        {
          revision: e.revision,
          generation: e.generation ?? base.historyGeneration,
          continuationTurnIds:
            e.compaction_continuation_turn_ids ?? [],
          // Only an idle, unraced newest page proves that an already-completed
          // row is the final narrative segment for this native turn. A running
          // or live-raced page can still be followed by a steer segment which
          // shares the same native turn id, so keep its fence pending.
          consumeSettledMatches: settledHistory,
        },
      );
      turns = terminalRuntime.turns;
      if (settledCodexHistory) {
        // A passive CLI holder shares the daemon but owns no active task. Once
        // the newest exact Codex History page is idle and no newer live frame
        // raced it, its completed rows are the terminal fence for browser-only
        // process projections. Preserve every payload/disclosure, but never let
        // an old item/started replay animate the session again.
        turns = cloneTurns(turns);
        turns.forEach((turn) => finishCompletedTurnChildren(turn));
      } else if (settledClaudeHistory) {
        // ResultMessage closes Claude's foreground turn, but a following
        // same-revision summary can restore live-observed detail after that
        // terminal. Settle only ordinary foreground children here; explicitly
        // detached Agent work remains live in its collaboration card.
        turns = cloneTurns(turns);
        turns.forEach((turn) =>
          finishCompletedTurnChildren(turn, false, true));
      }
      turns = turns.map(withLimitedTurnBlocks);
      const boundedTurns = boundRuntimeTurns(turns);
      const historyTrimmed = boundedTurns.length < turns.length;
      turns = boundedTurns;
      const locallyRetainedCursor = historyTrimmed
        ? (turns[0]?.historyTurnId ?? turns[0]?.id ?? null)
        : null;
      // Keeping a same-revision painted head and keeping its paging authority
      // are separate decisions. IndexedDB hydrates only turns/revision, so its
      // default false/null paging fields must never override an authoritative
      // newest page which says older history exists. An explicitly paged
      // runtime (including one which reached the history floor), or a runtime
      // with a complete usable cursor, still owns its stable reading boundary.
      const preserveStablePagination = preserveStableHeadHistory && (
        base.hasLoadedOlderHistory || (!!base.hasMore && !!base.oldestId)
      );
      // Older pages returned at the top of this case and never reach runtime
      // authority reconciliation; every remaining frame is a newest page.
      const acceptsControlState = true;
      const acceptsOwnershipState = acceptsControlState && !racedLiveEvent
        && !base.hasRevisionedControl;
      const confirmsWrapperRunning = acceptsControlState
        && e.in_progress === true
        && e.external !== true
        && !base.external
        && (!racedLiveEvent || (e.live_seq != null
          && base.lastLifecycleSeq <= e.live_seq));
      const hadModel = e.events.some((ev) => (ev as { type?: string }).type === "model");
      const hadEffort = e.events.some((ev) => (ev as { type?: string }).type === "effort");
      const nextHistoryGeneration = acceptsControlState
        ? (e.generation ?? base.historyGeneration)
        : base.historyGeneration;
      const nextOrderingGeneration = base.controlGeneration
        ?? nextHistoryGeneration;
      // Once History has established the accepted head as an explicit owner,
      // an older predecessor binding must not reopen that row on the next tool.
      let pendingLiveBinding = boundHistoryOwner
        ? null : base.pendingLiveBinding;
      // An exact idle History page is also a lifecycle boundary when the
      // browser missed State(idle). Do not carry task A's owner into a later
      // task B which can become running before its binding reaches this client.
      let liveOwner = settledHistory
        ? null
        : boundHistoryOwner
          ?? compactBridgeOwner
          ?? remapExplicitLiveTaskOwner(base.liveOwner, turns);
      const liveDetailTurnIds = remapTurnProvenanceIds(
        provenanceRuntime.liveDetailTurnIds, provenanceRuntime.turns, turns);
      if (pendingLiveBinding
          && pendingLiveBinding.generation !== nextOrderingGeneration) {
        pendingLiveBinding = null;
      }
      if (pendingLiveBinding && settledHistory && e.live_seq != null
          && e.live_seq >= pendingLiveBinding.seq) {
        pendingLiveBinding = null;
      }
      if (pendingLiveBinding && acceptsControlState
          && e.in_progress === true) {
        const exactBindingOwners = turns.filter((turn) =>
          turnHasIdentityAlias(turn, pendingLiveBinding!.msgId));
        if (exactBindingOwners.length === 1
            && !exactBindingOwners[0].done) {
          liveOwner = {
            turnId: exactBindingOwners[0].id,
            seq: pendingLiveBinding.seq,
          };
        }
      }
      if (base.backgroundLevelEmpty) {
        turns = settleOrphanedBackgroundProcessBlocks(
          turns, base.backgroundLevelTs);
      }
      let historyBrowse = state.historyBrowse;
      let retainedHistoryBrowse = state.retainedHistoryBrowse;
      if (aliasRevisionChanged && historyBrowse?.sid === sid
          && historyBrowse.revision === base.historyRevision
          && historyBrowse.generation === e.generation) {
        // Only additive aliases changed. Keep rows and the reading lifetime,
        // but revoke old in-flight page/detail work and use the new revision.
        historyBrowse = {
          ...historyBrowse, revision: e.revision,
          windowEpoch: historyBrowse.windowEpoch + 1, latestDirty: true,
        };
      }
      if (historyBrowse?.sid === sid) {
        if (historyBrowse.revision !== e.revision
            || (e.generation != null
              && historyBrowse.generation !== e.generation)) {
          historyBrowse = null;
        } else if ((e.newest_id != null
            && e.newest_id !== base.historyNewestId)
            || (e.build_seq ?? 0) > base.historyBuildSeq) {
          historyBrowse = markBrowseLatestDirty(historyBrowse);
        }
      }
      if (retainedHistoryBrowse?.sid === sid) {
        const responseGeneration = e.generation ?? base.historyGeneration;
        const retainedMatches =
          retainedHistoryBrowse.revision === e.revision
          && retainedHistoryBrowse.generation === responseGeneration;
        if (retainedMatches && state.focusedSid === sid) {
          historyBrowse = retainedHistoryBrowse;
          if ((e.newest_id != null
              && e.newest_id !== base.historyNewestId)
              || (e.build_seq ?? 0) > base.historyBuildSeq) {
            historyBrowse = markBrowseLatestDirty(historyBrowse);
          }
        }
        // Either the exact authority was restored above or this first page
        // proved the retained revision/generation obsolete. In both cases the
        // read-only snapshot has reached its atomic terminal boundary.
        retainedHistoryBrowse = null;
      }
      return {
        ...state,
        // History can contain legacy Error rows.  They may reconstruct a
        // turn-local outcome, but must never escape as a fresh global banner.
        banner: state.banner,
        runtimes: {
          ...state.runtimes,
          [sid]: {
            ...base, turns, loading: false,
            ccSessionId: acceptsControlState ? sid : base.ccSessionId,
            state: confirmsWrapperRunning
              ? (base.state === "interrupting" || base.state === "draining"
                  ? base.state : "running")
              : settledHistory
                  && e.external !== true && !base.external
                ? "idle"
                : base.state,
            mirroredRunning: acceptsControlState && !racedLiveEvent
              ? e.external === true && e.in_progress === true
              : base.mirroredRunning,
            historyInvalidated: acceptsControlState
              ? false : base.historyInvalidated,
            historyRevision: acceptsControlState
              ? e.revision : base.historyRevision,
            historyContinuityRevision: acceptsControlState
              ? e.continuity_revision ?? e.revision : base.historyContinuityRevision,
            pendingHistoryRevision: acceptsControlState
              ? null : base.pendingHistoryRevision,
            historyGeneration: nextHistoryGeneration,
            pendingHistoryGeneration: acceptsControlState
              ? null : base.pendingHistoryGeneration,
            pendingHistoryCandidateBuildSeq: acceptsControlState
              ? null : base.pendingHistoryCandidateBuildSeq,
            historyBuildSeq: acceptsControlState
              ? (e.build_seq ?? base.historyBuildSeq)
              : base.historyBuildSeq,
            historyLiveSeq: acceptsControlState
              ? (e.live_seq ?? base.historyLiveSeq)
              : base.historyLiveSeq,
            historyFence: acceptsControlState
              ? (e.live_seq ?? null)
              : base.historyFence,
            liveOwner,
            pendingLiveBinding,
            pendingTerminalFences:
              terminalRuntime.pendingTerminalFences,
            historyNewestId: acceptsControlState
              ? (Object.prototype.hasOwnProperty.call(e, "newest_id")
                  ? (e.newest_id ?? null)
                  : base.historyNewestId)
              : base.historyNewestId,
            historyHeadKnown: acceptsControlState
              ? true : base.historyHeadKnown,
            hasLoadedOlderHistory: e.before
              ? true
              : preserveStableHeadHistory
                ? base.hasLoadedOlderHistory
                : false,
            hydratedCacheTurnIds: acceptsControlState
              ? [] : base.hydratedCacheTurnIds,
            liveDetailTurnIds,
            // A first-page History can finish after a live thread-settings
            // notification.  Its transcript snapshot then contains the old
            // model/effort even though its narrative rows are still useful.
            // Keep the live app-server/TUI setting whenever the sequence
            // watermark proves that the History build lost that race.
            model: acceptsControlState && !racedLiveEvent && hadModel
              ? built.model : base.model,
            effort: acceptsControlState && !racedLiveEvent && hadEffort
              ? built.effort : base.effort,
            // The display-only browse window can evict from the opposite edge,
            // so a newest-biased runtime trim is now a valid older-page cursor
            // instead of a terminal pagination boundary.
            hasMore: historyTrimmed
              ? true
              : preserveStablePagination ? base.hasMore : e.has_more,
            oldestId: historyTrimmed
              ? locallyRetainedCursor
              : preserveStablePagination
                ? base.oldestId : (e.oldest_id ?? base.oldestId),
            truncated: base.truncated || historyTrimmed,
            // A native `claude`/`codex` in the terminal owns this session and is
            // appending to its transcript; the wrapper mirrors those appends here.
            // Render read-only — a cc session has ONE owner, and typing would fork it.
            external: acceptsOwnershipState ? !!e.external : base.external,
            takeoverPending: acceptsOwnershipState
              ? !!e.takeover_pending : base.takeoverPending,
            takeoverMessage: acceptsOwnershipState
              ? (e.takeover_pending ? base.takeoverMessage : null)
              : base.takeoverMessage,
            acceptancePending: acceptanceRuntime.acceptancePending,
            acceptanceKind: acceptanceRuntime.acceptanceKind,
            acceptanceHistoryBaseline:
              acceptanceRuntime.acceptanceHistoryBaseline,
          },
        },
        historyRecovery: advanceHistoryRecovery(state.historyRecovery, e),
        historyBrowse,
        retainedHistoryBrowse,
      };
    }
    case "turn_detail": {
      const sid = e.session_id;
      const base = state.runtimes[sid];
      if (!base || base.historyRevision !== e.revision) return state;
      if (e.authoritative === false) {
        const next = patch(state, sid, (rt) => {
          rt.turns = rt.turns.map((turn) => turn.id === e.turn_id
              || canonicalTurnId(turn) === e.turn_id
              ? {
                ...turn,
                detailLoading: false,
                detailAutoLoad: false,
                detailError: e.error ?? "详细过程暂时不可用，请重试",
                detailRetryBefore:
                  e.reset_required
                    ? null : turn.detailRetryBefore ?? e.before ?? null,
                detailRetryDirection: e.reset_required
                  ? "initial"
                  : turn.detailRetryDirection
                    ?? (e.before == null
                      ? "initial"
                      : e.before === turn.detailNewerCursor
                        ? "newer" : "older"),
                detailResetPending: e.reset_required
                  ? true : turn.detailResetPending,
              }
            : turn);
        });
        return next;
      }
      const target = base.turns.find((turn) => turn.id === e.turn_id
        || canonicalTurnId(turn) === e.turn_id);
      if (!target || e.events.length === 0) {
        const exactEmptyInitial = !!target && e.events.length === 0
          && e.before == null && !e.has_more && !e.has_newer;
        return patch(state, sid, (rt) => {
          rt.turns = rt.turns.map((turn) => (
            turn.id === e.turn_id || canonicalTurnId(turn) === e.turn_id)
            ? {
                ...turn,
                detailLoading: false,
                detailAutoLoad: false,
                detailError: undefined,
                detailRetryBefore: undefined,
                detailRetryDirection: undefined,
                detailResetPending: false,
                // An empty complete read can refine an opaque summary to an
                // exact direct reply. It cannot revoke positive evidence from
                // a previous page/live event in this revision: doing so makes
                // the collapsed process disappear until the next History
                // refresh restores it.
                ...(exactEmptyInitial
                    && turn.processDetailState !== "present" ? {
                  processDetailState: "none" as const,
                  detailReasons: turn.detailReasons?.filter(
                    (reason) => reason !== "process"),
                  detailEventCount: 0,
                  detailLoaded: true,
                  processStartedTs: undefined,
                  processDoneTs: undefined,
                } : {}),
              }
            : turn);
        });
      }
      const installed = installTurnDetailProjectionPage(
        target.detailResetPending && e.before == null
          ? undefined : target.detailProjection,
        {
          before: e.before,
          events: e.events as ServerEvent[],
          hasMore: e.has_more,
          oldestCursor: e.oldest_cursor,
          hasNewer: e.has_newer,
          newerCursor: e.newer_cursor,
        },
        (events) => decodeTurnDetailEvents(state, sid, e.turn_id, events),
      );
      const detailed = installed.detail;
      if (!detailed) {
        return patch(state, sid, (rt) => {
          rt.turns = rt.turns.map((turn) => (
            turn.id === e.turn_id || canonicalTurnId(turn) === e.turn_id)
            ? {
                ...turn,
                detailLoading: false,
                detailAutoLoad: false,
                detailError: DETAIL_PARSE_ERROR,
                detailRetryBefore:
                  turn.detailRetryBefore ?? e.before ?? null,
                detailRetryDirection: turn.detailRetryDirection
                  ?? (e.before == null
                    ? "initial"
                    : e.before === turn.detailNewerCursor
                      ? "newer" : "older"),
                detailResetPending: turn.detailResetPending,
              }
            : turn);
        });
      }
      return patch(state, sid, (rt) => {
        rt.turns = rt.turns.map((turn) => {
          if (turn.id !== e.turn_id
              && canonicalTurnId(turn) !== e.turn_id) return turn;
          const refreshAfterInFlight =
            turn.done && turn.detailRestorePending === true;
          const next = installAuthoritativeTurnDetailPage(
            turn,
            detailed,
            {
              hasMore: !!e.has_more,
              oldestCursor: e.oldest_cursor,
              hasNewer: !!e.has_newer,
              newerCursor: e.newer_cursor,
            },
            installed.projection,
          );
          if (next.done) {
            const status = next.interrupted
              ? "interrupted" : next.error ? "failed" : "succeeded";
            finishOpenDetailBlocks(
              next, status, status !== "succeeded");
          }
          if (continuedLiveSpillRefreshDue(next)
              && next.detailLoading !== true) {
            next.detailRestorePending = true;
            next.detailRestoreIncomplete = true;
          }
          if (refreshAfterInFlight) {
            next.detailRestorePending = true;
            next.detailRestoreIncomplete = true;
          }
          return next;
        });
        reconcileAuthoritativeBackgroundProcessLevel(rt);
      });
    }
    case "turn_file_changes_page":
    case "files_listed":
    case "agent_detail":
      // Agent detail is a requester-correlated side panel projection. App owns
      // it separately so it can never mutate the parent conversation runtime.
      return state;
    case "dir_list":
      return {
        ...state,
        dirPicker: {
          path: e.path,
          parent: e.parent ?? null,
          dirs: e.dirs,
          requestId: e.request_id ?? null,
        },
      };
    // The engine's real model catalog. Empty => the wrapper couldn't read it; keep
    // what we have (data.ts's static table) rather than blanking the pickers.
    case "models": {
      const cacheKey = modelCatalogScopeKey(
        e.engine,
        e.engine === "codex"
          ? (e.codex_profile_id ?? state.defaultCodexProfileId)
          : e.engine === "claude"
            ? (e.claude_profile_id ?? state.defaultClaudeProfileId)
            : null,
      );
      const catalog = e.models.length
        ? { ...state.catalog, [cacheKey]: e.models }
        : state.catalog;
      if (e.cwd && e.cwd !== state.newChat?.cwd) {
        // Cwd-aware reads run concurrently. Never let a late response for a
        // directory the user has left replace the still-current result.
        return catalog === state.catalog ? state : { ...state, catalog };
      }
      let catalogDefault = state.catalogDefault;
      let catalogDefaultEffort = state.catalogDefaultEffort;
      let catalogDefaultCwd = state.catalogDefaultCwd;
      if (e.cwd) {
        // A Claude response is authoritative even when probing failed and the
        // value is null: clear an older cwd's value instead of showing stale data.
        catalogDefault = { ...catalogDefault };
        catalogDefaultEffort = { ...catalogDefaultEffort };
        if (e.default_model) {
          catalogDefault[cacheKey] = matchModelId(e.default_model, e.engine);
        } else {
          delete catalogDefault[cacheKey];
        }
        if (e.default_effort) {
          catalogDefaultEffort[cacheKey] = e.default_effort;
        } else {
          delete catalogDefaultEffort[cacheKey];
        }
        catalogDefaultCwd = {
          ...catalogDefaultCwd, [cacheKey]: e.cwd,
        };
      } else {
        if (e.default_model) {
          catalogDefault = { ...catalogDefault,
            [cacheKey]: matchModelId(e.default_model, e.engine) };
        }
        if (e.default_effort) {
          catalogDefaultEffort = {
            ...catalogDefaultEffort, [cacheKey]: e.default_effort,
          };
        }
      }
      return {
        ...state, catalog, catalogDefault, catalogDefaultEffort,
        catalogDefaultCwd,
      };
    }
    // App owns this on-demand, surface-keyed sheet state. Keep the event in the
    // exhaustive reducer switch so protocol drift cannot silently bypass it.
    case "engine_capabilities":
      return state;
    case "wrapper_disconnected":
      return {
        ...state,
        runtimes: Object.fromEntries(Object.entries(state.runtimes).map(
          ([sid, runtime]) => [sid, {
            ...runtime, syncReady: false, replaying: false,
          }])),
        wrapperOnline: false,
        banner: "machine offline — waiting for reconnect",
        historyBrowse: null,
        retainedHistoryBrowse:
          state.historyBrowse ?? state.retainedHistoryBrowse,
      };
    case "wrapper_reconnected":
      // The event only proves a process connected to the relay. Wait for this
      // client's Hello replay/snapshot before draining any queued turns.
      return { ...state, wrapperOnline: false, banner: "machine reconnected — syncing…" };
    case "diff_report":
      if (!state.artifact || state.artifact.file !== e.file
          || state.artifact.requestId !== e.request_id
          || state.artifact.sid !== (e.sid ?? state.focusedSid)) return state;
      return { ...state, artifact: {
        file: e.file, sid: state.artifact.sid, requestId: e.request_id,
        kind: "gitdiff", sections: parseGitDiff(e.diff),
      } };
    case "file_preview":
      if (!state.artifact || !["md", "file", "html", "image", "pdf", "audio"].includes(state.artifact.kind)
          || state.artifact.requestId !== e.request_id
          || state.artifact.sid !== (e.sid ?? state.focusedSid)) return state;
      return { ...state, artifact: {
        file: e.path,
        sid: state.artifact.sid,
        requestId: e.request_id,
        kind: e.format === "markdown" ? "md" : e.format === "text" ? "file" : e.format,
        content: e.content,
        data: e.data ?? undefined,
        mediaType: e.media_type ?? undefined,
        convertedFrom: e.converted_from ?? undefined,
        size: e.size,
        truncated: e.truncated,
        mtimeNs: e.mtime_ns,
        revision: e.revision ?? undefined,
        writable: e.writable !== false,
        line: state.artifact.line,
        error: e.error ?? undefined,
        assets: {},
      } };
    case "file_save_result":
      if (!state.artifact || state.artifact.kind !== "md"
          || state.artifact.saveRequestId !== e.request_id
          || state.artifact.sid !== (e.sid ?? state.focusedSid)) return state;
      if (e.status === "saved") {
        return { ...state, artifact: {
          ...state.artifact,
          content: state.artifact.pendingContent ?? state.artifact.content,
          size: e.size,
          mtimeNs: e.mtime_ns,
          revision: e.revision ?? state.artifact.revision,
          saving: false,
          saveStatus: "saved",
          saveError: undefined,
          pendingContent: undefined,
        } };
      }
      return { ...state, artifact: {
        ...state.artifact,
        saving: false,
        saveStatus: e.status,
        saveError: e.error || (e.status === "conflict"
          ? "文件已被其他程序修改，请重新读取后再保存。" : "保存失败。"),
        pendingContent: undefined,
      } };
    case "preview_asset":
      if (!state.artifact || state.artifact.kind !== "md"
          || state.artifact.requestId !== e.preview_id
          || state.artifact.sid !== (e.sid ?? state.focusedSid)) return state;
      {
        const pending = state.artifact.assets?.[e.path];
        if (!pending
            || pending.requestId !== e.request_id
            || pending.previewId !== e.preview_id) return state;
      }
      return { ...state, artifact: {
        ...state.artifact,
        assets: {
          ...state.artifact.assets,
          [e.path]: {
            requestId: e.request_id,
            previewId: e.preview_id,
            loading: false,
            mediaType: e.media_type ?? undefined,
            data: e.data ?? undefined,
            error: e.error ?? undefined,
          },
        },
      } };
    case "preview_authorization_required": {
      const artifact = state.artifact;
      const sid = e.sid ?? state.focusedSid;
      if (!artifact || !sid || artifact.sid !== sid) return state;
      const authorization: PreviewAuthorizationState = {
        authorizationId: e.authorization_id,
        requestId: e.request_id,
        operation: e.operation,
        path: e.path,
        resolvedPath: e.resolved_path,
        format: e.format,
        previewId: e.preview_id ?? undefined,
        status: "required",
      };
      if (e.operation === "file_preview") {
        if (artifact.requestId !== e.request_id
            || artifact.file !== e.path) return state;
        return { ...state, artifact: {
          ...artifact,
          loading: false,
          error: undefined,
          authorization,
        } };
      }
      if (artifact.kind !== "md"
          || artifact.requestId !== e.preview_id) return state;
      const pending = artifact.assets?.[e.path];
      if (!pending
          || pending.requestId !== e.request_id
          || pending.previewId !== e.preview_id) return state;
      return { ...state, artifact: {
        ...artifact,
        assets: {
          ...artifact.assets,
          [e.path]: {
            ...pending,
            loading: false,
            error: undefined,
            authorization,
          },
        },
      } };
    }
    case "preview_authorization_result": {
      const artifact = state.artifact;
      const sid = e.sid ?? state.focusedSid;
      if (!artifact || !sid || artifact.sid !== sid) return state;
      const matches = (authorization?: PreviewAuthorizationState) => (
        !!authorization
        && authorization.authorizationId === e.authorization_id
        && authorization.requestId === e.request_id
        && (!e.operation || authorization.operation === e.operation)
        && (!e.path || authorization.path === e.path)
        && (!e.preview_id || authorization.previewId === e.preview_id)
      );
      const problem = e.error || (
        e.status === "denied"
          ? "已取消读取外部文件。"
          : "该预览确认已过期，请重新打开文件。"
      );
      if (matches(artifact.authorization)) {
        if (e.status === "granted") {
          return { ...state, artifact: {
            ...artifact,
            authorization: {
              ...artifact.authorization!,
              status: "granted",
            },
          } };
        }
        return { ...state, artifact: {
          ...artifact,
          authorization: undefined,
          loading: false,
          error: problem,
        } };
      }
      for (const [path, asset] of Object.entries(artifact.assets ?? {})) {
        if (!matches(asset.authorization)) continue;
        return { ...state, artifact: {
          ...artifact,
          assets: {
            ...artifact.assets,
            [path]: e.status === "granted"
              ? {
                  ...asset,
                  authorization: {
                    ...asset.authorization!,
                    status: "granted",
                  },
                }
              : {
                  ...asset,
                  authorization: undefined,
                  loading: false,
                  error: problem,
                },
          },
        } };
      }
      return state;
    }
    case "state": {
      const next = patch(state, e.sid, (rt) => {
        rt.state = e.state;
        if (typeof e.seq === "number") {
          rt.lastLifecycleSeq = Math.max(rt.lastLifecycleSeq, e.seq);
        }
        // A direct lifecycle frame belongs to this wrapper's resident turn and
        // supersedes any older rollout-only activity projection.
        rt.mirroredRunning = false;
        const turns = cloneTurns(rt.turns);
        const turn = e.msg_id
          ? turns.find((candidate) => candidate.id === e.msg_id)
          : turns[turns.length - 1];
        if (e.detail && turn && !turn.done) turn.progress = e.detail;
        else if (turn && (Object.hasOwn(e, "detail") || e.state !== "running")) {
          turn.progress = undefined;
        }
        if (e.state === "idle") {
          // State(idle) is the exact boundary between resident turns. Keeping
          // the completed owner here lets the next State(running) animate the
          // old row before its own UserMsg/TurnBinding establishes ownership.
          rt.liveOwner = null;
          rt.pendingLiveBinding = null;
          rt.pendingQuestion = null;
          const doneTs = eventTimestampMs(e.ts) ?? Date.now();
          for (const candidate of turns) {
            if (!candidate.done
                && candidate.terminalSource === "compact_continuation") {
              finishTurnWithoutTerminal(candidate, doneTs, null);
              delete candidate.terminalSource;
            }
          }
          if (rt.acceptanceKind === "steer_unknown") {
            for (const candidate of turns) {
              if (candidate.id === rt.acceptancePending) continue;
              finishTurnWithoutTerminal(candidate, doneTs);
            }
          }
          resolveUnknownPendingSteer(
            rt, turns, doneTs);
          if (state.sessions.some((session) =>
            session.session_id === e.sid && session.engine === "codex")) {
            // A direct idle frame is the exact shared-daemon boundary between
            // native tasks. Close only stale display children of already-done
            // turns; a later real background event can still reopen its item.
            turns.forEach((turn) => finishCompletedTurnChildren(turn));
          }
        }
        rt.turns = turns;
      });
      let changed = false;
      const sessions = next.sessions.map((session) => {
        if (session.session_id !== e.sid || session.state === e.state) {
          return session;
        }
        if (ownership && (
          (session.engine && session.engine !== ownership.engine)
          || (session.space && session.space !== ownership.space)
        )) return session;
        changed = true;
        return { ...session, state: e.state };
      });
      return changed ? { ...next, sessions } : next;
    }
    case "query_queue": {
      const next = patch(state, e.sid, (rt) => {
        // The wrapper owns deferred payloads. Retain only bounded display
        // metadata after its authoritative projection arrives so a sleeping
        // browser is no longer responsible for either execution or attachment
        // memory.
        const projected = e.items.map((item): PendingQuery => ({
          msg_id: item.msg_id,
          prompt: item.prompt_preview,
          imageCount: item.image_count,
          fileCount: item.file_count,
          queueKind: item.kind,
          queueState: "queued",
          retainedBytes: item.retained_bytes,
          queueError: item.error ?? undefined,
        }));
        const projectedIds = new Set(
          projected.map((query) => query.msg_id));
        const submittingQueue = rt.queue.filter((query) => (
          query.queueState !== "queued"
          && query.queueState !== "failed"
          && !projectedIds.has(query.msg_id)
        ));
        rt.queue = [
          ...projected.filter((_, index) =>
            e.items[index].kind === "queue"),
          ...submittingQueue,
        ];
        const replacement = projected.find((_, index) =>
          e.items[index].kind === "replace");
        const currentReplacement = rt.pendingSend;
        const submittingReplacement = (
          currentReplacement !== null
          && currentReplacement.queueState !== "queued"
          && currentReplacement.queueState !== "failed"
          && !projectedIds.has(currentReplacement.msg_id)
        ) ? {
            ...currentReplacement,
            replacesRetainedBytes:
              replacement?.retainedBytes
              ?? currentReplacement.replacesRetainedBytes,
          } : null;
        rt.pendingSend = submittingReplacement ?? replacement ?? null;
        rt.failedDeferred = rt.failedDeferred.filter(
          (query) => !projectedIds.has(query.msg_id));
      }, true);
      return {
        ...next,
        queryQueueCount: e.total_count,
        queryQueueBytes: e.total_bytes,
      };
    }
    case "session_control":
      // Direct control events require an explicit runtime key. Snapshot and
      // History controls are routed by their outer envelope above.
      if (!e.sid) return state;
      return patch(state, e.sid, (rt) => {
        applySessionControl(rt, e);
      }, true);
    case "takeover_state":
      return patch(state, e.sid, (rt) => {
        // Temporary compatibility while v15 producers migrate. Once a real
        // revisioned control value exists, an unrevisioned frame can never
        // overwrite it or revive a completed takeover lock.
        if (rt.hasRevisionedControl) return;
        rt.takeoverPending = e.pending;
        rt.takeoverMessage = e.message ?? null;
      });
    case "model":
      return patch(state, e.sid, (rt) => {
        const contextModel = rt.contextExactReport?.model
          ?? rt.contextReport?.model ?? rt.model;
        // An unattributed report received before the first authoritative Model
        // frame cannot be proven to belong to it: the empty rt.model fails this
        // comparison. A repeated announcement retains that report once rt.model
        // already establishes the same generation.
        rt.model = matchModelId(e.model);
        if (rt.contextReport
            && matchModelId(contextModel) !== rt.model) {
          // A model switch can change tokenization and context capacity. Never
          // pair the newly announced model with a prior model's exact total or
          // category breakdown. A new native/cache-only report will repopulate
          // the ring without manufacturing a percentage in the meantime.
          rt.contextReport = null;
          rt.contextExactReport = null;
          rt.contextError = null;
        }
      });
    case "effort":
      return patch(state, e.sid, (rt) => { rt.effort = e.effort; });
    case "codex_context":
      return patch(state, e.sid, (rt) => {
        const previous = rt.codexContext;
        rt.codexContext = e;
        if ((previous != null && (
          previous.applied_max_context_tokens !== e.applied_max_context_tokens
          || previous.applied_threshold_tokens !== e.applied_threshold_tokens))
            || (previous == null && !e.pending && e.applied_max_context_tokens != null
              && rt.contextReport != null && rt.contextReport.max_tokens !== e.applied_max_context_tokens)) {
          rt.contextReport = null;
          rt.contextExactReport = null;
          rt.contextError = null;
        }
      });
    case "auto_compact":
      return patch(state, e.sid, (rt) => {
        const previous = rt.autoCompact;
        const appliedChanged = previous != null && (
          previous.applied_mode !== e.applied_mode
          || previous.applied_threshold_tokens
            !== e.applied_threshold_tokens
        );
        const firstStateContradictsReport = previous == null
          && e.pending !== true
          && e.applied_mode === "custom"
          && e.applied_threshold_tokens != null
          && rt.contextReport?.auto_compact_threshold_tokens != null
          && rt.contextReport.auto_compact_threshold_tokens
            !== e.applied_threshold_tokens;
        rt.autoCompact = e;
        if (appliedChanged || firstStateContradictsReport) {
          // ContextReport is generation-bound.  Keep it while a busy session
          // merely queues a desired setting, but never display the old window
          // after the replacement Claude child has applied a new threshold.
          rt.contextReport = null;
          rt.contextExactReport = null;
          rt.contextError = null;
        }
      });
    case "fast":
      return patch(state, e.sid, (rt) => { rt.fast = e.on; });
    case "collaboration_mode":
      return patch(state, e.sid, (rt) => {
        rt.collaborationMode = e.mode;
      });
    case "btw_opened": {
      // Bind the fork to its authoritative parent without changing focus. A
      // response may arrive after the user navigates; it must remain hidden
      // until that exact parent is viewed again.
      if (e.revision < state.btwRevision) return state;
      const runtimes = {
        ...state.runtimes,
        [e.btw_sid]: state.runtimes[e.btw_sid] ?? createRuntime(),
      };
      const previous = state.btwByParentSid[e.parent_sid];
      const chat: BtwChat = {
        sid: e.btw_sid,
        engine: e.engine,
        createdAt: e.created_at,
        state: "idle",
      };
      const alreadyKnown = previous?.chats.some(
        (candidate) => candidate.sid === e.btw_sid) ?? false;
      const chats = alreadyKnown
        ? previous!.chats.map((candidate) =>
            candidate.sid === e.btw_sid ? chat : candidate)
        : [...(previous?.chats ?? []), chat];
      chats.sort((a, b) => a.createdAt - b.createdAt
        || a.sid.localeCompare(b.sid));
      return {
        ...state,
        btwByParentSid: {
          ...state.btwByParentSid,
          [e.parent_sid]: {
            chats,
            activeSid: alreadyKnown && previous
              ? previous.activeSid : e.btw_sid,
          },
        },
        btwRevision: e.revision,
        runtimes,
      };
    }
    case "btw_closed": {
      if (e.revision < state.btwRevision) return state;
      const group = state.btwByParentSid[e.parent_sid];
      const runtimes = { ...state.runtimes };
      delete runtimes[e.btw_sid];
      if (!group || !group.chats.some((chat) => chat.sid === e.btw_sid)) {
        return { ...state, btwRevision: e.revision, runtimes };
      }
      const chats = group.chats.filter((chat) => chat.sid !== e.btw_sid);
      const btwByParentSid = { ...state.btwByParentSid };
      if (chats.length === 0) {
        delete btwByParentSid[e.parent_sid];
      } else {
        const closedIndex = group.chats.findIndex(
          (chat) => chat.sid === e.btw_sid);
        const fallback = chats[Math.min(closedIndex, chats.length - 1)];
        btwByParentSid[e.parent_sid] = {
          chats,
          activeSid: group.activeSid === e.btw_sid
            ? fallback.sid : group.activeSid,
        };
      }
      return {
        ...state,
        btwByParentSid,
        btwRevision: e.revision,
        runtimes,
      };
    }
    case "perm":
      return patch(state, e.sid, (rt) => { rt.perm = e.mode; });
    case "permission_profile":
      return patch(state, e.sid, (rt) => {
        rt.permissionProfile = e.profile ?? null;
      });
    case "permission_profiles":
      return patch(state, e.sid, (rt) => {
        rt.permissionProfiles = e.profiles;
      });
    case "web_search":
      return patch(state, e.sid, (rt) => {
        rt.webSearch = e.mode;
      });
    case "context_report":
      return patch(state, e.sid, (rt) => {
        rt.contextReport = e;
        if (e.available !== false && e.source !== "recent_turn") {
          rt.contextExactReport = e;
        }
        // Reports are broadcast so every viewer benefits from the fresh value,
        // but only the matching response may settle this client's in-flight
        // request. While an exact refresh is deferred, an uncorrelated
        // recent-turn/cached publish is useful for the ring but cannot consume
        // that user intent; only another proven native control reading can.
        const matchesRequest = rt.contextRequestId !== null
          && rt.contextRequestId === e.request_id;
        const satisfiesDeferred = rt.contextRequestId === null
          && rt.contextRefreshDeferred
          && e.available !== false
          && e.source === "control";
        if (matchesRequest || satisfiesDeferred) {
          rt.contextRequestId = null;
          rt.contextRefreshDeferred = false;
          rt.contextError = null;
        } else if (rt.contextRequestId === null
            && !rt.contextRefreshDeferred) {
          // A normal unsolicited report still proves an obsolete local error
          // no longer describes the visible reading.
          rt.contextError = null;
        }
      });
    case "ask_user_sync":
      return patch(state, e.sid, (rt) => { rt.pendingQuestion = null; });
    case "ask_user":
      return patch(state, e.sid, (rt) => { rt.pendingQuestion = { ask_id: e.ask_id, header: e.header, question: e.question, options: e.options, allow_text: e.allow_text, secret: e.secret, multi_select: e.multi_select }; });
    case "ask_user_closed":
      return patch(state, e.sid, (rt) => {
        if (rt.pendingQuestion?.ask_id === e.ask_id) {
          rt.pendingQuestion = null;
        }
      });
    case "goal_state":
      return patch(state, e.sid, (rt) => {
        rt.goal = e.goal ?? null;
        rt.goalId = e.goal_id ?? null;
        rt.goalDismissed = e.dismissed === true;
      }, true);
    case "completion_state":
      return patch(state, e.sid, (rt) => {
        applyCompletionProjection(rt, {
          id: e.completion_id ?? null,
          unread: e.unread === true,
          revision: e.revision ?? 0,
        });
      }, true);
    case "rollback_result": {
      const next = patch(state, e.sid, (rt) => {
        const succeeded = [e.conversation, e.files].filter(
          (outcome) => outcome === "succeeded").length;
        const failed = [e.conversation, e.files].filter(
          (outcome) => outcome === "failed").length;
        const title = failed === 0 ? "回滚完成"
          : succeeded > 0 ? "回滚部分完成" : "回滚失败";
        const parts = [
          e.conversation !== "skipped" ? `对话：${e.conversation === "succeeded" ? "已恢复" : "失败"}` : "",
          e.files !== "skipped" ? `代码：${e.files === "succeeded" ? "已恢复" : "失败"}` : "",
        ].filter(Boolean);
        const notice: Notice = {
          v: e.v, type: "notice", ts: e.ts, sid: e.sid,
          notice_id: `rollback-${e.ts}-${e.session_id}`,
          severity: failed > 0 ? "warning" : "info",
          category: "runtime", title,
          message: e.detail || parts.join(" · ") || title,
          detail: e.conflicts.length > 0
            ? `冲突文件：${e.conflicts.slice(0, 12).join("、")}` : undefined,
          thread_id: e.session_id,
        };
        rt.notices = mergeNotices(rt.notices, [notice]);
      });
      // A files-only rollback has no HistoryInvalidated frame to close the
      // current file/diff preview.  Treat the successful result itself as the
      // authoritative byte boundary so a stale snapshot never remains visible.
      return e.files === "succeeded" && next.artifact?.sid === e.session_id
        ? { ...next, artifact: null }
        : next;
    }
    case "status_report":
      return patch(state, e.sid, (rt) => {
        // A status read can finish after a newer request.  Never let that old
        // snapshot overwrite the newer request's loading state or result.
        if (rt.statusRequestId && e.request_id !== rt.statusRequestId) return;
        rt.statusReport = e;
        rt.rateLimits = e.rate_limits.map((limit) => ({ ...limit }));
        rt.statusRequestId = null;
        rt.statusError = null;
      });
    case "rate_limit_reset_result":
      return patch(state, e.sid, (rt) => {
        // An ACK-lost retry can replay an older private result. It may restore
        // an otherwise-untracked request, but must not settle a newer click.
        if (rt.statusRequestId && e.request_id !== rt.statusRequestId) return;
        rt.statusRequestId = null;
        rt.resetCreditResult = e;
        rt.statusError = null;
        // The coupon inventory predates this mutation. Hide it immediately so
        // a slow/failed follow-up read cannot offer another click against stale
        // rows. The targeted StatusReport that follows restores authoritative
        // inventory; a failed read leaves the manual Refresh path available.
        if (rt.statusReport?.reset_credits) {
          rt.statusReport = { ...rt.statusReport, reset_credits: null };
        }
      });
    case "notice":
      return patch(state, e.sid, (rt) => {
        rt.notices = mergeNotices(rt.notices, [e]);
      });
    case "rate_limit_update":
      return patch(state, e.sid, (rt) => {
        rt.rateLimits = mergeRateLimitCollection(rt.rateLimits, e);
        rt.statusReport = mergeRateLimitUpdate(rt.statusReport, rt.rateLimits);
      });
    case "replay_start": {
      const replaySid = e.sid ?? state.focusedSid;
      // Ephemeral side chats have no canonical History endpoint. Their ring is
      // the authoritative bounded projection, so keep and apply a retained
      // suffix even when its older prefix has fallen out of the ring.
      const needsAuthoritativeHistory = (e.truncated || !!e.rebuild)
        && !replaySid?.startsWith("btw-");
      const submittedTurn = needsAuthoritativeHistory && !e.rebuild
        ? recoverableSubmittedTurn(
            state.runtimes[replaySid ?? ""] ?? createRuntime(),
            e.generation,
          )
        : undefined;
      let historyRecovery = state.historyRecovery;
      if (needsAuthoritativeHistory && state.focusedSid === e.sid
          && !state.newChat && e.sid) {
        historyRecovery = beginHistoryRecovery(
          state.historyRecovery,
          e.sid,
          state.runtimes[e.sid] ?? createRuntime(),
          e.generation,
          null,
          submittedTurn?.id ?? null,
        );
        if (submittedTurn && historyRecovery.turns) {
          // A prior recovery projection may predate this Query. Reconcile the
          // exact browser id into that frozen readable copy as well as the live
          // runtime, otherwise switching away/back exposes either no question
          // or both its optimistic and canonical rows while History catches up.
          historyRecovery = {
            ...historyRecovery,
            turns: mergeInitialHistory(
              historyRecovery.turns,
              [submittedTurn],
              { preserveLiveTailOpen: true },
            ),
          };
        }
      }
      const next = patch(state, e.sid, (rt) => {
        switchControlGeneration(rt, e.generation);
        rt.replaying = true;
        rt.syncReady = false;
        rt.truncated = e.truncated;
        // rebuild clears turns then refills — keep loading=true so the gap shows a
        // spinner rather than briefly flashing the empty "send a message" prompt.
        if (needsAuthoritativeHistory) {
          // Canonical rows still rebuild from source, but a same-generation
          // replay suffix cannot recreate a submitted user boundary which has
          // already fallen out of the ring. Keep its exact local/bound owner in
          // the live runtime so subsequent deltas and History share one row.
          rt.turns = submittedTurn ? [submittedTurn] : [];
          if (submittedTurn && rt.pendingLiveBinding
              && turnHasIdentityAlias(
                submittedTurn, rt.pendingLiveBinding.msgId)) {
            rt.liveOwner = {
              turnId: submittedTurn.id,
              seq: Math.max(
                rt.liveOwner?.seq ?? 0,
                rt.pendingLiveBinding.seq,
              ),
            };
          }
          rt.hasMore = false;
          rt.oldestId = null;
          rt.historyHeadKnown = false;
          rt.historyInvalidated = true;
          rt.pendingHistoryGeneration = e.generation ?? null;
          rt.pendingHistoryCandidateBuildSeq = null;
          // A replay gap does not reveal which revision was missed. Accept the
          // next authoritative first page; an actual rollback marker replayed
          // inside this envelope will immediately replace this with its token.
          rt.pendingHistoryRevision = null;
          rt.hydratedCacheTurnIds = [];
          rt.liveDetailTurnIds = [];
          rt.hasLoadedOlderHistory = false;
          if (e.rebuild) {
            // The wrapper generation (and every SessionContext seq) restarted.
            // Never compare the new generation against old live/build watermarks.
            rt.historyBuildSeq = 0;
            rt.historyLiveSeq = 0;
            rt.historyFence = null;
            rt.liveOwner = null;
            rt.pendingLiveBinding = null;
            rt.pendingTerminalFences = null;
            rt.historyGeneration = null;
            rt.historyNewestId = null;
            rt.lastLiveSeq = 0;
            rt.lastLifecycleSeq = 0;
          }
          rt.loading = true;
        }
      }, true);
      return {
        ...next,
        historyRecovery,
        historyBrowse: needsAuthoritativeHistory
            && next.historyBrowse?.sid === e.sid
          ? null : next.historyBrowse,
        retainedHistoryBrowse: needsAuthoritativeHistory
            && next.historyBrowse?.sid === e.sid
          ? next.historyBrowse
          : next.retainedHistoryBrowse,
        artifact: needsAuthoritativeHistory && next.artifact?.sid === e.sid
          ? null : next.artifact,
      };
    }
    case "replay_end":
      return { ...patch(state, e.sid, (rt) => {
        rt.replaying = false;
        rt.syncReady = true;
        rt.truncated = rt.truncated || e.truncated;
        // A truncated/rebuild replay is not authoritative history. Keep the
        // loading barrier until the first History page replaces the gap.
        rt.loading = rt.historyInvalidated;
      }, true), wrapperOnline: true };
    case "error": {
      // The relay has not accepted/rejected the command yet: reliable commands
      // stay in the outbox and will be retried when the wrapper returns. Keep the
      // optimistic turn pending instead of falsely marking it failed.
      if (e.code === "wrapper_offline") {
        return {
          ...state,
          runtimes: Object.fromEntries(Object.entries(state.runtimes).map(
            ([sid, runtime]) => [sid, {
              ...runtime, syncReady: false, replaying: false,
            }])),
          wrapperOnline: false,
          banner: "设备离线，正在等待重新连接…",
          historyBrowse: null,
          retainedHistoryBrowse:
            state.historyBrowse ?? state.retainedHistoryBrowse,
        };
      }
      if (e.request_id && e.sid) {
        if (state.artifact?.requestId === e.request_id
            && state.artifact.sid === e.sid) {
          return { ...state, artifact: {
            ...state.artifact, loading: false,
            error: presentCommandProblem(e),
          } };
        }
        const runtime = state.runtimes[e.sid];
        if (runtime?.contextRequestId === e.request_id) {
          return patch(state, e.sid, (rt) => {
            rt.contextRequestId = null;
            if (e.code === "busy") {
              rt.contextRefreshDeferred = true;
              rt.contextError = null;
            } else {
              rt.contextRefreshDeferred = false;
              rt.contextError = presentCommandProblem(e);
            }
          });
        }
        if (runtime?.statusRequestId === e.request_id) {
          return patch(state, e.sid, (rt) => {
            rt.statusRequestId = null;
            rt.statusError = presentCommandProblem(e);
            rt.resetCreditResult = null;
            if (rt.statusReport?.reset_credits) {
              rt.statusReport = { ...rt.statusReport, reset_credits: null };
            }
          });
        }
      }
      if ((e.code === "not_steerable"
          || e.code === "steer_outcome_unknown") && e.msg_id) {
        const next = patch(state, e.sid, (rt) => {
          if (rt.acceptancePending !== e.msg_id
              || rt.acceptanceKind !== "steer") return;
          if (e.code === "steer_outcome_unknown") {
            // Transport loss after submission is not a rejection. Keep both
            // the reliable acceptance latch and the optimistic row until a
            // narrative echo or an authoritative terminal boundary resolves it.
            rt.acceptanceKind = "steer_unknown";
            return;
          }
          // A definitive rejection is a control failure, not the terminal state
          // of the still-running native turn. Remove only the exact untouched
          // optimistic segment; never close or rewrite its active predecessor.
          rt.turns = rt.turns.filter((turn) => !(
            turn.id === e.msg_id
            && turn.clientMsgId === e.msg_id
            && !turn.liveTaskId
          ));
          clearAcceptance(rt);
        });
        return { ...next, banner: presentCommandProblem(e) };
      }
      if (e.msg_id) {
        const key = e.sid ?? state.focusedSid;
        const runtime = key ? state.runtimes[key] : undefined;
        const queued = runtime?.queue.find(
          (query) => query.msg_id === e.msg_id);
        const pending = runtime?.pendingSend?.msg_id === e.msg_id
          ? runtime.pendingSend : undefined;
        const deferred = queued ?? pending;
        if (deferred && key) {
          const problem = presentCommandProblem(e);
          if (deferred.queueState === "queued") {
            const next = patch(state, key, (rt) => {
              rt.queue = rt.queue.map((query) =>
                query.msg_id === e.msg_id
                  ? { ...query, queueError: problem }
                  : query);
              const currentPending = rt.pendingSend;
              if (
                currentPending !== null
                && currentPending.msg_id === e.msg_id
              ) {
                rt.pendingSend = {
                  ...currentPending, queueError: problem,
                };
              }
            });
            return { ...next, banner: problem };
          }
          const next = patch(state, key, (rt) => {
            rt.queue = rt.queue.filter(
              (query) => query.msg_id !== e.msg_id);
            if (rt.pendingSend?.msg_id === e.msg_id) {
              rt.pendingSend = null;
            }
            rt.failedDeferred = [
              ...rt.failedDeferred.filter(
                (query) => query.msg_id !== e.msg_id),
              {
                ...deferred,
                queueKind: deferred.queueKind
                  ?? (pending ? "replace" : "queue"),
                queueState: "failed",
                queueError: problem,
                retainedBytes: queuedQueryWireBytes(deferred),
                failedAt: Date.now(),
              },
            ];
          });
          return {
            ...next,
            runtimes: boundFailedDeferred(next.runtimes),
            banner: problem,
          };
        }
      }
      if (!e.msg_id) {
        return { ...state, banner: presentCommandProblem(e) };
      }
      return patch(state, e.sid, (rt) => {
        rt.loading = false; // never leave a spinner spinning behind an error
        if (rt.acceptancePending === e.msg_id) {
          clearAcceptance(rt);
        }
        markTurnAsLive(rt, e.msg_id!, boundCompletedTurns, e.seq);
        const turns = cloneTurns(rt.turns);
        const t = turns.find((turn) => turn.id === e.msg_id);
        if (t) {
          t.error = presentTurnProblem(e);
          t.terminalSource = "failed";
          t.progress = undefined;
          t.done = true;
          t.doneTs ??= Date.now();
          finishOpenBlocks(t, "failed", true);
        }
        else turns.push({ id: e.msg_id!, prompt: "", blocks: [], done: true,
          error: presentTurnProblem(e), terminalSource: "failed",
          doneTs: Date.now() });
        if (boundCompletedTurns) replaceWithBoundedTurns(rt, turns);
        else rt.turns = turns;
        rt.pendingQuestion = null;
      }, true);
    }
    case "user_msg": {
      const next = patch(state, e.sid, (rt) => {
        // query_queue removal normally precedes the user boundary. This fallback
        // also reconciles a replay gap or an older wrapper which emitted only
        // the accepted message.
        const acceptedIds = new Set(
          [e.msg_id, e.client_msg_id].filter(
            (id): id is string => !!id));
        rt.queue = rt.queue.filter(
          (query) => !query.msg_id || !acceptedIds.has(query.msg_id));
        if (rt.pendingSend?.msg_id
            && acceptedIds.has(rt.pendingSend.msg_id)) rt.pendingSend = null;
        rt.failedDeferred = rt.failedDeferred.filter(
          (query) => !query.msg_id || !acceptedIds.has(query.msg_id));
        if (rt.acceptancePending
            && acceptedIds.has(rt.acceptancePending)) {
          // Claude's native TurnBinding follows this exact user echo later.
          if (boundCompletedTurns && e.seq != null) {
            rt.liveOwner = { turnId: rt.acceptancePending, seq: e.seq };
          }
          clearAcceptance(rt);
        }
        markTurnAsLive(rt, e.msg_id, boundCompletedTurns, e.seq);
        if (e.client_msg_id && e.client_msg_id !== e.msg_id) {
          markTurnAsLive(
            rt, e.client_msg_id, boundCompletedTurns, e.seq);
        }
        const turns = cloneTurns(rt.turns);
        const existing = turns.find((turn) =>
          turnHasIdentityAlias(turn, e.msg_id)
          || turnHasIdentityAlias(turn, e.client_msg_id));
        const imgs = (e.images && e.images.length) ? e.images : undefined;
        const fileMeta = (e.files && e.files.length)
          ? e.files.map((file) => ({ filename: file.filename, data: "" }))
          : undefined;
        // Server time correlates the optimistic id with transcript history. The
        // client clock may drift, so authoritative echo time replaces it.
        const stamp = e.ts ? Math.round(e.ts * 1000) : undefined;
        if (existing) {
          if (!existing.prompt && e.prompt) existing.prompt = e.prompt;
          if (!existing.images && imgs) existing.images = imgs;
          if (fileMeta) existing.files = fileMeta;
          else if (existing.files) existing.files = existing.files.map(
            (file) => ({ filename: file.filename, data: "" }));
          if (stamp) existing.ts = stamp;
          if (e.client_msg_id) existing.clientMsgId ??= e.client_msg_id;
          if (e.client_msg_id === existing.id && e.msg_id !== existing.id) {
            existing.historyTurnId ??= e.msg_id;
          }
        } else {
          turns.push({
            id: e.msg_id,
            clientMsgId: e.client_msg_id ?? undefined,
            prompt: e.prompt,
            images: imgs,
            files: fileMeta,
            blocks: [],
            done: false,
            ts: stamp,
          });
        }
        const binding = rt.pendingLiveBinding;
        const boundNativeTurnId = binding
          && acceptedIds.has(binding.msgId) ? binding.turnId : undefined;
        rt.turns = reconcileBoundCompactionOrphan(
          rt,
          turns,
          [e.msg_id, e.client_msg_id],
          boundNativeTurnId,
        );
        applyPendingCodexTerminalFences(rt);
      });
      const sessions = e.sid
        ? bumpSessionActivity(next.sessions, e.sid, Math.round(e.ts * 1000))
        : next.sessions;
      return sessions === next.sessions ? next : { ...next, sessions };
    }
    case "turn_steered": {
      const next = patch(state, e.sid, (rt) => {
        if (rt.pendingLiveBinding
            && (typeof e.seq !== "number"
              || e.seq >= rt.pendingLiveBinding.seq)) {
          rt.pendingLiveBinding = null;
        }
        const turns = cloneTurns(rt.turns);
        const imgs = (e.images && e.images.length) ? e.images : undefined;
        const fileMeta = (e.files && e.files.length)
          ? e.files.map((file) => ({ filename: file.filename, data: "" }))
          : undefined;
        const stamp = eventTimestampMs(e.ts);
        const doneTs = stamp ?? Date.now();
        const localAcceptance = rt.acceptancePending === e.msg_id
          && (rt.acceptanceKind === "steer"
            || rt.acceptanceKind === "steer_unknown");
        const optimisticIndex = pendingOptimisticSteerIndex(rt, turns);
        let existing = turns.find((turn) =>
          turnHasIdentityAlias(turn, e.msg_id));
        if (existing) {
          if (localAcceptance) {
            // An external steer can be accepted while this browser's own
            // optimistic row waits for its echo. Keep every accepted segment
            // in source order and make the local segment the latest owner.
            turns.splice(turns.indexOf(existing), 1);
            const previous = [...turns].reverse()
              .find((turn) => !turn.done);
            if (previous) {
              finishTurnAtSteerFence(previous, e.turn_id, doneTs);
            }
            existing.done = false;
            existing.doneTs = undefined;
            existing.durationMs = undefined;
            existing.interrupted = undefined;
            existing.error = undefined;
            existing.progress = undefined;
            turns.push(existing);
            clearAcceptance(rt);
          }
          // Reliable-command replay can deliver the correlated narrative frame
          // again after reconnect. Other duplicates only refresh metadata.
          existing.prompt ||= e.prompt;
          existing.images ??= imgs;
          if (fileMeta) existing.files = fileMeta;
          existing.ts ??= stamp;
          existing.clientMsgId ??= e.msg_id;
          existing.liveTaskId ??= e.turn_id;
          if (boundCompletedTurns && typeof e.seq === "number") {
            rt.liveOwner = {
              turnId: existing.id,
              seq: Math.max(
                rt.liveOwner?.turnId === existing.id
                  ? rt.liveOwner.seq : 0,
                e.seq,
              ),
            };
          }
          markTurnAsLive(rt, existing.id, boundCompletedTurns, e.seq);
          rt.turns = turns;
          applyPendingCodexTerminalFences(rt);
          return;
        }

        const insertBeforeOptimistic = optimisticIndex >= 0
          && !localAcceptance;
        const predecessorPool = insertBeforeOptimistic
          ? turns.slice(0, optimisticIndex)
          : turns;
        const previous = [...predecessorPool].reverse()
          .find((turn) => !turn.done);
        if (previous) {
          finishTurnAtSteerFence(previous, e.turn_id, doneTs);
        }
        existing = {
          id: e.msg_id,
          clientMsgId: e.msg_id,
          liveTaskId: e.turn_id,
          prompt: e.prompt,
          images: imgs,
          files: fileMeta,
          blocks: [],
          done: false,
          ts: stamp,
        };
        if (insertBeforeOptimistic) {
          turns.splice(optimisticIndex, 0, existing);
        } else {
          turns.push(existing);
        }
        if (localAcceptance) clearAcceptance(rt);
        if (boundCompletedTurns && typeof e.seq === "number") {
          rt.liveOwner = { turnId: existing.id, seq: e.seq };
        }
        markTurnAsLive(rt, existing.id, boundCompletedTurns, e.seq);
        if (boundCompletedTurns) replaceWithBoundedTurns(rt, turns);
        else rt.turns = turns;
        applyPendingCodexTerminalFences(rt);
      }, true);
      const sessions = e.sid
        ? bumpSessionActivity(next.sessions, e.sid, Math.round(e.ts * 1000))
        : next.sessions;
      return sessions === next.sessions ? next : { ...next, sessions };
    }
    case "assistant_msg_start":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        const explicit = findMessageEventOwner(turns, e.message_id, e.turn_id);
        const t = explicit ?? (!e.turn_id
          ? findTurnOwningMessage(turns, e.message_id)
            ?? preSteerTurn(rt, turns)
            ?? openUnboundLiveTurn(
              rt, turns, e.message_id, eventTimestampMs(e.ts), e.seq)
          : undefined);
        // An explicit owner outside the materialized page must never fall
        // through to a newer live tail. Canonical History will restore it.
        if (!t) { rt.turns = turns; return; }
        const detachedBackground = t.done && (e.background === true
          || (!!rt.liveOwner && !turnHasIdentityAlias(t, rt.liveOwner.turnId)));
        if (!detachedBackground) {
          markTurnAsLive(rt, t.id, boundCompletedTurns, e.seq);
        }
        t.progress = undefined;
        const block = mutableTurnBlocks(t).find((b) => b.kind === "text"
          && b.message_id === e.message_id) as TextBlock | undefined;
        const stamp = eventTimestampMs(e.ts);
        if (block) {
          block.channel = resolvedChannel(block.channel, e.channel ?? "unknown");
          if (e.background === true) {
            block.startedTs ??= stamp;
            block.background = true;
          }
        }
        else {
          const nextBlock: TextBlock = {
            kind: "text", message_id: e.message_id, text: "",
            done: false, channel: e.channel ?? "unknown",
          };
          if (e.background === true) {
            nextBlock.startedTs = stamp;
            nextBlock.background = true;
          }
          appendLiveBlock(t, nextBlock);
          if (boundCompletedTurns) limitTurnBlocks(t);
        }
        rt.turns = turns;
      });
    case "delta":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        const explicit = findMessageEventOwner(turns, e.message_id, e.turn_id);
        const t = explicit ?? (!e.turn_id
          ? findTurnOwningMessage(turns, e.message_id)
            ?? preSteerTurn(rt, turns)
            ?? openUnboundLiveTurn(
              rt, turns, e.message_id, eventTimestampMs(e.ts), e.seq)
          : undefined);
        if (!t) { rt.turns = turns; return; }
        const detachedBackground = t.done && (e.background === true
          || (!!rt.liveOwner && !turnHasIdentityAlias(t, rt.liveOwner.turnId)));
        if (!detachedBackground) {
          markTurnAsLive(rt, t.id, boundCompletedTurns, e.seq);
        }
        t.progress = undefined;
        let block = mutableTurnBlocks(t).find((b) => b.kind === "text"
          && b.message_id === e.message_id) as TextBlock | undefined;
        if (!block) {
          block = { kind: "text", message_id: e.message_id, text: "", done: false,
            channel: e.channel ?? "unknown" };
          if (e.background === true) {
            block.startedTs = eventTimestampMs(e.ts);
            block.background = true;
          }
          appendLiveBlock(t, block);
          if (boundCompletedTurns) limitTurnBlocks(t);
        }
        block.channel = resolvedChannel(block.channel, e.channel ?? "unknown");
        if (e.background === true) {
          block.startedTs ??= eventTimestampMs(e.ts);
          block.background = true;
        }
        // History can win the race against an app-server replay and install the
        // completed native item before its delayed deltas arrive. Native message
        // ids are immutable, so a completed exact block is authoritative. Never
        // use text containment here: repeated prose and bounded History prefixes
        // are both legitimate content and cannot safely prove replay identity.
        if (!block.done) {
          block.text = appendField(block.text, e.text, MAX_LIVE_TEXT_CHARS);
        }
        if (block.channel !== "final" && e.text.length > 0) {
          markTurnDetailAsLive(rt, t.id, boundCompletedTurns);
        }
        if (block.channel === "commentary" && e.text.length > 0) {
          markVisibleProcessStarted(t, eventTimestampMs(e.ts));
        }
        if (boundCompletedTurns) limitTurnBlocks(t);
        rt.turns = turns;
      });
    case "tool_use":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        const explicit = findTurnByEngineId(turns, e.turn_id);
        const t = explicit ?? (!e.turn_id
          ? findTurnOwningItem(turns, e.tool_use_id)
            ?? findTurnOwningMessage(turns, e.message_id)
            ?? preSteerTurn(rt, turns)
            ?? openUnboundLiveTurn(
              rt, turns, e.message_id, eventTimestampMs(e.ts), e.seq)
          : undefined);
        if (!t) { rt.turns = turns; return; }
        const detachedBackground = e.background === true && t.done;
        if (!detachedBackground) {
          markTurnAsLive(rt, t.id, boundCompletedTurns, e.seq);
        }
        markTurnDetailAsLive(rt, t.id, boundCompletedTurns);
        t.progress = undefined;
        const existing = mutableTurnBlocks(t).find((b) => b.kind === "tool"
          && b.tool_use_id === e.tool_use_id) as ToolBlock | undefined;
        if (existing) {
          existing.tool = e.tool;
          existing.input = e.input;
          existing.category = e.category ?? "tool";
          existing.title = e.title;
          existing.parent_id = e.parent_id;
          existing.server = e.server;
          if (e.background === true) {
            existing.startedTs ??= eventTimestampMs(e.ts);
            existing.background = true;
          }
        } else {
          const nextBlock: ToolBlock = { kind: "tool", message_id: e.message_id,
            tool_use_id: e.tool_use_id, tool: e.tool, input: e.input,
            category: e.category ?? "tool", title: e.title, parent_id: e.parent_id,
            server: e.server, done: false };
          if (e.background === true) {
            nextBlock.startedTs = eventTimestampMs(e.ts);
            nextBlock.background = true;
          }
          appendLiveBlock(t, nextBlock);
          if (boundCompletedTurns) limitTurnBlocks(t);
        }
        markVisibleProcessStarted(t, eventTimestampMs(e.ts));
        rt.turns = turns;
      });
    case "tool_delta":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        const explicit = findTurnByEngineId(turns, e.turn_id);
        const candidates = e.turn_id ? [explicit] : turns;
        for (const t of candidates) {
          if (!t) continue;
          const block = mutableTurnBlocks(t).find((b) => b.kind === "tool"
            && b.tool_use_id === e.tool_use_id) as ToolBlock | undefined;
          if (!block) continue;
          if (e.background === true) block.background = true;
          const detachedBackground = e.background === true && t.done;
          if (!detachedBackground) {
            markTurnAsLive(rt, t.id, boundCompletedTurns, e.seq);
          }
          markTurnDetailAsLive(rt, t.id, boundCompletedTurns);
          markVisibleProcessStarted(t, eventTimestampMs(e.ts));
          if (e.stream === "progress" || e.stream === "summary") {
            block.progress = appendField(
              block.progress, e.delta, MAX_LIVE_PROGRESS_CHARS);
          } else if (e.stream === "diff") {
            block.diff = appendField(block.diff, e.delta, MAX_LIVE_DIFF_CHARS);
          } else {
            block.output = appendField(
              block.output, e.delta, MAX_LIVE_TOOL_OUTPUT_CHARS);
          }
          t.progress = undefined;
          if (boundCompletedTurns) limitTurnBlocks(t);
          break;
        }
        rt.turns = turns;
      });
    case "tool_result":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        const explicit = findTurnByEngineId(turns, e.turn_id);
        const candidates = e.turn_id ? [explicit] : turns;
        for (const t of candidates) {
          if (!t) continue;
          const b = mutableTurnBlocks(t).find((b) => b.kind === "tool"
            && b.tool_use_id === e.tool_use_id) as ToolBlock | undefined;
          if (b) {
            const detachedBackground = e.background === true && t.done;
            if (!detachedBackground) {
              markTurnAsLive(rt, t.id, boundCompletedTurns, e.seq);
            }
            markTurnDetailAsLive(rt, t.id, boundCompletedTurns);
            b.result = { content: e.content, is_error: e.is_error,
              truncated: e.truncated ?? undefined, status: e.status,
              summary: e.summary, diff: e.diff, exit_code: e.exit_code,
              duration_ms: e.duration_ms };
            if ("diff" in e) b.diff = e.diff ?? undefined;
            b.done = true;
            if (e.background === true || b.background === true) {
              b.doneTs = eventTimestampMs(e.ts);
              b.background = true;
            }
            t.progress = undefined;
            markVisibleProcessStarted(t, eventTimestampMs(e.ts));
            settleVisibleProcessIfComplete(t, eventTimestampMs(e.ts));
            if (boundCompletedTurns) limitTurnBlocks(t);
            break;
          }
        }
        rt.turns = turns;
      });
    case "assistant_msg_end":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        const explicit = findMessageEventOwner(turns, e.message_id, e.turn_id);
        const candidates = e.turn_id ? [explicit] : turns;
        for (const t of candidates) {
          if (!t) continue;
          const b = mutableTurnBlocks(t).find((b) => b.kind === "text"
            && b.message_id === e.message_id) as TextBlock | undefined;
          if (b) {
            const detachedBackground = t.done && (e.background === true
              || (!!rt.liveOwner && !turnHasIdentityAlias(t, rt.liveOwner.turnId)));
            if (!detachedBackground) {
              markTurnAsLive(rt, t.id, boundCompletedTurns, e.seq);
            }
            b.channel = resolvedChannel(b.channel, e.channel ?? "unknown");
            b.done = true;
            if (e.delivery === "async") {
              b.delivery = "async";
              if (e.questions?.length) b.questions = e.questions;
            }
            if (e.background === true || b.background === true) {
              b.doneTs = eventTimestampMs(e.ts);
              b.background = true;
            }
            if (b.channel === "commentary" && b.text.length > 0) {
              settleVisibleProcessIfComplete(t, eventTimestampMs(e.ts));
            }
            if (e.delivery === "async" && boundCompletedTurns) limitTurnBlocks(t);
            break;
          }
        }
        rt.turns = turns;
      });
    case "background_process_sync":
      return patch(state, e.sid, (rt) => {
        const runtimeGeneration = runtimeOrderingGeneration(rt);
        if (e.generation && runtimeGeneration
            && e.generation !== runtimeGeneration) return;
        if (e.generation) switchControlGeneration(rt, e.generation);
        rt.backgroundProcesses = e.items
          .slice(0, MAX_BACKGROUND_PROCESS_ITEMS)
          .map(backgroundProcessBlock);
        rt.backgroundLevelEmpty = e.items.length === 0;
        rt.backgroundLevelTs = eventTimestampMs(e.ts);
        reconcileAuthoritativeBackgroundProcessLevel(rt);
      });
    case "process":
      return patch(state, e.sid, (rt) => {
        applyBackgroundProcessEdge(rt, e);
        const turns = cloneTurns(rt.turns);
        let owner: Turn | undefined;
        let block: ProcessBlock | undefined;
        for (const candidate of turns) {
          const found = mutableTurnBlocks(candidate).find((b) => b.kind === "process"
            && b.item_id === e.item_id) as ProcessBlock | undefined;
          if (found) { owner = candidate; block = found; break; }
        }
        // Background task/hook events may arrive after their originating turn
        // ended and after a newer query opened. Prefer their explicit parent or
        // engine turn id before falling back to the current tail; otherwise a
        // delayed subagent update creates a phantom new turn or attaches to the
        // wrong conversation.
        if (!owner) owner = findTurnOwningItem(turns, e.parent_id);
        if (!owner) {
          owner = findBoundLiveTaskOwner(
            rt, turns, e.turn_id, e.seq, true);
        }
        if (!owner) owner = findTurnByEngineId(turns, e.turn_id);
        if (!owner) {
          owner = openTurn(
            turns, e.turn_id || e.item_id, eventTimestampMs(e.ts));
        }
        const detachedBackground = e.background === true && owner.done;
        if (!detachedBackground) {
          markTurnAsLive(rt, owner.id, boundCompletedTurns, e.seq);
        }
        markTurnDetailAsLive(rt, owner.id, boundCompletedTurns);
        if (!block) {
          block = { kind: "process", item_id: e.item_id, processKind: e.kind,
            phase: e.phase, status: e.status, turn_id: e.turn_id,
            parent_id: e.parent_id, title: e.title, done: false };
          appendLiveBlock(owner, block);
        }
        block.processKind = e.kind;
        block.phase = e.phase;
        block.status = e.status;
        block.turn_id = e.turn_id ?? block.turn_id;
        block.parent_id = e.parent_id ?? block.parent_id;
        block.title = e.title || block.title;
        if (e.summary != null) block.summary = e.summary;
        if (e.detail != null) block.detail = e.detail;
        if (e.input != null) block.input = e.input;
        if (e.output != null) block.output = e.output;
        if (e.diff != null) block.diff = e.diff;
        if (e.progress != null) block.progress = e.progress;
        if (e.server != null) block.server = e.server;
        if (e.tool != null) block.tool = e.tool;
        if (e.command != null) block.command = e.command;
        if (e.cwd != null) block.cwd = e.cwd;
        if (e.exit_code != null) block.exit_code = e.exit_code;
        if (e.duration_ms != null) block.duration_ms = e.duration_ms;
        if (e.truncated != null) block.truncated = e.truncated;
        if (e.background != null) block.background = e.background;
        if (block.background === true) {
          block.startedTs ??= eventTimestampMs(e.ts);
          block.updatedTs = eventTimestampMs(e.ts) ?? block.updatedTs;
        }
        if (e.append_to && e.delta) {
          if (e.append_to === "summary") {
            block.summary = appendField(
              block.summary, e.delta, MAX_LIVE_PROGRESS_CHARS);
          } else if (e.append_to === "detail") {
            block.detail = appendField(
              block.detail, e.delta, MAX_LIVE_DETAIL_CHARS);
          } else if (e.append_to === "output") {
            block.output = appendField(
              block.output, e.delta, MAX_LIVE_TOOL_OUTPUT_CHARS);
          } else if (e.append_to === "diff") {
            block.diff = appendField(block.diff, e.delta, MAX_LIVE_DIFF_CHARS);
          } else {
            block.progress = appendField(
              block.progress, e.delta, MAX_LIVE_PROGRESS_CHARS);
          }
        }
        const wasDone = block.done;
        block.done = e.phase === "end" || terminalProcessStatus(e.status);
        if (block.background === true && block.done && !wasDone) {
          block.terminalOrder = allocateLiveOrder(owner);
          block.terminalTs = eventTimestampMs(e.ts);
        }
        if (isStateVisibleProcessBlock(block)) {
          markVisibleProcessStarted(owner, eventTimestampMs(e.ts));
          if (block.done) {
            settleVisibleProcessIfComplete(owner, eventTimestampMs(e.ts));
          }
        }
        if (!detachedBackground) owner.progress = undefined;
        if (boundCompletedTurns) limitTurnBlocks(owner);
        rt.turns = turns;
        applyPendingCodexTerminalFences(rt);
      });
    case "turn_plan":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        let t = findTurnOwningItem(turns, e.item_id)
          ?? findBoundLiveTaskOwner(rt, turns, e.turn_id, e.seq, true)
          ?? findTurnByEngineId(turns, e.turn_id);
        if (!t) {
          t = openTurn(
            turns, e.turn_id || e.item_id, eventTimestampMs(e.ts));
        }
        markTurnAsLive(rt, t.id, boundCompletedTurns, e.seq);
        markTurnDetailAsLive(rt, t.id, boundCompletedTurns);
        let block = mutableTurnBlocks(t).find((b) => b.kind === "process"
          && b.item_id === e.item_id) as ProcessBlock | undefined;
        if (!block) {
          block = { kind: "process", item_id: e.item_id, processKind: "plan",
            phase: "snapshot", status: "running", turn_id: e.turn_id,
            title: "计划", done: false };
          appendLiveBlock(t, block);
        }
        block.explanation = e.explanation;
        block.plan = e.plan.map((entry) => ({ ...entry }));
        block.status = e.plan.length > 0 && e.plan.every((entry) => entry.status === "completed")
          ? "succeeded" : "running";
        block.done = block.status === "succeeded";
        markVisibleProcessStarted(t, eventTimestampMs(e.ts));
        if (block.done) {
          settleVisibleProcessIfComplete(
            t, eventTimestampMs(e.ts) ?? t.doneTs);
        }
        t.progress = undefined;
        if (boundCompletedTurns) limitTurnBlocks(t);
        rt.turns = turns;
      });
    case "turn_file_changes":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        const turn = findBoundLiveTaskOwner(rt, turns, e.turn_id, e.seq, true)
          ?? findTurnByEngineId(turns, e.turn_id);
        if (!turn) return;
        turn.fileChanges = e.changes;
        turn.fileChangesTurnId = e.turn_id;
        rt.turns = turns;
      });
    case "turn_diff":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        let t = findTurnOwningItem(turns, e.item_id)
          ?? findBoundLiveTaskOwner(rt, turns, e.turn_id, e.seq, true)
          ?? findTurnByEngineId(turns, e.turn_id);
        if (!t) {
          t = openTurn(
            turns, e.turn_id || e.item_id, eventTimestampMs(e.ts));
        }
        markTurnAsLive(rt, t.id, boundCompletedTurns, e.seq);
        markTurnDetailAsLive(rt, t.id, boundCompletedTurns);
        let block = mutableTurnBlocks(t).find((b) => b.kind === "process"
          && b.item_id === e.item_id) as ProcessBlock | undefined;
        if (!block) {
          block = { kind: "process", item_id: e.item_id, processKind: "diff",
            phase: "snapshot", status: "running", turn_id: e.turn_id,
            title: "代码改动", done: false };
          appendLiveBlock(t, block);
        }
        block.diff = e.diff;
        block.truncated = e.truncated;
        // TurnDiff is a complete snapshot, not an open stream. A later
        // snapshot may update it, but this event itself has a terminal
        // boundary and must not keep the process clock running until TurnEnd.
        block.phase = "snapshot";
        block.status = "succeeded";
        block.done = true;
        markVisibleProcessStarted(t, eventTimestampMs(e.ts));
        settleVisibleProcessIfComplete(t, eventTimestampMs(e.ts));
        t.progress = undefined;
        if (boundCompletedTurns) limitTurnBlocks(t);
        rt.turns = turns;
      });
    case "turn_binding":
      return patch(state, e.sid, (rt) => {
        if (rt.acceptancePending === e.msg_id) {
          clearAcceptance(rt);
        }
        let turns = cloneTurns(rt.turns);
        const seq = typeof e.seq === "number" ? e.seq : 0;
        const binding = {
          msgId: e.msg_id,
          turnId: e.turn_id,
          seq,
          generation: runtimeOrderingGeneration(rt),
        };
        const currentOwner = rt.liveOwner
          ? turns.find((turn) =>
              turnHasIdentityAlias(turn, rt.liveOwner!.turnId))
          : undefined;
        const bindingOwnsCurrentRow = !!currentOwner
          && turnHasIdentityAlias(currentOwner, e.msg_id);
        const bindingCanSupersedeOwner = !rt.liveOwner
          || bindingOwnsCurrentRow || seq > rt.liveOwner.seq;
        const current = rt.pendingLiveBinding;
        if (bindingCanSupersedeOwner && (
          !current || current.generation !== binding.generation
            || seq >= current.seq
        )) {
          rt.pendingLiveBinding = binding;
        }
        if (bindingCanSupersedeOwner) {
          bindAuthoritativeActiveHistoryHead(
            rt, turns, e.msg_id, e.turn_id, seq);
        }
        // A native Codex task may contain multiple visible steer rows. Once a
        // newer row owns that task, an older delayed binding cannot prove which
        // row a standalone compaction belongs to; leave it for canonical
        // History instead of moving it into the completed predecessor.
        if (bindingCanSupersedeOwner) {
          turns = reconcileBoundCompactionOrphan(
            rt, turns, [e.msg_id], e.turn_id);
        }
        const exact = turns.filter((turn) =>
          turnHasIdentityAlias(turn, e.msg_id));
        if (exact.length === 1) {
          const owner = exact[0];
          const ownerIndex = turns.indexOf(owner);
          const wasInitialUnboundOwner = !owner.done
            && !turnHasBoundEngineId(owner);
          const authoritativeCandidates = turns.filter((turn, index) =>
            index !== ownerIndex
            && (turn.forkPointId === e.turn_id || turn.id === e.turn_id));
          if (!owner.forkPointId || owner.forkPointId === e.turn_id
              || owner.liveTaskId === e.turn_id) {
            owner.forkPointId = e.turn_id;
            if (!owner.done && boundCompletedTurns) {
              rt.liveOwner = { turnId: owner.id, seq };
            }
          }
          // The initial Query may materialize in History before its first
          // TurnBinding.  It is the only safe native-id-only merge: steer rows
          // already carry liveTaskId, and completed predecessors must never be
          // collapsed into a later segment which shares this native task.
          if (wasInitialUnboundOwner
              && authoritativeCandidates.length === 1) {
            const authoritative = authoritativeCandidates[0];
            const authoritativeIndex = turns.indexOf(authoritative);
            const mergedTurns = mergeInitialHistory(
              [authoritative], [owner]);
            if (mergedTurns.length === 1) {
              const merged = mergedTurns[0];
              merged.forkPointId = e.turn_id;
              const first = Math.min(ownerIndex, authoritativeIndex);
              const second = Math.max(ownerIndex, authoritativeIndex);
              turns.splice(second, 1);
              turns.splice(first, 1, merged);
              rt.liveOwner = { turnId: merged.id, seq };
            }
          }
        }
        if (boundCompletedTurns) replaceWithBoundedTurns(rt, turns);
        else rt.turns = turns;
        applyPendingCodexTerminalFences(rt);
      });
    case "turn_end":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        const unknownSteerOwner = preSteerTurn(rt, turns);
        let t = findBoundLiveTaskOwner(
          rt, turns, e.turn_id, e.seq, false)
          ?? findTurnByEngineId(turns, e.turn_id);
        if (!t && e.checkpoint_id) {
          // Claude binds the optimistic row to its native user/checkpoint UUID,
          // while ResultMessage identifies the terminal with the final assistant
          // UUID.  Codex has no checkpoint_id and keeps the strict turn_id path
          // above; this exact Claude identity fallback must happen before the
          // legacy single-open-row heuristic rejects an already-bound owner.
          t = findBoundLiveTaskOwner(
            rt, turns, e.checkpoint_id, e.seq, false)
            ?? findTurnByEngineId(turns, e.checkpoint_id);
        }
        if (!t) {
          const openTurns = turns.filter((turn) => !turn.done
            && !(rt.acceptanceKind === "steer_unknown"
              && turn.id === rt.acceptancePending
              && turn.clientMsgId === rt.acceptancePending
              && !turn.liveTaskId));
          // Claude and older producers may reveal the native id only at the
          // terminal boundary. Preserve that legacy path when there is exactly
          // one unclosed owner, while never closing an unrelated completed row.
          if (openTurns.length === 1
              && !turnHasBoundEngineId(openTurns[0])) {
            t = openTurns[0];
          }
        }
        const terminalClosedOpenTurn = !!t && !t.done;
        if (t) {
          markTurnAsLive(rt, t.id, boundCompletedTurns, e.seq);
          if (rt.acceptancePending === t.id) {
            clearAcceptance(rt);
          }
          t.done = true;
          t.durationMs = e.result.subtype === "steered"
              && e.result.duration_ms === 0
            ? undefined
            : e.result.duration_ms;
          if (e.turn_id) {
            t.forkPointId = e.turn_id;
            t.liveTaskId = undefined;
          }
          if (e.checkpoint_id) t.checkpointId = e.checkpoint_id;
          t.progress = undefined;
          if (e.result.subtype === "error_during_execution") t.interrupted = true;
          if (e.result.is_error) {
            if (e.result.subtype !== "error_during_execution") {
              t.error ??= "本次回复未完成，请重试。";
            }
            t.terminalSource = e.result.subtype === "error_during_execution"
              ? (rt.state === "interrupting" || rt.state === "draining")
                ? "remote_interrupt"
                : "unexpected_interrupt"
              : "failed";
          } else {
            delete t.terminalSource;
          }
          // Stamp completion time from the event's own server ts (seconds -> ms).
          // Robust for BOTH live turns and replayed history: the old
          // `t.ts + duration_ms` reconstruction dropped the timestamp for any turn
          // without a client-side start time (i.e. everything after a refresh,
          // where turns come from history replay). Fall back to start time, then now.
          t.doneTs = e.ts ? Math.round(e.ts * 1000) : (t.ts || Date.now());
          finishOpenBlocks(
            t,
            e.result.is_error ? "interrupted" : "succeeded",
            e.result.is_error,
            e.result.subtype === "steered",
          );
          settleVisibleProcessIfComplete(
            t, eventTimestampMs(e.ts) ?? t.doneTs);
          if (t.liveBlocksSpilled) {
            // Refresh the newest source-backed page at the terminal boundary.
            // If a running snapshot is already in flight, keep this pending
            // bit through that response and issue one terminal snapshot next.
            t.detailLoaded = false;
            t.detailRestorePending = true;
            t.detailRestoreIncomplete = true;
          }
        }
        const terminalBinding = rt.pendingLiveBinding;
        if (terminalBinding
            && (terminalBinding.turnId === e.turn_id
              || terminalBinding.turnId === e.checkpoint_id)
            && (typeof e.seq !== "number"
              || e.seq >= terminalBinding.seq)) {
          rt.pendingLiveBinding = null;
        }
        if (t && t === unknownSteerOwner) {
          resolveUnknownPendingSteer(
            rt, turns, eventTimestampMs(e.ts) ?? Date.now());
        }
        if (boundCompletedTurns) replaceWithBoundedTurns(rt, turns);
        else rt.turns = turns;
        applyPendingCodexTerminalFences(rt);
        if (terminalClosedOpenTurn) {
          discardPendingCodexTerminalFence(rt, e.turn_id);
        }
        // TurnEnd closes the visible turn, but the wrapper may still be
        // draining an interrupt, finishing a checkpoint, or releasing its
        // app-server consumer. Only the following authoritative State(idle)
        // unlocks the composer and queued/pending sends.
        rt.pendingQuestion = null;
      });
    case "pong":
    case "command_ack":
    case "queued_query_detail":
    case "queued_query_updated":
    case "history_image":
    case "session_forked":
    case "session_list_invalidated":
    case "hello":
      return state;
  }
}
