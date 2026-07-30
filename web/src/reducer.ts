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
  ServerEvent, SessionInfo, State, ContextReport, StatusReport, ThreadGoal,
  QueryImg, QueryFile, DirEntry, AssistantChannel, ProcessStatus,
  CollaborationModeName, Notice, RateLimitUpdate,
  StatusRateLimit, StatusRateWindow, SessionControl, PermissionProfileInfo,
} from "./protocol";
import type { SendMode } from "./composer-submit";
import {
  compareSessionControl, sessionControlLocksInput, sessionControlTargetsSid,
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
  mergeAuthoritativeTurnDetail, mergeInitialHistory,
  restoreCachedTurnDetails,
} from "./history-merge";
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
import { presentCommandProblem, presentTurnProblem } from "./problem-presentation";
import {
  matchQueryAcceptanceHistory,
  queryAcceptanceDescriptor,
  type QueryAcceptanceHistoryHead,
} from "./outbox";
import type {
  Block,
  ProcessBlock,
  TextBlock,
  ToolBlock,
  Turn,
} from "./domain/conversation";
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
// Notices are ephemeral UI control state, not transcript history.  Eight keeps
// simultaneous startup/config/security warnings available without allowing a
// noisy app-server to grow every resident session indefinitely.
export const MAX_SESSION_NOTICES = 8;

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
  mediaType?: string;
  data?: string;
  error?: string;
}

export interface Artifact {
  file: string;
  kind: "diff" | "md" | "file" | "gitdiff" | "html" | "image" | "pdf";
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
  saveRequestId?: string;
  saving?: boolean;
  saveStatus?: "saved" | "conflict" | "error";
  saveError?: string;
  pendingContent?: string;
  line?: number;
  error?: string;
  assets?: Record<string, PreviewAssetState>;
}

export interface SessionRuntime {
  turns: Turn[];
  state: State;
  // Display-only activity observed from a native/external client. It must not
  // grant Stop/Interrupt semantics to a turn this wrapper does not own.
  mirroredRunning: boolean;
  model: string;
  effort: string;
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
  // Native newest turn id from the last authoritative first History page. A
  // query freezes this together with revision/build/live watermarks so a later
  // materialized page can prove acceptance even when its UserMsg id is native.
  historyNewestId: string | null;
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
  contextRequestId: string | null;
  contextError: string | null;
  goal: ThreadGoal | null;
  statusReport: StatusReport | null;
  statusRequestId: string | null;
  statusError: string | null;
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
  } | null;
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
  // /btw ephemeral side-forks are owned by their parent sessions. Their
  // runtimes live under each binding's `sid`; navigation only changes which
  // binding is visible and never reassigns a fork to another parent.
  btwByParentSid: Record<string, { sid: string; engine: string }>;
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

export function createRuntime(): SessionRuntime {
  return {
    // These are authoritative engine settings.  A newly-created browser runtime
    // has not heard them yet, so keep them unknown instead of briefly claiming a
    // model, effort, or permission policy that may not match the native CLI.
    turns: [], state: "idle", mirroredRunning: false,
    model: "", effort: "", perm: "",
    permissionProfile: null, permissionProfiles: null, webSearch: null,
    collaborationMode: "default",
    fast: null,
    control: null, controlGeneration: null, hasRevisionedControl: false,
    takeoverPending: false, takeoverMessage: null,
    replaying: false, syncReady: false, truncated: false,
    historyInvalidated: false,
    historyRevision: null, pendingHistoryRevision: null,
    historyGeneration: null, pendingHistoryGeneration: null,
    pendingHistoryCandidateBuildSeq: null,
    historyBuildSeq: 0, historyLiveSeq: 0,
    lastLiveSeq: 0, lastLifecycleSeq: 0,
    hasLoadedOlderHistory: false,
    hydratedCacheTurnIds: [],
    historyNewestId: null,
    pendingQuestion: null, contextReport: null,
    contextRequestId: null, contextError: null, goal: null,
    statusReport: null, statusRequestId: null, statusError: null,
    notices: [], sendMode: "steer",
    queue: [], pendingSend: null, failedDeferred: [],
    acceptancePending: null,
    acceptanceHistoryBaseline: null,
  };
}

export type Action =
  | { type: "reset" }
  | { type: "event"; event: ServerEvent; ownership?: EventOwnership }
  | { type: "query_sent"; sid: string; prompt: string; msg_id: string; images?: QueryImg[]; files?: QueryFile[]; ts: number }
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
  | { type: "begin_status_request"; sid: string; requestId: string }
  | { type: "set_turns"; sid: string; turns: Turn[] }
  | { type: "set_artifact"; artifact: Artifact }
  | { type: "open_artifact_loading"; file: string; sid: string | null; requestId: string }
  | { type: "open_file_loading"; file: string; sid: string | null; requestId: string; kind: "md" | "file"; line?: number }
  | { type: "start_file_save"; requestId: string; content: string }
  | { type: "clear_artifact" }
  | { type: "clear_btw"; parentSid: string }
  | { type: "clear_all_btw" }
  | { type: "clear_session_list" }
  | { type: "restore_session_list"; sessions: SessionInfo[] }
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
  | { type: "history_browse_detail"; sid: string; scopeKey: string; revision: string; viewId: string; windowEpoch: number; turnId: string; events: ServerEvent[]; before?: string | null; hasMore?: boolean; oldestCursor?: string | null; hasNewer?: boolean; newerCursor?: string | null }
  | { type: "history_detail_cancelled"; context: HistoryDetailRequestContext }
  | { type: "return_to_latest"; sid: string }
  | { type: "hydrate_cache"; sid: string; turns: Turn[]; revision: string | null; generation?: string | null; control?: SessionControl | null }
  | { type: "prune_runtimes"; protectedSids: string[] }
  | { type: "answer_question"; sid: string; ask_id: string }
  | { type: "dismiss_notice"; sid: string; noticeId: string }
  | { type: "enter_new_chat"; cwd: string; cwdSource?: "default" | "inherited" | "explicit"; model?: string | null; effort?: string | null }
  | { type: "set_new_chat_cwd"; cwd: string; cwdSource?: "default" | "inherited" | "explicit" }
  | { type: "clear_scope_cwd"; scopeKey: string }
  | { type: "set_new_chat_model"; model: string | null }
  | { type: "set_new_chat_effort"; effort: string | null }
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
  sessions: [],
  focusedSid: null,
  runtimes: {},
  queryQueueCount: 0,
  queryQueueBytes: 0,
  historyRecovery: null,
  historyBrowse: null,
  btwByParentSid: {},
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
  return turns.map((t) => ({ ...t, blocks: t.blocks.map((b) => ({ ...b })) }));
}

function openTurn(turns: Turn[], fallbackId: string, ts?: number): Turn {
  let turn = turns[turns.length - 1];
  if (!turn || turn.done) {
    turn = { id: fallbackId, prompt: "", blocks: [], done: false, ts };
    turns.push(turn);
  }
  return turn;
}

function eventTimestampMs(ts: number | null | undefined): number | undefined {
  return typeof ts === "number" ? Math.round(ts * 1000) : undefined;
}

function findTurnByEngineId(turns: Turn[], id: string | null | undefined): Turn | undefined {
  if (!id) return undefined;
  return [...turns].reverse().find((turn) =>
    turn.id === id || turn.liveTaskId === id
    || turn.forkPointId === id || turn.codexTurnId === id
    || turn.blocks.some((block) => block.kind === "process"
      && block.turn_id === id));
}

function findTurnOwningItem(turns: Turn[], id: string | null | undefined): Turn | undefined {
  if (!id) return undefined;
  return [...turns].reverse().find((turn) => turn.blocks.some((block) =>
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

function omissionBlock(): ProcessBlock {
  return {
    kind: "process",
    item_id: OMITTED_PROCESS_ITEM_ID,
    processKind: "compaction",
    phase: "snapshot",
    status: "succeeded",
    title: "较早过程已省略",
    summary: "为限制此回合的内存占用，较早的处理记录未显示。",
    done: true,
  };
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
    return 128 + block.message_id.length + block.text.length;
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

/** Mutate one cloned turn into a fixed-size display window.
 *
 * Keep at least the newest known final answer and newest live activity, then
 * prefer the remaining final blocks, remaining live blocks, and newest
 * completed process records in that order.  A single fixed marker replaces all
 * evicted items; it deliberately carries no ever-growing counter or id set. */
function limitTurnBlocks(turn: Turn): void {
  const markerCount = turn.blocks.reduce(
    (count, block) => count + (isOmissionBlock(block) ? 1 : 0), 0);
  if (turn.blocks.length <= MAX_TURN_BLOCKS && markerCount <= 1
      && turnBlockPayloadChars(turn.blocks) <= MAX_TURN_BLOCK_CHARS) return;

  const candidates = turn.blocks.filter((block) => !isOmissionBlock(block))
    .map(limitedBlockPayload);
  const capacity = MAX_TURN_BLOCKS - 1;
  const keep = new Set<number>();
  let retainedChars = blockPayloadChars(omissionBlock());
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
  turn.blocks = [omissionBlock(), ...retained];
}

function withLimitedTurnBlocks(turn: Turn): Turn {
  if (turn.blocks.length <= MAX_TURN_BLOCKS
      && turn.blocks.filter(isOmissionBlock).length <= 1
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
): void {
  for (const block of turn.blocks) {
    if (block.kind === "text") {
      block.done = true;
    } else if (block.kind === "process" && !block.done) {
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

/** Reconcile an unfinished browser tail against a current authoritative History
 * snapshot which explicitly says the session is idle. This is a lost-terminal
 * recovery path: keep already-rendered text/process detail, but never leave its
 * timer and child blocks running forever. */
function finishOpenTurnsFromIdleHistory(
  turns: Turn[], interrupted: boolean, doneTs: number,
): Turn[] {
  return turns.map((turn) => {
    if (turn.done) return turn;
    const next = { ...turn, blocks: turn.blocks.map((block) => ({ ...block })) };
    next.done = true;
    next.doneTs ??= doneTs;
    next.progress = undefined;
    if (interrupted) next.interrupted = true;
    finishOpenBlocks(
      next, interrupted ? "interrupted" : "succeeded", interrupted);
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

function markTurnAsLive(
  runtime: SessionRuntime, turnId: string, liveEvent: boolean,
  eventSeq?: number | null,
): void {
  if (!liveEvent) return;
  if (typeof eventSeq === "number") {
    runtime.lastLiveSeq = Math.max(runtime.lastLiveSeq, eventSeq);
  }
  if (runtime.hydratedCacheTurnIds.length > 0) {
    runtime.hydratedCacheTurnIds = runtime.hydratedCacheTurnIds.filter(
      (cachedId) => cachedId !== turnId);
  }
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
  if (!generation || generation === runtime.controlGeneration) return;
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
): StatusRateWindow | null | undefined {
  if (!update) return current;
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

function mergeRateLimitUpdate(
  report: StatusReport | null, update: RateLimitUpdate,
): StatusReport | null {
  if (!report) return null;
  const limits = report.rate_limits.map((limit) => ({ ...limit }));
  let index = update.limit_id
    ? limits.findIndex((limit) => limit.limit_id === update.limit_id)
    : limits.length === 1 ? 0 : -1;
  if (index < 0) {
    index = limits.length;
    limits.push({});
  }
  const current = limits[index];
  const next: StatusRateLimit = { ...current };
  if (update.limit_id != null) next.limit_id = update.limit_id;
  if (update.name != null) next.limit_name = update.name;
  if (update.plan_type != null) next.plan_type = update.plan_type;
  if (update.reached_type != null) {
    next.rate_limit_reached_type = update.reached_type;
  }
  next.primary = mergeRateWindow(current.primary, update.primary);
  next.secondary = mergeRateWindow(current.secondary, update.secondary);
  limits[index] = next;
  return { ...report, rate_limits: limits.slice(-16) };
}

export function reduce(state: AppState, action: Action): AppState {
  switch (action.type) {
    case "reset":
      return {
        ...initialState,
        sessions: [], runtimes: {}, artifact: null, dirPicker: null,
        newChat: null, btwByParentSid: {}, catalog: {}, catalogDefault: {},
        catalogDefaultEffort: {}, catalogDefaultCwd: {},
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
        banner,
      };
    }
    case "command_error":
      return { ...state, banner: action.detail };
    case "dismiss_banner":
      return state.banner === action.banner
        ? { ...state, banner: undefined }
        : state;
    case "query_sent": {
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
        images: action.images,
        files: action.files?.map((file) => ({ filename: file.filename, data: "" })),
        ts: action.ts,
      };
      let runtimes = reduceTargetedRuntime(
        state.runtimes, action.sid, { type: "query_sent", turn });
      if (runtimes[action.sid]?.acceptancePending !== action.msg_id) {
        runtimes = {
          ...runtimes,
          [action.sid]: {
            ...runtimes[action.sid],
            acceptancePending: action.msg_id,
            acceptanceHistoryBaseline,
          },
        };
      }
      const sessions = bumpSessionActivity(state.sessions, action.sid, action.ts);
      const historyBrowse = state.historyBrowse?.sid === action.sid
        ? null : state.historyBrowse;
      if (runtimes === state.runtimes && sessions === state.sessions
          && historyBrowse === state.historyBrowse) return state;
      return { ...state, runtimes, sessions, historyBrowse };
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
      const next = patch(state, targetSid, (rt) => {
        rt.queue = [...rt.queue, optimistic];
      });
      return targetSid && next.historyBrowse?.sid === targetSid
        ? { ...next, historyBrowse: null }
        : next;
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
      const next = patch(
        state, targetSid, (rt) => { rt.pendingSend = optimistic; });
      return targetSid && next.historyBrowse?.sid === targetSid
        ? { ...next, historyBrowse: null }
        : next;
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
      return patch(state, state.focusedSid, (rt) => { rt.contextReport = action.report; });
    case "clear_context":
      return patch(state, state.focusedSid, (rt) => { rt.contextReport = null; });
    case "begin_context_request":
      return patch(state, action.sid, (rt) => {
        rt.contextRequestId = action.requestId;
        rt.contextError = null;
      });
    case "begin_status_request":
      return patch(state, action.sid, (rt) => {
        rt.statusRequestId = action.requestId;
        rt.statusError = null;
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
    case "clear_btw": {
      const binding = state.btwByParentSid[action.parentSid];
      if (!binding) return state;
      const runtimes = { ...state.runtimes };
      delete runtimes[binding.sid];
      const btwByParentSid = { ...state.btwByParentSid };
      delete btwByParentSid[action.parentSid];
      return { ...state, btwByParentSid, runtimes };
    }
    case "clear_all_btw": {
      const bindings = Object.values(state.btwByParentSid);
      if (bindings.length === 0) return state;
      const runtimes = { ...state.runtimes };
      for (const binding of bindings) delete runtimes[binding.sid];
      return { ...state, btwByParentSid: {}, runtimes };
    }
    case "clear_session_list":
      return {
        ...state, sessions: [], focusedSid: null, historyRecovery: null,
        historyBrowse: null,
      };
    case "restore_session_list":
      // Surface switches are view changes. Paint that surface's last accepted
      // list immediately, then let the in-flight authoritative list replace it.
      // Clearing first exposed Codex app-server startup time as a blank/frozen
      // sidebar even though the browser already had the exact rows in memory.
      return {
        ...state, sessions: action.sessions, focusedSid: null,
        historyRecovery: null, historyBrowse: null,
      };
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
      // empty "send a message" prompt) until cache-hydrate or the wrapper replay lands.
      const runtimes = { ...state.runtimes, [sid]: { ...rt, loading: rt.turns.length === 0 } };
      return {
        ...state, focusedSid: sid, runtimes, artifact: null,
        historyRecovery: state.historyRecovery?.sid === sid
          ? state.historyRecovery : null,
        // Switching away and back always opens the authoritative latest
        // runtime. A delayed page from the previous viewId is then harmless.
        historyBrowse: null,
      };
    }
    case "turn_detail_requested":
      return patch(state, action.sid, (rt) => {
        rt.turns = rt.turns.map((turn) => (
          turn.id === action.turnId || canonicalTurnId(turn) === action.turnId)
          ? {
              ...turn,
              detailLoading: true,
              detailAutoLoad: action.before == null
                ? (action.autoLoad ?? true) : turn.detailAutoLoad,
              detailRestorePending: false,
              detailRestoreIncomplete: action.before == null
                ? action.autoLoad === false
                : turn.detailRestoreIncomplete,
            }
          : turn);
      }, true);
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
      return { ...state, historyBrowse: mutation.projection };
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
      const historyBrowse = markBrowseDetailLoading(
        browse, action.turnId, true, {
          expectedScopeKey: action.scopeKey,
          expectedViewId: action.viewId,
        }, action.before == null ? true : undefined);
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
        const historyBrowse = markBrowseDetailLoading(
          browse, action.turnId, false, {
            expectedScopeKey: action.scopeKey,
            expectedViewId: action.viewId,
          }, false);
        return historyBrowse === browse ? state : { ...state, historyBrowse };
      }
      const installed = installTurnDetailProjectionPage(
        target.detailProjection,
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
          }, false);
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
          }, false);
        return historyBrowse === browse ? state : { ...state, historyBrowse };
      }
      const runtime = state.runtimes[context.sid];
      if (!runtime || runtime.historyRevision !== context.revision) return state;
      return patch(state, context.sid, (rt) => {
        rt.turns = rt.turns.map((turn) => (
          turn.id === context.turnId
            || canonicalTurnId(turn) === context.turnId)
          ? { ...turn, detailLoading: false, detailAutoLoad: false }
          : turn);
      }, true);
    }
    case "return_to_latest":
      return state.historyBrowse?.sid === action.sid
        ? { ...state, historyBrowse: null }
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
        if (rt.turns.length === 0) {
          switchControlGeneration(
            rt, cacheGeneration);
          if (control) applySessionControl(rt, control);
          if (action.turns.length) {
            replaceWithBoundedTurns(rt, action.turns.map((turn) => (
              !turn.forkPointId && turn.codexTurnId
                ? { ...turn, forkPointId: turn.codexTurnId }
                : turn
            )));
            rt.historyRevision = action.revision;
            rt.historyGeneration = cacheGeneration;
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
          rt.turns = restoreCachedTurnDetails(rt.turns, action.turns);
        }
        rt.loading = false;
      }, true);
    case "prune_runtimes": {
      const protectedSids = new Set(action.protectedSids);
      if (state.focusedSid) protectedSids.add(state.focusedSid);
      for (const binding of Object.values(state.btwByParentSid)) {
        protectedSids.add(binding.sid);
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
        newChat: {
          cwd: action.cwd,
          cwdSource: action.cwdSource ?? "default",
          model: action.model ?? null,
          effort: action.effort ?? null,
        },
      };
    case "set_new_chat_cwd":
      return state.newChat ? { ...state, newChat: {
        ...state.newChat,
        cwd: action.cwd,
        cwdSource: action.cwdSource ?? "explicit",
      } } : state;
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
      // switch confirmed by the wrapper → stop the loading spinner. Essential for
      // a RESIDENT session with no replay (e.g. one that only ran /theme and has
      // no history) — otherwise it'd spin until the 6s fallback.
      const base = state.runtimes[newF] ?? createRuntime();
      const runtimes = {
        ...state.runtimes,
        [newF]: {
          ...base,
          loading: base.historyInvalidated ? true : false,
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
        } satisfies SessionInfo, ...state.sessions]
        : state.sessions;
      return {
        ...state, focusedSid: newF, runtimes, sessions,
        historyRecovery: state.historyRecovery?.sid === newF
          ? state.historyRecovery : null,
        historyBrowse: state.focusedSid === newF
          ? state.historyBrowse : null,
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
      if (runtimes[old_key]) {
        const source = runtimes[old_key];
        const target = runtimes[session_id];
        if (target) {
          const seen = new Set(target.turns.map((turn) => turn.id));
          const mergedTurns = [
            ...target.turns,
            ...source.turns.filter((turn) => !seen.has(turn.id)),
          ];
          const mergedControlGeneration =
            source.controlGeneration ?? target.controlGeneration;
          const mergedControl = source.controlGeneration
              && source.controlGeneration !== target.controlGeneration
            ? source.control
            : newestSessionControl(target.control, source.control);
          const mergedRuntime: SessionRuntime = {
            ...target,
            ...source,
            control: null,
            controlGeneration: null,
            hasRevisionedControl:
              target.hasRevisionedControl || source.hasRevisionedControl,
            state: target.state,
            syncReady: target.syncReady || source.syncReady,
            historyInvalidated:
              target.historyInvalidated || source.historyInvalidated,
            historyRevision:
              source.historyRevision ?? target.historyRevision,
            pendingHistoryRevision:
              source.pendingHistoryRevision ?? target.pendingHistoryRevision,
            historyBuildSeq: source.historyRevision == null
              ? target.historyBuildSeq
              : source.historyRevision === target.historyRevision
                ? Math.max(source.historyBuildSeq, target.historyBuildSeq)
                : source.historyBuildSeq,
            historyLiveSeq: source.historyRevision == null
              ? target.historyLiveSeq
              : source.historyRevision === target.historyRevision
                ? Math.max(source.historyLiveSeq, target.historyLiveSeq)
                : source.historyLiveSeq,
            historyGeneration: source.historyRevision == null
              ? target.historyGeneration : source.historyGeneration,
            pendingHistoryGeneration:
              source.pendingHistoryGeneration ?? target.pendingHistoryGeneration,
            pendingHistoryCandidateBuildSeq: source.historyInvalidated
              ? source.pendingHistoryCandidateBuildSeq
              : target.pendingHistoryCandidateBuildSeq,
            historyNewestId: source.historyRevision == null
              ? target.historyNewestId : source.historyNewestId,
            lastLiveSeq: Math.max(source.lastLiveSeq, target.lastLiveSeq),
            lastLifecycleSeq: Math.max(
              source.lastLifecycleSeq, target.lastLifecycleSeq),
            hydratedCacheTurnIds: Array.from(new Set([
              ...target.hydratedCacheTurnIds,
              ...source.hydratedCacheTurnIds,
            ])),
            ccSessionId: session_id,
            turns: mergedTurns,
            queue: [...source.queue, ...target.queue],
            pendingSend: source.pendingSend ?? target.pendingSend,
            failedDeferred: [
              ...source.failedDeferred,
              ...target.failedDeferred.filter((query) => (
                !source.failedDeferred.some(
                  (sourceQuery) => sourceQuery.msg_id === query.msg_id)
              )),
            ],
            acceptancePending:
              source.acceptancePending ?? target.acceptancePending,
            acceptanceHistoryBaseline: source.acceptancePending
              ? source.acceptanceHistoryBaseline
              : target.acceptanceHistoryBaseline,
            notices: mergeNotices(target.notices, source.notices),
          };
          switchControlGeneration(mergedRuntime, mergedControlGeneration);
          if (mergedControl) applySessionControl(mergedRuntime, mergedControl);
          replaceWithBoundedTurns(mergedRuntime, mergedTurns);
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
        if (!targetBtw) btwByParentSid[session_id] = parentBtw;
        else if (targetBtw.sid !== parentBtw.sid) delete runtimes[parentBtw.sid];
        delete btwByParentSid[old_key];
      }
      const historyRecovery = state.historyRecovery?.sid === old_key
        ? { ...state.historyRecovery, sid: session_id }
        : state.historyRecovery;
      // A new session cannot have deep authoritative history yet. Keeping a
      // temp-keyed browse projection through re-key would also leave its page
      // cache under the wrong durable identity.
      const historyBrowse = state.historyBrowse?.sid === old_key
        ? null : state.historyBrowse;
      return {
        ...state,
        runtimes, sessions, historyRecovery, historyBrowse,
        focusedSid: wasFocused ? session_id : state.focusedSid,
        btwByParentSid,
        cwdByScope,
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
      const focusedMissing = !!state.focusedSid
        && !state.focusedSid.startsWith("tmp-")
        && !e.sessions.some((session) => session.session_id === state.focusedSid);
      const focusedSession = ownership && state.focusedSid
        ? e.sessions.find(
          (session) => session.session_id === state.focusedSid)
        : undefined;
      // Another client may migrate the focused session while this tab is
      // offline. The live SessionMigrated frame is then unavailable, so repair
      // the scope default from the authoritative reconnect catalog as well.
      const cwdByScope = ownership && focusedSession?.cwd
          && state.cwdByScope[ownership.scopeKey] !== focusedSession.cwd
        ? { ...state.cwdByScope, [ownership.scopeKey]: focusedSession.cwd }
        : state.cwdByScope;
      return {
        ...state,
        sessions: e.sessions,
        cwdByScope,
        focusedSid: focusedMissing ? null : state.focusedSid,
        historyRecovery: focusedMissing ? null : state.historyRecovery,
        historyBrowse: focusedMissing ? null : state.historyBrowse,
        newChat: focusedMissing
          ? {
            cwd: (ownership
              ? state.cwdByScope[ownership.scopeKey] : "") || "~",
            cwdSource: ownership
              && !!state.cwdByScope[ownership.scopeKey]
              ? "inherited" : "default",
            model: null,
            effort: null,
          }
          : state.newChat,
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
        rt.pendingHistoryRevision = e.revision;
        rt.historyNewestId = null;
        // Keep the accepted generation until replacement arrives: a slow
        // pre-rollback build from that same generation must remain rejectable.
        rt.historyBuildSeq = 0;
        rt.historyLiveSeq = 0;
        rt.hasLoadedOlderHistory = false;
        rt.hydratedCacheTurnIds = [];
        rt.loading = true;
      }, true);
      if (next.historyRecovery?.sid === e.session_id) {
        next = { ...next, historyRecovery: null };
      }
      if (next.historyBrowse?.sid === e.session_id) {
        next = { ...next, historyBrowse: null };
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
      if (!e.before && preControlBase.pendingHistoryGeneration
          && e.generation !== preControlBase.pendingHistoryGeneration) {
        return state;
      }
      if (!e.before && e.authoritative !== false
          && isHistoryRecoveryPending(state.historyRecovery, sid)
          && !historyMatchesRecovery(state.historyRecovery, e)) {
        return state;
      }
      const sameBuildGeneration = e.generation != null
        ? preControlBase.historyGeneration === e.generation
        : preControlBase.historyGeneration == null
          && preControlBase.historyRevision === e.revision;
      const staleHistoryBuild = !e.before && e.build_seq != null
        && sameBuildGeneration
        && e.build_seq < preControlBase.historyBuildSeq;
      const runtimeRecoveryPending =
        isRuntimeHistoryRecoveryPending(preControlBase);
      if (!e.before && e.authoritative !== false && runtimeRecoveryPending) {
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
      if (!e.before && e.authoritative !== false
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
        }));
      }
      if (e.authoritative === false) {
        const provisional = !e.error && !e.before && built.turns.length > 0;
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
      // A pre-rollback first page and an older pagination response can arrive
      // after the replayable marker. Only the marker's exact revision may cross
      // the destructive boundary; pagination is valid only for the revision
      // whose first page is already installed.
      if (!e.before && base.pendingHistoryRevision
          && e.revision !== base.pendingHistoryRevision) return state;
      if (e.before && (base.historyInvalidated
          || !base.historyRevision || e.revision !== base.historyRevision)) {
        return state;
      }
      const pendingAcceptanceTurn = base.acceptancePending
        ? base.turns.find((turn) => turn.id === base.acceptancePending)
        : undefined;
      const acceptedNativeTurnId = pendingAcceptanceTurn
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
      const racedLiveEvent = !e.before && e.live_seq != null
        && base.lastLiveSeq > e.live_seq;
      const preserveStableHeadHistory = !e.before
        && base.turns.length > 0
        && (base.hasLoadedOlderHistory || e.has_more === true)
        && !base.historyInvalidated
        && base.historyRevision === e.revision
        && (e.generation != null
          ? base.historyGeneration === e.generation
          : base.historyGeneration == null);
      let turns: Turn[];
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
          // echo so a History read cannot erase an in-flight send. Likewise,
          // an explicitly running snapshot may precede the transcript flush;
          // only its newest unfinished row can be the active unflushed tail.
          ? unfinished.filter((turn) =>
              turn.id === base.acceptancePending
              || historyContainsTurn(built.turns, turn)
              || (e.in_progress === true && turn === newestUnfinished))
          : unfinished;
        turns = mergeInitialHistory(
          built.turns,
          liveTail, {
          // History's final TurnEnd is synthetic: Claude transcripts do not
          // contain ResultMessage. A newer live event always wins; otherwise an
          // explicit in_progress value is authoritative, and only an older
          // wrapper without that field falls back to the local runtime state.
          preserveLiveTailOpen: racedLiveEvent || e.in_progress === true
            || (e.in_progress == null && base.state !== "idle"),
        });
        // A current first page which explicitly reports idle is the recovery
        // boundary for a lost TurnEnd. Do not close a merely optimistic local
        // query (base is still idle), or a tail advanced after this History read.
        if (e.in_progress === false && !racedLiveEvent && base.state !== "idle") {
          const wasInterrupting = base.state === "interrupting"
            || base.state === "draining";
          turns = finishOpenTurnsFromIdleHistory(
            turns, wasInterrupting,
            e.ts ? Math.round(e.ts * 1000) : Date.now());
        }
      }
      if (e.detail === "summary" && !base.historyInvalidated
          && base.historyRevision === e.revision) {
        const loadedDetail = new Map(base.turns
          .filter((turn) => turn.detailLoaded
            || (turn.detailProjection?.segments.length ?? 0) > 0)
          .map((turn) => [turn.id, turn]));
        turns = turns.map((turn) => {
          const detail = loadedDetail.get(turn.id);
          if (!detail) return turn;
          const merged = mergeAuthoritativeTurnDetail(turn, detail);
          if (turn.done) {
            const status = turn.interrupted
              ? "interrupted" : turn.error ? "failed" : "succeeded";
            finishOpenBlocks(merged, status, status !== "succeeded");
          }
          return merged;
        });
        const cachedScopeMatches = e.generation != null
          ? base.historyGeneration === e.generation
          : base.historyGeneration == null;
        if (cachedScopeMatches && base.hydratedCacheTurnIds.length > 0) {
          const cachedIds = new Set(base.hydratedCacheTurnIds);
          turns = restoreCachedTurnDetails(
            turns,
            base.turns.filter((turn) => cachedIds.has(turn.id)),
          );
        }
      }
      turns = turns.map(withLimitedTurnBlocks);
      const boundedTurns = boundRuntimeTurns(turns);
      const historyTrimmed = boundedTurns.length < turns.length;
      turns = boundedTurns;
      const locallyRetainedCursor = historyTrimmed
        ? (turns[0]?.historyTurnId ?? turns[0]?.id ?? null)
        : null;
      const acceptsControlState = !e.before;
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
      const acceptanceConfirmed = !!base.acceptancePending && (
        !!acceptedNativeTurnId
        || built.turns.some((turn) => turn.id === base.acceptancePending)
        || e.events.some((ev) => (
          (ev.type === "user_msg" || ev.type === "turn_steered"
            || ev.type === "turn_binding"
            || (ev.type === "error" && ev.code !== "wrapper_offline"))
          && ev.msg_id === base.acceptancePending
        ))
      );
      let historyBrowse = state.historyBrowse;
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
              : acceptsControlState && !racedLiveEvent
                  && e.external !== true && !base.external
                  && e.in_progress === false
                ? "idle"
                : base.state,
            mirroredRunning: acceptsControlState && !racedLiveEvent
              ? e.external === true && e.in_progress === true
              : base.mirroredRunning,
            historyInvalidated: acceptsControlState
              ? false : base.historyInvalidated,
            historyRevision: acceptsControlState
              ? e.revision : base.historyRevision,
            pendingHistoryRevision: acceptsControlState
              ? null : base.pendingHistoryRevision,
            historyGeneration: acceptsControlState
              ? (e.generation ?? base.historyGeneration)
              : base.historyGeneration,
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
            historyNewestId: acceptsControlState
              ? (Object.prototype.hasOwnProperty.call(e, "newest_id")
                  ? (e.newest_id ?? null)
                  : base.historyNewestId)
              : base.historyNewestId,
            hasLoadedOlderHistory: e.before
              ? true
              : preserveStableHeadHistory
                ? base.hasLoadedOlderHistory
                : false,
            hydratedCacheTurnIds: acceptsControlState
              ? [] : base.hydratedCacheTurnIds,
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
              : preserveStableHeadHistory ? base.hasMore : e.has_more,
            oldestId: historyTrimmed
              ? locallyRetainedCursor
              : preserveStableHeadHistory
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
            acceptancePending: acceptanceConfirmed
              ? null : base.acceptancePending,
            acceptanceHistoryBaseline: acceptanceConfirmed
              ? null : base.acceptanceHistoryBaseline,
          },
        },
        historyRecovery: advanceHistoryRecovery(state.historyRecovery, e),
        historyBrowse,
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
            ? { ...turn, detailLoading: false }
            : turn);
        });
        return next;
      }
      const target = base.turns.find((turn) => turn.id === e.turn_id
        || canonicalTurnId(turn) === e.turn_id);
      if (!target || e.events.length === 0) {
        return patch(state, sid, (rt) => {
          rt.turns = rt.turns.map((turn) => (
            turn.id === e.turn_id || canonicalTurnId(turn) === e.turn_id)
            ? { ...turn, detailLoading: false, detailAutoLoad: false }
            : turn);
        });
      }
      const installed = installTurnDetailProjectionPage(
        target.detailProjection,
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
            ? { ...turn, detailLoading: false, detailAutoLoad: false }
            : turn);
        });
      }
      return patch(state, sid, (rt) => {
        rt.turns = rt.turns.map((turn) => {
          if (turn.id !== e.turn_id
              && canonicalTurnId(turn) !== e.turn_id) return turn;
          return installAuthoritativeTurnDetailPage(
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
        });
      });
    }
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
      const catalog = e.models.length
        ? { ...state.catalog, [e.engine]: e.models }
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
          catalogDefault[e.engine] = matchModelId(e.default_model, e.engine);
        } else {
          delete catalogDefault[e.engine];
        }
        if (e.default_effort) {
          catalogDefaultEffort[e.engine] = e.default_effort;
        } else {
          delete catalogDefaultEffort[e.engine];
        }
        catalogDefaultCwd = {
          ...catalogDefaultCwd, [e.engine]: e.cwd,
        };
      } else {
        if (e.default_model) {
          catalogDefault = { ...catalogDefault,
            [e.engine]: matchModelId(e.default_model, e.engine) };
        }
        if (e.default_effort) {
          catalogDefaultEffort = {
            ...catalogDefaultEffort, [e.engine]: e.default_effort,
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
      if (!state.artifact || !["md", "file", "html", "image", "pdf"].includes(state.artifact.kind)
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
      return { ...state, artifact: {
        ...state.artifact,
        assets: {
          ...state.artifact.assets,
          [e.path]: {
            mediaType: e.media_type ?? undefined,
            data: e.data ?? undefined,
            error: e.error ?? undefined,
          },
        },
      } };
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
          rt.pendingQuestion = null;
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
      return patch(state, e.sid, (rt) => { rt.model = matchModelId(e.model); });
    case "effort":
      return patch(state, e.sid, (rt) => { rt.effort = e.effort; });
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
      const runtimes = { ...state.runtimes, [e.btw_sid]: state.runtimes[e.btw_sid] ?? createRuntime() };
      const previous = state.btwByParentSid[e.parent_sid];
      if (previous && previous.sid !== e.btw_sid) delete runtimes[previous.sid];
      return {
        ...state,
        btwByParentSid: {
          ...state.btwByParentSid,
          [e.parent_sid]: { sid: e.btw_sid, engine: e.engine },
        },
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
        rt.contextRequestId = null;
        rt.contextError = null;
      });
    case "ask_user":
      return patch(state, e.sid, (rt) => { rt.pendingQuestion = { ask_id: e.ask_id, header: e.header, question: e.question, options: e.options, allow_text: e.allow_text, secret: e.secret, multi_select: e.multi_select }; });
    case "ask_user_closed":
      return patch(state, e.sid, (rt) => {
        if (rt.pendingQuestion?.ask_id === e.ask_id) {
          rt.pendingQuestion = null;
        }
      });
    case "goal_state":
      return patch(state, e.sid, (rt) => { rt.goal = e.goal ?? null; });
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
        rt.statusRequestId = null;
        rt.statusError = null;
      });
    case "notice":
      return patch(state, e.sid, (rt) => {
        rt.notices = mergeNotices(rt.notices, [e]);
      });
    case "rate_limit_update":
      return patch(state, e.sid, (rt) => {
        rt.statusReport = mergeRateLimitUpdate(rt.statusReport, e);
      });
    case "replay_start": {
      const needsAuthoritativeHistory = e.truncated || !!e.rebuild;
      let historyRecovery = state.historyRecovery;
      if (needsAuthoritativeHistory && state.focusedSid === e.sid
          && !state.newChat && e.sid) {
        historyRecovery = beginHistoryRecovery(
          state.historyRecovery,
          e.sid,
          state.runtimes[e.sid] ?? createRuntime(),
          e.generation,
        );
      }
      const next = patch(state, e.sid, (rt) => {
        switchControlGeneration(rt, e.generation);
        rt.replaying = true;
        rt.syncReady = false;
        rt.truncated = e.truncated;
        // rebuild clears turns then refills — keep loading=true so the gap shows a
        // spinner rather than briefly flashing the empty "send a message" prompt.
        if (needsAuthoritativeHistory) {
          rt.turns = [];
          rt.pendingQuestion = null;
          rt.hasMore = false;
          rt.oldestId = null;
          rt.historyInvalidated = true;
          rt.pendingHistoryGeneration = e.generation ?? null;
          rt.pendingHistoryCandidateBuildSeq = null;
          // A replay gap does not reveal which revision was missed. Accept the
          // next authoritative first page; an actual rollback marker replayed
          // inside this envelope will immediately replace this with its token.
          rt.pendingHistoryRevision = null;
          rt.hydratedCacheTurnIds = [];
          rt.hasLoadedOlderHistory = false;
          if (e.rebuild) {
            // The wrapper generation (and every SessionContext seq) restarted.
            // Never compare the new generation against old live/build watermarks.
            rt.historyBuildSeq = 0;
            rt.historyLiveSeq = 0;
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
            rt.contextError = presentCommandProblem(e);
          });
        }
        if (runtime?.statusRequestId === e.request_id) {
          return patch(state, e.sid, (rt) => {
            rt.statusRequestId = null;
            rt.statusError = presentCommandProblem(e);
          });
        }
      }
      if ((e.code === "not_steerable"
          || e.code === "steer_outcome_unknown") && e.msg_id) {
        // A rejected steer is a control failure, not the terminal state of the
        // still-running native turn. Never fabricate an empty failed turn that
        // would steal subsequent deltas from the active segment.
        return { ...state, banner: presentCommandProblem(e) };
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
          rt.acceptancePending = null;
          rt.acceptanceHistoryBaseline = null;
        }
        markTurnAsLive(rt, e.msg_id!, boundCompletedTurns, e.seq);
        const turns = cloneTurns(rt.turns);
        const t = turns.find((turn) => turn.id === e.msg_id);
        if (t) {
          t.error = presentTurnProblem(e);
          t.progress = undefined;
          t.done = true;
          t.doneTs ??= Date.now();
          finishOpenBlocks(t, "failed", true);
        }
        else turns.push({ id: e.msg_id!, prompt: "", blocks: [], done: true,
          error: presentTurnProblem(e), doneTs: Date.now() });
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
        rt.queue = rt.queue.filter((query) => query.msg_id !== e.msg_id);
        if (rt.pendingSend?.msg_id === e.msg_id) rt.pendingSend = null;
        rt.failedDeferred = rt.failedDeferred.filter(
          (query) => query.msg_id !== e.msg_id);
        if (rt.acceptancePending === e.msg_id) {
          rt.acceptancePending = null;
          rt.acceptanceHistoryBaseline = null;
        }
        markTurnAsLive(rt, e.msg_id, boundCompletedTurns, e.seq);
        const turns = cloneTurns(rt.turns);
        const existing = turns.find((t) => t.id === e.msg_id);
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
        } else {
          turns.push({ id: e.msg_id, prompt: e.prompt, images: imgs,
            files: fileMeta, blocks: [], done: false, ts: stamp });
        }
        rt.turns = turns;
      });
      const sessions = e.sid
        ? bumpSessionActivity(next.sessions, e.sid, Math.round(e.ts * 1000))
        : next.sessions;
      return sessions === next.sessions ? next : { ...next, sessions };
    }
    case "turn_steered": {
      const next = patch(state, e.sid, (rt) => {
        if (rt.acceptancePending === e.msg_id) {
          rt.acceptancePending = null;
          rt.acceptanceHistoryBaseline = null;
        }
        markTurnAsLive(rt, e.msg_id, boundCompletedTurns, e.seq);
        const turns = cloneTurns(rt.turns);
        const imgs = (e.images && e.images.length) ? e.images : undefined;
        const fileMeta = (e.files && e.files.length)
          ? e.files.map((file) => ({ filename: file.filename, data: "" }))
          : undefined;
        const stamp = eventTimestampMs(e.ts);
        const existing = turns.find((turn) =>
          turn.id === e.msg_id || turn.clientMsgId === e.msg_id);
        if (existing) {
          // Reliable-command replay can deliver the correlated narrative frame
          // again after reconnect. Refresh metadata without closing another
          // segment or duplicating the user bubble.
          existing.prompt ||= e.prompt;
          existing.images ??= imgs;
          if (fileMeta) existing.files = fileMeta;
          existing.ts ??= stamp;
          existing.clientMsgId ??= e.msg_id;
          existing.liveTaskId ??= e.turn_id;
          rt.turns = turns;
          return;
        }

        const previous = [...turns].reverse().find((turn) => !turn.done);
        if (previous) {
          previous.done = true;
          previous.durationMs = 0;
          previous.doneTs = stamp ?? Date.now();
          previous.progress = undefined;
          // Before steer, TurnBinding temporarily attached the native task to
          // the first visible segment. The task's real fork boundary belongs to
          // the final segment after all steered input has run.
          if (previous.forkPointId === e.turn_id) {
            previous.forkPointId = undefined;
          }
          previous.liveTaskId = undefined;
          finishOpenBlocks(previous, "succeeded", false);
        }
        turns.push({
          id: e.msg_id,
          clientMsgId: e.msg_id,
          liveTaskId: e.turn_id,
          prompt: e.prompt,
          images: imgs,
          files: fileMeta,
          blocks: [],
          done: false,
          ts: stamp,
        });
        if (boundCompletedTurns) replaceWithBoundedTurns(rt, turns);
        else rt.turns = turns;
      }, true);
      const sessions = e.sid
        ? bumpSessionActivity(next.sessions, e.sid, Math.round(e.ts * 1000))
        : next.sessions;
      return sessions === next.sessions ? next : { ...next, sessions };
    }
    case "assistant_msg_start":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        const t = openTurn(turns, e.message_id, eventTimestampMs(e.ts));
        markTurnAsLive(rt, t.id, boundCompletedTurns, e.seq);
        t.progress = undefined;
        const block = t.blocks.find((b) => b.kind === "text"
          && b.message_id === e.message_id) as TextBlock | undefined;
        if (block) block.channel = resolvedChannel(block.channel, e.channel ?? "unknown");
        else {
          t.blocks.push({ kind: "text", message_id: e.message_id, text: "",
            done: false, channel: e.channel ?? "unknown" });
          if (boundCompletedTurns) limitTurnBlocks(t);
        }
        rt.turns = turns;
      });
    case "delta":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        const t = openTurn(turns, e.message_id, eventTimestampMs(e.ts));
        markTurnAsLive(rt, t.id, boundCompletedTurns, e.seq);
        t.progress = undefined;
        let block = t.blocks.find((b) => b.kind === "text" && b.message_id === e.message_id) as TextBlock | undefined;
        if (!block) {
          block = { kind: "text", message_id: e.message_id, text: "", done: false,
            channel: e.channel ?? "unknown" };
          t.blocks.push(block);
          if (boundCompletedTurns) limitTurnBlocks(t);
        }
        block.channel = resolvedChannel(block.channel, e.channel ?? "unknown");
        block.text = appendField(block.text, e.text, MAX_LIVE_TEXT_CHARS);
        if (boundCompletedTurns) limitTurnBlocks(t);
        rt.turns = turns;
      });
    case "tool_use":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        const t = openTurn(turns, e.message_id, eventTimestampMs(e.ts));
        markTurnAsLive(rt, t.id, boundCompletedTurns, e.seq);
        t.progress = undefined;
        const existing = t.blocks.find((b) => b.kind === "tool"
          && b.tool_use_id === e.tool_use_id) as ToolBlock | undefined;
        if (existing) {
          existing.tool = e.tool;
          existing.input = e.input;
          existing.category = e.category ?? "tool";
          existing.title = e.title;
          existing.parent_id = e.parent_id;
          existing.server = e.server;
        } else {
          t.blocks.push({ kind: "tool", message_id: e.message_id,
            tool_use_id: e.tool_use_id, tool: e.tool, input: e.input,
            category: e.category ?? "tool", title: e.title, parent_id: e.parent_id,
            server: e.server, done: false });
          if (boundCompletedTurns) limitTurnBlocks(t);
        }
        rt.turns = turns;
      });
    case "tool_delta":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        for (const t of turns) {
          const block = t.blocks.find((b) => b.kind === "tool"
            && b.tool_use_id === e.tool_use_id) as ToolBlock | undefined;
          if (!block) continue;
          markTurnAsLive(rt, t.id, boundCompletedTurns, e.seq);
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
        for (const t of turns) {
          const b = t.blocks.find((b) => b.kind === "tool" && b.tool_use_id === e.tool_use_id) as ToolBlock | undefined;
          if (b) {
            markTurnAsLive(rt, t.id, boundCompletedTurns, e.seq);
            b.result = { content: e.content, is_error: e.is_error,
              truncated: e.truncated ?? undefined, status: e.status,
              summary: e.summary, diff: e.diff, exit_code: e.exit_code,
              duration_ms: e.duration_ms };
            if (e.diff) b.diff = e.diff;
            b.done = true;
            t.progress = undefined;
            if (boundCompletedTurns) limitTurnBlocks(t);
            break;
          }
        }
        rt.turns = turns;
      });
    case "assistant_msg_end":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        for (const t of turns) {
          const b = t.blocks.find((b) => b.kind === "text" && b.message_id === e.message_id) as TextBlock | undefined;
          if (b) {
            markTurnAsLive(rt, t.id, boundCompletedTurns, e.seq);
            b.channel = resolvedChannel(b.channel, e.channel ?? "unknown");
            b.done = true;
            break;
          }
        }
        rt.turns = turns;
      });
    case "process":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        let owner: Turn | undefined;
        let block: ProcessBlock | undefined;
        for (const candidate of turns) {
          const found = candidate.blocks.find((b) => b.kind === "process"
            && b.item_id === e.item_id) as ProcessBlock | undefined;
          if (found) { owner = candidate; block = found; break; }
        }
        // Background task/hook events may arrive after their originating turn
        // ended and after a newer query opened. Prefer their explicit parent or
        // engine turn id before falling back to the current tail; otherwise a
        // delayed subagent update creates a phantom new turn or attaches to the
        // wrong conversation.
        if (!owner) owner = findTurnOwningItem(turns, e.parent_id);
        if (!owner) owner = findTurnByEngineId(turns, e.turn_id);
        if (!owner) {
          owner = openTurn(
            turns, e.turn_id || e.item_id, eventTimestampMs(e.ts));
        }
        markTurnAsLive(rt, owner.id, boundCompletedTurns, e.seq);
        if (!block) {
          block = { kind: "process", item_id: e.item_id, processKind: e.kind,
            phase: e.phase, status: e.status, turn_id: e.turn_id,
            parent_id: e.parent_id, title: e.title, done: false };
          owner.blocks.push(block);
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
        block.done = e.phase === "end" || terminalProcessStatus(e.status);
        owner.progress = undefined;
        if (boundCompletedTurns) limitTurnBlocks(owner);
        rt.turns = turns;
      });
    case "turn_plan":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        let t = findTurnOwningItem(turns, e.item_id)
          ?? findTurnByEngineId(turns, e.turn_id);
        if (!t) {
          t = openTurn(
            turns, e.turn_id || e.item_id, eventTimestampMs(e.ts));
        }
        markTurnAsLive(rt, t.id, boundCompletedTurns, e.seq);
        let block = t.blocks.find((b) => b.kind === "process"
          && b.item_id === e.item_id) as ProcessBlock | undefined;
        if (!block) {
          block = { kind: "process", item_id: e.item_id, processKind: "plan",
            phase: "snapshot", status: "running", turn_id: e.turn_id,
            title: "计划", done: false };
          t.blocks.push(block);
        }
        block.explanation = e.explanation;
        block.plan = e.plan.map((entry) => ({ ...entry }));
        block.status = e.plan.length > 0 && e.plan.every((entry) => entry.status === "completed")
          ? "succeeded" : "running";
        block.done = block.status === "succeeded";
        t.progress = undefined;
        if (boundCompletedTurns) limitTurnBlocks(t);
        rt.turns = turns;
      });
    case "turn_diff":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        let t = findTurnOwningItem(turns, e.item_id)
          ?? findTurnByEngineId(turns, e.turn_id);
        if (!t) {
          t = openTurn(
            turns, e.turn_id || e.item_id, eventTimestampMs(e.ts));
        }
        markTurnAsLive(rt, t.id, boundCompletedTurns, e.seq);
        let block = t.blocks.find((b) => b.kind === "process"
          && b.item_id === e.item_id) as ProcessBlock | undefined;
        if (!block) {
          block = { kind: "process", item_id: e.item_id, processKind: "diff",
            phase: "snapshot", status: "running", turn_id: e.turn_id,
            title: "代码改动", done: false };
          t.blocks.push(block);
        }
        block.diff = e.diff;
        block.truncated = e.truncated;
        t.progress = undefined;
        if (boundCompletedTurns) limitTurnBlocks(t);
        rt.turns = turns;
      });
    case "turn_binding":
      return patch(state, e.sid, (rt) => {
        if (rt.acceptancePending === e.msg_id) {
          rt.acceptancePending = null;
          rt.acceptanceHistoryBaseline = null;
        }
        const turns = cloneTurns(rt.turns);
        const optimisticIndex = turns.findIndex((turn) => turn.id === e.msg_id);
        if (optimisticIndex < 0) return;
        turns[optimisticIndex].forkPointId = e.turn_id;
        const authoritativeIndex = turns.findIndex((turn, index) => (
          index !== optimisticIndex
          && (turn.forkPointId === e.turn_id || turn.id === e.turn_id)
        ));
        if (authoritativeIndex >= 0) {
          const optimistic = turns[optimisticIndex];
          const authoritative = turns[authoritativeIndex];
          const merged = mergeInitialHistory(
            [authoritative], [optimistic])[0] ?? optimistic;
          merged.forkPointId = e.turn_id;
          const first = Math.min(optimisticIndex, authoritativeIndex);
          const second = Math.max(optimisticIndex, authoritativeIndex);
          turns.splice(second, 1);
          turns.splice(first, 1, merged);
        }
        if (boundCompletedTurns) replaceWithBoundedTurns(rt, turns);
        else rt.turns = turns;
      });
    case "turn_end":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        let t = findTurnByEngineId(turns, e.turn_id);
        if (!t) {
          const openTurns = turns.filter((turn) => !turn.done);
          // Claude and older producers may reveal the native id only at the
          // terminal boundary. Preserve that legacy path when there is exactly
          // one unclosed owner, while never closing an unrelated completed row.
          if (openTurns.length === 1) t = openTurns[0];
        }
        if (t) {
          markTurnAsLive(rt, t.id, boundCompletedTurns, e.seq);
          if (rt.acceptancePending === t.id) {
            rt.acceptancePending = null;
            rt.acceptanceHistoryBaseline = null;
          }
          t.done = true;
          t.durationMs = e.result.duration_ms;
          if (e.turn_id) {
            t.forkPointId = e.turn_id;
            t.liveTaskId = undefined;
          }
          if (e.checkpoint_id) t.checkpointId = e.checkpoint_id;
          t.progress = undefined;
          if (e.result.subtype === "error_during_execution") t.interrupted = true;
          // Stamp completion time from the event's own server ts (seconds -> ms).
          // Robust for BOTH live turns and replayed history: the old
          // `t.ts + duration_ms` reconstruction dropped the timestamp for any turn
          // without a client-side start time (i.e. everything after a refresh,
          // where turns come from history replay). Fall back to start time, then now.
          t.doneTs = e.ts ? Math.round(e.ts * 1000) : (t.ts || Date.now());
          finishOpenBlocks(t, e.result.is_error ? "interrupted" : "succeeded",
            e.result.is_error);
        }
        if (boundCompletedTurns) replaceWithBoundedTurns(rt, turns);
        else rt.turns = turns;
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
    case "hello":
      return state;
  }
}
