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
import type { ConnState } from "./ws";
import type {
  ServerEvent, SessionInfo, State, ContextReport, StatusReport, ThreadGoal,
  QueryImg, QueryFile, DirEntry, AssistantChannel, ToolCategory, ProcessKind,
  ProcessStatus, PlanEntry, CollaborationModeName, Notice, RateLimitUpdate,
  StatusRateLimit, StatusRateWindow,
} from "./protocol";
import type { Catalog } from "./data";
import type { DiffLine, GitDiffSection } from "./diff";
import { parseGitDiff } from "./diff";
import { matchModelId } from "./data";
import { canEnqueueQuery, collectWaitingQueries, reduceTargetedRuntime } from "./runtime-drain";
import { mergeInitialHistory } from "./history-merge";
import { boundRuntimeTurns, pruneRuntimeMap } from "./runtime-bounds";

export interface TextBlock {
  kind: "text";
  message_id: string;
  text: string;
  done: boolean;
  channel?: AssistantChannel;
}
export interface ToolBlock {
  kind: "tool";
  message_id: string;
  tool_use_id: string;
  tool: string;
  input: Record<string, unknown>;
  category?: ToolCategory;
  title?: string | null;
  parent_id?: string | null;
  server?: string | null;
  progress?: string;
  output?: string;
  diff?: string;
  result?: {
    content: string;
    is_error: boolean;
    truncated?: boolean | null;
    status?: ProcessStatus | null;
    summary?: string | null;
    diff?: string | null;
    exit_code?: number | null;
    duration_ms?: number | null;
  };
  done: boolean;
}
export interface ProcessBlock {
  kind: "process";
  item_id: string;
  processKind: ProcessKind;
  phase: "start" | "update" | "end" | "snapshot";
  status: ProcessStatus;
  turn_id?: string | null;
  parent_id?: string | null;
  title: string;
  summary?: string | null;
  detail?: string | null;
  input?: Record<string, unknown> | null;
  output?: string | null;
  diff?: string | null;
  progress?: string | null;
  server?: string | null;
  tool?: string | null;
  command?: string | null;
  cwd?: string | null;
  exit_code?: number | null;
  duration_ms?: number | null;
  truncated?: boolean | null;
  explanation?: string | null;
  plan?: PlanEntry[];
  done: boolean;
}
export type Block = TextBlock | ToolBlock | ProcessBlock;

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

export interface Turn {
  id: string;
  // Engine-specific authoritative branch point: a Codex app-server turn id or
  // a Claude transcript assistant UUID. The wire keeps the legacy `turn_id`
  // name so already-deployed protocol-v5 peers remain compatible.
  forkPointId?: string;
  /** @deprecated Read only while migrating CACHE_VER=5 entries. */
  codexTurnId?: string;
  prompt: string; // empty when we joined mid-turn (no user bubble rendered)
  blocks: Block[];
  done: boolean;
  interrupted?: boolean;
  error?: string;
  progress?: string;
  images?: QueryImg[];
  files?: QueryFile[];
  ts?: number;
  doneTs?: number;
  durationMs?: number;
}

export interface PendingQuery {
  prompt: string;
  images?: QueryImg[];
  files?: QueryFile[];
}

export interface PreviewAssetState {
  mediaType?: string;
  data?: string;
  error?: string;
}

export interface Artifact {
  file: string;
  kind: "diff" | "md" | "file" | "gitdiff";
  sid?: string | null;
  requestId?: string;
  diff?: DiffLine[];
  content?: string;
  sections?: GitDiffSection[];
  loading?: boolean;
  size?: number;
  truncated?: boolean;
  mtimeNs?: number;
  line?: number;
  error?: string;
  assets?: Record<string, PreviewAssetState>;
}

export interface SessionRuntime {
  turns: Turn[];
  state: State;
  model: string;
  effort: string;
  perm: string;
  collaborationMode: CollaborationModeName;
  fast: boolean | null;   // null until the wrapper reports the real service tier
  replaying: boolean;
  // True only after this connection has received this sid's Snapshot or
  // ReplayEnd. Prevents stale local "idle" state from draining work early.
  syncReady: boolean;
  truncated: boolean;
  // true while we've switched to a session but its history hasn't arrived yet
  // (no cache hit + waiting on the wrapper's cold spawn/replay) — drives a spinner.
  loading?: boolean;
  // pagination: older turns exist beyond what's loaded, and the oldest loaded
  // turn id — the cursor the "load more" button pages back from.
  hasMore?: boolean;
  oldestId?: string | null;
  // true => an external process (native `claude`/`codex` in the terminal) owns this
  // session and is writing its transcript; we mirror it read-only.
  external?: boolean;
  takeoverPending: boolean;
  takeoverMessage: string | null;
  ccSessionId?: string;
  pendingQuestion: { ask_id: string; header?: string | null; question: string; options: { label: string; ds?: string }[]; allow_text?: boolean; secret?: boolean } | null;
  contextReport: ContextReport | null;
  goal: ThreadGoal | null;
  statusReport: StatusReport | null;
  notices: Notice[];
  queue: PendingQuery[];
  pendingSend: PendingQuery | null;
}

export interface AppState {
  // connection / global UI
  connState: ConnState;
  wrapperOnline: boolean;
  banner?: string;
  artifact: Artifact | null;
  dirPicker: { path: string; parent: string | null; dirs: DirEntry[] } | null;
  currentCwd: string;
  sendMode: "interrupt" | "queue";
  // new-chat welcome page (global; only one new-chat flow at a time). model/effort
  // are the pre-selected values (null = use the wrapper's engine default).
  newChat: { cwd: string; model: string | null; effort: string | null } | null;
  // sessions + multi-session runtimes
  sessions: SessionInfo[];
  focusedSid: string | null;
  runtimes: Record<string, SessionRuntime>;
  // /btw ephemeral side-fork: the fork's routing key (its runtime lives in
  // `runtimes[btwSid]`) + engine, or null when no side panel is open.
  btwSid: string | null;
  btwEngine?: string;
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
    turns: [], state: "idle", model: "", effort: "", perm: "",
    collaborationMode: "default",
    fast: null,
    takeoverPending: false, takeoverMessage: null,
    replaying: false, syncReady: false, truncated: false,
    pendingQuestion: null, contextReport: null, goal: null, statusReport: null,
    notices: [],
    queue: [], pendingSend: null,
  };
}

export type Action =
  | { type: "reset" }
  | { type: "event"; event: ServerEvent }
  | { type: "query_sent"; sid: string; prompt: string; msg_id: string; images?: QueryImg[]; files?: QueryFile[]; ts: number }
  | { type: "conn"; connState: ConnState; detail?: string }
  | { type: "command_error"; detail: string }
  | { type: "enqueue"; query: PendingQuery }
  | { type: "dequeue_at"; sid: string; i: number }
  | { type: "set_send_mode"; mode: "interrupt" | "queue" }
  | { type: "set_pending"; query: PendingQuery }
  | { type: "clear_pending"; sid: string }
  | { type: "set_model"; model: string }
  | { type: "set_effort"; effort: string }
  | { type: "set_perm"; perm: string }
  | { type: "set_collaboration_mode"; mode: CollaborationModeName }
  | { type: "set_context"; report: ContextReport }
  | { type: "clear_context" }
  | { type: "set_turns"; sid: string; turns: Turn[] }
  | { type: "set_artifact"; artifact: Artifact }
  | { type: "open_artifact_loading"; file: string; sid: string | null }
  | { type: "open_file_loading"; file: string; sid: string | null; requestId: string; kind: "md" | "file"; line?: number }
  | { type: "clear_artifact" }
  | { type: "clear_btw" }
  | { type: "focus_session"; sid: string }
  | { type: "hydrate_cache"; sid: string; turns: Turn[] }
  | { type: "prune_runtimes"; protectedSids: string[] }
  | { type: "answer_question" }
  | { type: "dismiss_notice"; sid: string; noticeId: string }
  | { type: "enter_new_chat"; cwd: string; model?: string | null; effort?: string | null }
  | { type: "set_new_chat_cwd"; cwd: string }
  | { type: "set_new_chat_model"; model: string | null }
  | { type: "set_new_chat_effort"; effort: string | null }
  | { type: "exit_new_chat" };

export const initialState: AppState = {
  connState: "connecting",
  // Require a wrapper-originated frame before draining queued work. A relay
  // socket can be connected while the machine-side wrapper is still offline.
  wrapperOnline: false,
  artifact: null,
  dirPicker: null,
  currentCwd: "",
  sendMode: "interrupt",
  newChat: null,
  sessions: [],
  focusedSid: null,
  runtimes: {},
  btwSid: null,
  catalog: {},
  catalogDefault: {},
  catalogDefaultEffort: {},
  catalogDefaultCwd: {},
};

function cloneTurns(turns: Turn[]): Turn[] {
  return turns.map((t) => ({ ...t, blocks: t.blocks.map((b) => ({ ...b })) }));
}

function openTurn(turns: Turn[], fallbackId: string): Turn {
  let turn = turns[turns.length - 1];
  if (!turn || turn.done) {
    turn = { id: fallbackId, prompt: "", blocks: [], done: false };
    turns.push(turn);
  }
  return turn;
}

function findTurnByEngineId(turns: Turn[], id: string | null | undefined): Turn | undefined {
  if (!id) return undefined;
  return [...turns].reverse().find((turn) =>
    turn.id === id || turn.forkPointId === id || turn.codexTurnId === id);
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
    runtime.hasMore = false;
    runtime.oldestId = bounded[0]?.id ?? null;
  }
  runtime.turns = bounded;
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

function mergeNotices(...groups: Notice[][]): Notice[] {
  const merged: Notice[] = [];
  for (const notice of groups.flat()) {
    const prior = merged.findIndex((item) => item.notice_id === notice.notice_id);
    if (prior >= 0) merged.splice(prior, 1);
    merged.push(notice);
  }
  return merged.slice(-MAX_SESSION_NOTICES);
}

function mergeRateWindow(
  current: StatusRateWindow | null | undefined,
  update: StatusRateWindow | null | undefined,
): StatusRateWindow | null | undefined {
  if (!update) return current;
  const next = { ...(current ?? {}) };
  if (update.used_percent != null) next.used_percent = update.used_percent;
  if (update.resets_at != null) next.resets_at = update.resets_at;
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
        newChat: null, btwSid: null, catalog: {}, catalogDefault: {},
        catalogDefaultEffort: {}, catalogDefaultCwd: {},
      };
    case "conn": {
      let banner = state.banner;
      if (action.connState === "connected") banner = undefined;
      else if (action.connState === "reconnecting") banner = action.detail || "reconnecting…";
      else if (action.connState === "connecting") banner = "connecting…";
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
        banner,
      };
    }
    case "command_error":
      return { ...state, banner: action.detail };
    case "query_sent": {
      const turn: Turn = {
        id: action.msg_id, prompt: action.prompt, blocks: [], done: false,
        images: action.images,
        files: action.files?.map((file) => ({ filename: file.filename, data: "" })),
        ts: action.ts,
      };
      const runtimes = reduceTargetedRuntime(
        state.runtimes, action.sid, { type: "query_sent", turn });
      return runtimes === state.runtimes ? state : { ...state, runtimes };
    }
    case "enqueue": {
      const allQueued = collectWaitingQueries(state.runtimes);
      if (!canEnqueueQuery(allQueued, action.query)) return state;
      return patch(state, state.focusedSid, (rt) => {
        rt.queue = [...rt.queue, action.query];
      });
    }
    case "dequeue_at": {
      const runtimes = reduceTargetedRuntime(
        state.runtimes, action.sid, { type: "dequeue_at", i: action.i });
      return runtimes === state.runtimes ? state : { ...state, runtimes };
    }
    case "set_send_mode":
      return { ...state, sendMode: action.mode };
    case "set_pending": {
      const waiting = collectWaitingQueries(state.runtimes, state.focusedSid);
      if (!canEnqueueQuery(waiting, action.query)) return state;
      return patch(state, state.focusedSid, (rt) => { rt.pendingSend = action.query; });
    }
    case "clear_pending": {
      const runtimes = reduceTargetedRuntime(
        state.runtimes, action.sid, { type: "clear_pending" });
      return runtimes === state.runtimes ? state : { ...state, runtimes };
    }
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
    case "set_artifact":
      return { ...state, artifact: action.artifact };
    case "open_artifact_loading":
      // optimistic: show the diff panel (with a spinner) instantly on click; the
      // diff_report event replaces it with the real sections when it arrives.
      return { ...state, artifact: { file: action.file, sid: action.sid, kind: "gitdiff", sections: [], loading: true } };
    case "open_file_loading":
      return { ...state, artifact: {
        file: action.file, sid: action.sid, requestId: action.requestId,
        kind: action.kind, line: action.line, content: "", assets: {}, loading: true,
      } };
    case "clear_artifact":
      return { ...state, artifact: null };
    case "clear_btw": {
      if (!state.btwSid) return state;
      const runtimes = { ...state.runtimes };
      delete runtimes[state.btwSid];   // ephemeral: drop the fork's runtime
      return { ...state, btwSid: null, btwEngine: undefined, runtimes };
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
      return { ...state, focusedSid: sid, runtimes, artifact: null };
    }
    case "hydrate_cache":
      // fill a session's turns from the IndexedDB cache for an INSTANT render;
      // only if still empty (never clobber live/streaming or already-replayed turns).
      return patch(state, action.sid, (rt) => {
        if (rt.turns.length === 0 && action.turns.length) {
          replaceWithBoundedTurns(rt, action.turns.map((turn) => (
            !turn.forkPointId && turn.codexTurnId
              ? { ...turn, forkPointId: turn.codexTurnId }
              : turn
          )));
        }
        rt.loading = false;
      }, true);
    case "prune_runtimes": {
      const protectedSids = new Set(action.protectedSids);
      if (state.focusedSid) protectedSids.add(state.focusedSid);
      if (state.btwSid) protectedSids.add(state.btwSid);
      if (state.artifact?.sid) protectedSids.add(state.artifact.sid);
      const runtimes = pruneRuntimeMap(state.runtimes, protectedSids);
      return runtimes === state.runtimes ? state : { ...state, runtimes };
    }
    case "answer_question":
      return patch(state, state.focusedSid, (rt) => { rt.pendingQuestion = null; });
    case "dismiss_notice":
      return patch(state, action.sid, (rt) => {
        rt.notices = rt.notices.filter(
          (notice) => notice.notice_id !== action.noticeId);
      });
    case "enter_new_chat":
      return { ...state, newChat: { cwd: action.cwd, model: action.model ?? null, effort: action.effort ?? null } };
    case "set_new_chat_cwd":
      return state.newChat ? { ...state, newChat: { ...state.newChat, cwd: action.cwd } } : state;
    case "set_new_chat_model":
      return state.newChat ? { ...state, newChat: { ...state.newChat, model: action.model } } : state;
    case "set_new_chat_effort":
      return state.newChat ? { ...state, newChat: { ...state.newChat, effort: action.effort } } : state;
    case "exit_new_chat":
      return { ...state, newChat: null };
    case "event":
      return reduceEvent(state, action.event);
  }
}

function reduceEvent(
  state: AppState, e: ServerEvent, boundCompletedTurns = true,
): AppState {
  switch (e.type) {
    case "snapshot": {
      // Per-session: the frame's sid is the runtime key; cc_session_id is the
      // real cc id (may still be null while a brand-new session's id is captured).
      const key = e.sid ?? e.cc_session_id ?? state.focusedSid;
      if (!key) return state;
      // First snapshot on a fresh connect focuses that session so the UI shows
      // something; a later session_focus (or the user picking one) overrides it.
      const focusedSid = state.focusedSid ?? key;
      return { ...patch(state, key, (rt) => {
        rt.state = e.state;
        rt.syncReady = true;
        rt.ccSessionId = e.cc_session_id ?? rt.ccSessionId;
      }, true), focusedSid, wrapperOnline: true };
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
        [newF]: { ...base, loading: false, syncReady: true },
      };
      return {
        ...state, focusedSid: newF, runtimes,
        artifact: state.focusedSid && state.focusedSid !== newF ? null : state.artifact,
        currentCwd: e.cwd ?? state.currentCwd,
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
          const mergedRuntime: SessionRuntime = {
            ...target,
            ...source,
            state: target.state,
            syncReady: target.syncReady || source.syncReady,
            ccSessionId: session_id,
            turns: mergedTurns,
            queue: [...source.queue, ...target.queue],
            pendingSend: source.pendingSend ?? target.pendingSend,
            notices: mergeNotices(target.notices, source.notices),
          };
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
      return {
        ...state,
        runtimes,
        focusedSid: wasFocused ? session_id : state.focusedSid,
        currentCwd: wasFocused && e.cwd ? e.cwd : state.currentCwd,
      };
    }
    case "session_list":
      return { ...state, sessions: e.sessions };
    case "history": {
      // Bulk on-demand history (one frame, read from the transcript — like a web
      // chat's GET /conversation). Rebuild this session's COMPLETED turns by
      // running the events through a throwaway empty runtime: this reuses the
      // per-event reduce logic verbatim so deltas accumulate EXACTLY ONCE (never
      // double-appending over cache-hydrated or live text). Any not-yet-done turn
      // already in the runtime (an in-flight turn still streaming live, not yet in
      // the transcript) is preserved and appended after the rebuilt history.
      const sid = e.session_id;
      let scratch: AppState = {
        ...state, banner: undefined, runtimes: { [sid]: createRuntime() },
      };
      for (const ev of e.events) {
        scratch = reduceEvent(scratch, ev as ServerEvent, false);
      }
      const built = scratch.runtimes[sid] ?? createRuntime();
      const base = state.runtimes[sid] ?? createRuntime();
      let turns: Turn[];
      if (e.before) {
        // pagination (load older): PREPEND the older turns ahead of what we have,
        // deduped by id — keeps the current view and in-flight turn intact.
        const haveIds = new Set(base.turns.map((t) => t.id));
        turns = [...built.turns.filter((t) => !haveIds.has(t.id)), ...base.turns];
      } else {
        // Initial load has no atomic transcript/live boundary. Merge instead of
        // replacing: preserve just-finished turns not flushed to disk, correlate
        // optimistic client ids by prompt/time, and combine an in-flight tail.
        turns = mergeInitialHistory(built.turns, base.turns, {
          // History's final TurnEnd is synthetic: Claude transcripts do not
          // contain ResultMessage.  A focus switch can read that EOF while the
          // resident turn is still running, so never let it close the live tail.
          preserveLiveTailOpen: !!e.in_progress || base.state !== "idle",
        });
      }
      turns = turns.map(withLimitedTurnBlocks);
      const boundedTurns = boundRuntimeTurns(turns);
      const historyTrimmed = boundedTurns.length < turns.length;
      turns = boundedTurns;
      const acceptsControlState = !e.before;
      const hadModel = e.events.some((ev) => (ev as { type?: string }).type === "model");
      const hadEffort = e.events.some((ev) => (ev as { type?: string }).type === "effort");
      return {
        ...state,
        banner: scratch.banner ?? state.banner,
        runtimes: {
          ...state.runtimes,
          [sid]: {
            ...base, turns, loading: false,
            model: acceptsControlState && hadModel ? built.model : base.model,
            effort: acceptsControlState && hadEffort ? built.effort : base.effort,
            hasMore: historyTrimmed ? false : e.has_more,
            oldestId: historyTrimmed
              ? (turns[0]?.id ?? null)
              : (e.oldest_id ?? base.oldestId),
            truncated: base.truncated || historyTrimmed,
            // A native `claude`/`codex` in the terminal owns this session and is
            // appending to its transcript; the wrapper mirrors those appends here.
            // Render read-only — a cc session has ONE owner, and typing would fork it.
            external: !!e.external,
            takeoverPending: !!e.takeover_pending,
            takeoverMessage: e.takeover_pending ? base.takeoverMessage : null,
          },
        },
      };
    }
    case "dir_list":
      return { ...state, dirPicker: { path: e.path, parent: e.parent ?? null, dirs: e.dirs } };
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
          || state.artifact.sid !== (e.sid ?? state.focusedSid)) return state;
      return { ...state, artifact: {
        file: e.file, sid: state.artifact.sid, kind: "gitdiff", sections: parseGitDiff(e.diff),
      } };
    case "file_preview":
      if (!state.artifact || !["md", "file"].includes(state.artifact.kind)
          || state.artifact.requestId !== e.request_id
          || state.artifact.sid !== (e.sid ?? state.focusedSid)) return state;
      return { ...state, artifact: {
        file: e.path,
        sid: state.artifact.sid,
        requestId: e.request_id,
        kind: e.format === "markdown" ? "md" : "file",
        content: e.content,
        size: e.size,
        truncated: e.truncated,
        mtimeNs: e.mtime_ns,
        line: state.artifact.line,
        error: e.error ?? undefined,
        assets: {},
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
    case "state":
      return patch(state, e.sid, (rt) => {
        rt.state = e.state;
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
    case "takeover_state":
      return patch(state, e.sid, (rt) => {
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
      // open the side panel + ensure a runtime for the fork; do NOT change focus
      // (the main view stays put — the fork lives only in the panel).
      const runtimes = { ...state.runtimes, [e.btw_sid]: state.runtimes[e.btw_sid] ?? createRuntime() };
      return { ...state, btwSid: e.btw_sid, btwEngine: e.engine, runtimes };
    }
    case "perm":
      return patch(state, e.sid, (rt) => { rt.perm = e.mode; });
    case "context_report":
      return patch(state, e.sid, (rt) => { rt.contextReport = e; });
    case "ask_user":
      return patch(state, e.sid, (rt) => { rt.pendingQuestion = { ask_id: e.ask_id, header: e.header, question: e.question, options: e.options, allow_text: e.allow_text, secret: e.secret }; });
    case "goal_state":
      return patch(state, e.sid, (rt) => { rt.goal = e.goal ?? null; });
    case "status_report":
      return patch(state, e.sid, (rt) => { rt.statusReport = e; });
    case "notice":
      return patch(state, e.sid, (rt) => {
        rt.notices = mergeNotices(rt.notices, [e]);
      });
    case "rate_limit_update":
      return patch(state, e.sid, (rt) => {
        rt.statusReport = mergeRateLimitUpdate(rt.statusReport, e);
      });
    case "replay_start":
      return { ...patch(state, e.sid, (rt) => {
        rt.replaying = true;
        rt.syncReady = false;
        rt.truncated = e.truncated;
        // rebuild clears turns then refills — keep loading=true so the gap shows a
        // spinner rather than briefly flashing the empty "send a message" prompt.
        if (e.truncated || !!e.rebuild) { rt.turns = []; rt.loading = true; }
        if (e.rebuild) rt.pendingQuestion = null;
      }, true) };
    case "replay_end":
      return { ...patch(state, e.sid, (rt) => {
        rt.replaying = false;
        rt.syncReady = true;
        rt.truncated = rt.truncated || e.truncated;
        rt.loading = false;
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
          banner: "machine offline — waiting for reconnect",
        };
      }
      if (!e.msg_id) {
        return { ...state, banner: `${e.code}: ${e.message}` };
      }
      return patch(state, e.sid, (rt) => {
        rt.loading = false; // never leave a spinner spinning behind an error
        const turns = cloneTurns(rt.turns);
        const t = turns.find((turn) => turn.id === e.msg_id);
        if (t) {
          t.error = `${e.code}: ${e.message}`;
          t.progress = undefined;
          t.done = true;
          t.doneTs ??= Date.now();
          finishOpenBlocks(t, "failed", true);
        }
        else turns.push({ id: e.msg_id!, prompt: "", blocks: [], done: true,
          error: `${e.code}: ${e.message}`, doneTs: Date.now() });
        if (boundCompletedTurns) replaceWithBoundedTurns(rt, turns);
        else rt.turns = turns;
        rt.pendingQuestion = null;
      }, true);
    }
    case "user_msg":
      return patch(state, e.sid, (rt) => {
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
    case "assistant_msg_start":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        const t = openTurn(turns, e.message_id);
        t.progress = undefined;
        const block = t.blocks.find((b) => b.kind === "text"
          && b.message_id === e.message_id) as TextBlock | undefined;
        if (block) block.channel = resolvedChannel(block.channel, e.channel ?? "unknown");
        else {
          t.blocks.push({ kind: "text", message_id: e.message_id, text: "",
            done: false, channel: e.channel ?? "unknown" });
          limitTurnBlocks(t);
        }
        rt.turns = turns;
      });
    case "delta":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        const t = openTurn(turns, e.message_id);
        t.progress = undefined;
        let block = t.blocks.find((b) => b.kind === "text" && b.message_id === e.message_id) as TextBlock | undefined;
        if (!block) {
          block = { kind: "text", message_id: e.message_id, text: "", done: false,
            channel: e.channel ?? "unknown" };
          t.blocks.push(block);
          limitTurnBlocks(t);
        }
        block.channel = resolvedChannel(block.channel, e.channel ?? "unknown");
        block.text = appendField(block.text, e.text, MAX_LIVE_TEXT_CHARS);
        limitTurnBlocks(t);
        rt.turns = turns;
      });
    case "tool_use":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        const t = openTurn(turns, e.message_id);
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
          limitTurnBlocks(t);
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
          limitTurnBlocks(t);
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
            b.result = { content: e.content, is_error: e.is_error,
              truncated: e.truncated ?? undefined, status: e.status,
              summary: e.summary, diff: e.diff, exit_code: e.exit_code,
              duration_ms: e.duration_ms };
            if (e.diff) b.diff = e.diff;
            b.done = true;
            t.progress = undefined;
            limitTurnBlocks(t);
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
        if (!owner) owner = openTurn(turns, e.turn_id || e.item_id);
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
        limitTurnBlocks(owner);
        rt.turns = turns;
      });
    case "turn_plan":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        let t = findTurnOwningItem(turns, e.item_id)
          ?? findTurnByEngineId(turns, e.turn_id);
        if (!t) t = openTurn(turns, e.turn_id || e.item_id);
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
        limitTurnBlocks(t);
        rt.turns = turns;
      });
    case "turn_diff":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        let t = findTurnOwningItem(turns, e.item_id)
          ?? findTurnByEngineId(turns, e.turn_id);
        if (!t) t = openTurn(turns, e.turn_id || e.item_id);
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
        limitTurnBlocks(t);
        rt.turns = turns;
      });
    case "turn_end":
      return patch(state, e.sid, (rt) => {
        const turns = cloneTurns(rt.turns);
        const t = turns[turns.length - 1];
        if (t) {
          t.done = true;
          t.durationMs = e.result.duration_ms;
          if (e.turn_id) t.forkPointId = e.turn_id;
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
        rt.state = "idle";
        rt.pendingQuestion = null;
      });
    case "pong":
    case "command_ack":
    case "session_forked":
    case "hello":
      return state;
  }
}
