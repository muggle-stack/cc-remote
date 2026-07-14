// Mirror of cc_remote/protocol.py. Kept in sync manually (generate later).

export type State = "idle" | "running" | "interrupting" | "draining";
export type Engine = "claude" | "codex";
export type AssistantChannel = "unknown" | "thinking" | "commentary" | "final";
export type ToolCategory = "tool" | "command" | "file" | "mcp" | "agent" | "server_tool" | "web_search";
export type ProcessKind = "reasoning" | "plan" | "command" | "file_change" | "mcp" | "agent" | "hook" | "server_tool" | "web_search" | "task" | "terminal" | "model" | "safety" | "diff" | "compaction";
export type ProcessPhase = "start" | "update" | "end" | "snapshot";
export type ProcessStatus = "pending" | "running" | "succeeded" | "failed" | "declined" | "cancelled" | "interrupted" | "unknown";
export type ProcessAppendTarget = "summary" | "detail" | "output" | "diff" | "progress";
export type CodexThreadStatus = "notLoaded" | "idle" | "systemError" | "active";
export type EffortLevel = "none" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max" | "ultra";
export type PermissionMode = "default" | "acceptEdits" | "plan" | "auto" | "bypassPermissions" | "never" | "on-request" | "untrusted";
export type CollaborationModeName = "default" | "plan";
export type ServiceTier = "" | "default" | "fast" | "toggle";
export type DiffTheme = "light" | "dark";
export type CodexPermissionMode = "never" | "on-request" | "untrusted";
export type CodexServiceTier = "default" | "fast";
export type NoticeSeverity = "info" | "warning";
export type NoticeCategory = "runtime" | "guardian" | "config" | "deprecation" | "security" | "rate_limit";

interface Base {
  v: number;
  type: string;
  ts: number;
  sid?: string | null;
  seq?: number | null;
  to?: string | null;
  route_id?: string | null;
  cmd_id?: string | null;
  client_id?: string | null;
}

export interface Hello extends Base {
  type: "hello";
  role: "client" | "wrapper";
  client_id?: string | null;
  last_seq?: number | null;
  cursors?: Record<string, number> | null;
  generations?: Record<string, string> | null;
  cc_session_id?: string | null;
  state?: State | null;
  buffer_head_seq?: number | null;
  buffer_tail_seq?: number | null;
  wrapper_generation?: string | null;
}
export interface QueryImg { media_type: "image/png" | "image/jpeg" | "image/jpg" | "image/webp"; data: string }
export interface QueryFile { filename: string; data: string }
export interface Query extends Base { type: "query"; prompt: string; msg_id: string; images?: QueryImg[] | null; files?: QueryFile[] | null }
export interface Interrupt extends Base { type: "interrupt" }
export interface Takeover extends Base { type: "takeover"; sid: string; cmd_id: string }
export interface TakeoverState extends Base { type: "takeover_state"; pending: boolean; message?: string | null }
export interface SetModel extends Base { type: "set_model"; model: string }
export interface SetEffort extends Base { type: "set_effort"; effort: EffortLevel }
export interface SetServiceTier extends Base { type: "set_service_tier"; service_tier: ServiceTier }
export interface SetCollaborationMode extends Base { type: "set_collaboration_mode"; mode: CollaborationModeName }
export interface Ping extends Base { type: "ping"; n: number }
export interface Pong extends Base { type: "pong"; n: number }
export interface CommandAck extends Base { type: "command_ack"; cmd_id: string; client_id: string }
export interface ReplayStart extends Base { type: "replay_start"; from_seq: number; to_seq: number; truncated: boolean; rebuild?: boolean; generation?: string | null }
export interface ReplayEnd extends Base { type: "replay_end"; to_seq: number; truncated: boolean }
export interface Snapshot extends Base { type: "snapshot"; cc_session_id?: string | null; state: State; tail_text: string; cwd?: string | null; generation?: string | null }
export interface StateEvent extends Base {
  type: "state";
  state: State;
  phase?: "retrying" | "waiting" | null;
  detail?: string | null;
  msg_id?: string | null;
}
export interface Model extends Base { type: "model"; model: string }
export interface Effort extends Base { type: "effort"; effort: string }
export interface Fast extends Base { type: "fast"; on: boolean }
export interface CollaborationMode extends Base { type: "collaboration_mode"; mode: CollaborationModeName }
export interface OpenBtw extends Base { type: "open_btw"; request_id: string; client_id?: string }
export interface CloseBtw extends Base { type: "close_btw" }
export interface BtwOpened extends Base { type: "btw_opened"; request_id: string; btw_sid: string; parent_sid: string; engine: string }
export interface ForkSession extends Base {
  type: "fork_session";
  session_id: string;
  request_id: string;
  last_turn_id: string;
}
export interface ForkSessionWorktree extends Base {
  type: "fork_session_worktree";
  session_id: string;
  name?: string | null;
  request_id: string;
  last_turn_id?: string | null;
}
export interface SessionForked extends Base {
  type: "session_forked";
  parent_session_id: string;
  session_id: string;
  cwd: string;
  git_branch?: string | null;
  request_id: string;
  last_turn_id?: string | null;
  target: "same_cwd" | "worktree";
}
export interface UserMsg extends Base { type: "user_msg"; msg_id: string; prompt: string; images?: QueryImg[] | null; files?: { filename: string }[] | null }
export interface AssistantMsgStart extends Base { type: "assistant_msg_start"; message_id: string; channel?: AssistantChannel }
export interface Delta extends Base { type: "delta"; message_id: string; text: string; channel?: AssistantChannel }
export interface ToolUse extends Base {
  type: "tool_use";
  message_id: string;
  tool_use_id: string;
  tool: string;
  input: Record<string, unknown>;
  category?: ToolCategory;
  title?: string | null;
  parent_id?: string | null;
  server?: string | null;
}
export interface ToolDelta extends Base {
  type: "tool_delta";
  tool_use_id: string;
  stream: "progress" | "output" | "diff" | "summary" | "terminal";
  delta: string;
}
export interface ToolResult extends Base {
  type: "tool_result";
  tool_use_id: string;
  content: string;
  is_error: boolean;
  truncated?: boolean | null;
  status?: ProcessStatus | null;
  summary?: string | null;
  diff?: string | null;
  exit_code?: number | null;
  duration_ms?: number | null;
}
export interface AssistantMsgEnd extends Base { type: "assistant_msg_end"; message_id: string; channel?: AssistantChannel }
export interface ProcessEvent extends Base {
  type: "process";
  item_id: string;
  kind: ProcessKind;
  phase: ProcessPhase;
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
  append_to?: ProcessAppendTarget | null;
  delta?: string | null;
  server?: string | null;
  tool?: string | null;
  command?: string | null;
  cwd?: string | null;
  exit_code?: number | null;
  duration_ms?: number | null;
  truncated?: boolean | null;
}
export interface PlanEntry { step: string; status: "pending" | "inProgress" | "completed" }
export interface TurnPlan extends Base { type: "turn_plan"; item_id: string; turn_id?: string | null; explanation?: string | null; plan: PlanEntry[] }
export interface TurnDiff extends Base { type: "turn_diff"; item_id: string; turn_id?: string | null; diff: string; truncated?: boolean | null }
export interface TurnResult { subtype: string; duration_ms: number; is_error: boolean; total_cost_usd?: number | null; num_turns?: number | null }
export interface TurnEnd extends Base { type: "turn_end"; result: TurnResult; turn_id?: string | null }
export interface ErrorMsg extends Base {
  type: "error";
  code: string;
  message: string;
  request_id?: string | null;
  msg_id?: string | null;
}
export interface WrapperDisconnected extends Base { type: "wrapper_disconnected" }
export interface WrapperReconnected extends Base { type: "wrapper_reconnected"; cc_session_id?: string | null; state: State; generation?: string | null }

// sessions
export interface SessionInfo {
  session_id: string;
  summary?: string | null;
  last_modified?: string | null;
  first_prompt?: string | null;
  git_branch?: string | null;
  cwd?: string | null;
  tag?: string | null;
  state?: State | null;
  engine?: Engine | null;
  forked_from_id?: string | null;
  codex_status?: CodexThreadStatus | null;
}
export interface ListSessions extends Base { type: "list_sessions"; engine?: Engine }
export interface SwitchSession extends Base { type: "switch_session"; session_id: string; engine?: Engine }
export interface NewSession extends Base {
  type: "new_session";
  request_id?: string | null;
  cwd?: string | null;
  engine?: "claude" | "codex";
  model?: string | null;
  effort?: string | null;
  collaboration_mode?: CollaborationModeName | null;
  permission_mode?: CodexPermissionMode | null;
  service_tier?: CodexServiceTier | null;
  prompt?: string | null;
  msg_id?: string | null;
  images?: QueryImg[] | null;
  files?: QueryFile[] | null;
}
export interface SessionList extends Base { type: "session_list"; engine: Engine; sessions: SessionInfo[] }
export interface SessionFocus extends Base { type: "session_focus"; session_id: string; cwd?: string | null; request_id?: string | null }
// NON-focusing re-key: a temp-keyed new session captured its real cc id. Rename
// the runtime old_key -> session_id + migrate the cursor; focus only follows if
// we were already viewing old_key. Prevents focus-steal by background sessions.
export interface SessionRekey extends Base { type: "session_rekey"; old_key: string; session_id: string; cwd?: string | null }
export interface RenameSession extends Base { type: "rename_session"; session_id: string; title: string }
export interface ArchiveSession extends Base { type: "archive_session"; session_id: string; archived: boolean }
export interface DirEntry { name: string; path: string }
export interface ListDir extends Base { type: "list_dir"; path?: string | null }
export interface DirList extends Base { type: "dir_list"; path: string; parent?: string | null; dirs: DirEntry[] }
export interface SetPerm extends Base { type: "set_perm"; mode: PermissionMode }
export interface Perm extends Base { type: "perm"; mode: string }
export interface GetContext extends Base { type: "get_context" }
export interface GetDiff extends Base { type: "get_diff"; file: string; theme?: DiffTheme }
export interface DiffReport extends Base { type: "diff_report"; file: string; diff: string }
// On-demand bulk history: fetched once when a session is opened (like a web
// chat's GET /conversation) instead of replaying the ring buffer on every hello.
export interface GetHistory extends Base { type: "get_history"; session_id: string; client_id?: string | null; cwd?: string | null; before?: string | null; limit?: number | null }
// `external`: this session's transcript is being appended to by a native `claude`/
// `codex` in the user's terminal. The wrapper mirrors those appends by broadcasting
// a fresh History; we render the session read-only (a cc session has one owner).
export interface History extends Base { type: "history"; session_id: string; events: ServerEvent[]; has_more: boolean; oldest_id?: string | null; newest_id?: string | null; before?: string | null; external?: boolean; takeover_pending?: boolean; in_progress?: boolean }
// The engine's own model catalog. codex's app-server reports, per model, exactly
// which reasoning levels it accepts — and `turn/start` does NOT validate the level
// (it accepts `bogus-zzz`), so one we invent client-side only fails later inside the
// model API. The server is authoritative; data.ts's table is a fallback.
export interface GetModels extends Base { type: "get_models"; engine?: string | null; client_id?: string | null; cwd?: string | null }
export interface CatalogModel {
  id: string;
  display_name: string;
  description: string;
  efforts: string[];
  default_effort?: string | null;
  is_default?: boolean;
}
// Effective controls for a NEW no-override session. These are display metadata,
// not the focused session's controls and not implicit overrides on NewSession.
export interface Models extends Base { type: "models"; engine: string; models: CatalogModel[]; default_model?: string | null; default_effort?: string | null; cwd?: string | null }
export interface AskOption { label: string; ds?: string }
export interface AskUser extends Base { type: "ask_user"; ask_id: string; header?: string | null; question: string; options: AskOption[]; allow_text?: boolean; secret?: boolean }
export interface AnswerQuestion extends Base { type: "answer_question"; ask_id: string; answer: string }
export type GoalStatus = "active" | "paused" | "blocked" | "usageLimited" | "budgetLimited" | "complete";
export interface GetGoal extends Base { type: "get_goal" }
export interface SetGoal extends Base {
  type: "set_goal";
  objective?: string | null;
  status?: GoalStatus | null;
  token_budget?: number | null;
}
export interface ClearGoal extends Base { type: "clear_goal" }
export interface GetStatus extends Base { type: "get_status" }
export interface ThreadGoal {
  threadId: string;
  objective: string;
  status: GoalStatus;
  engine: Engine;
  tokenBudget?: number | null;
  tokensUsed: number;
  timeUsedSeconds: number;
  createdAt?: number;
  updatedAt?: number;
  // Claude Code's native /goal lifecycle fields (active_goal events).
  iterations?: number;
  lastReason?: string | null;
  setAt?: number;
  tokensAtStart?: number;
}
export interface GoalState extends Base { type: "goal_state"; goal?: ThreadGoal | null }
export interface StatusThread {
  thread_id: string;
  session_id?: string | null;
  cwd?: string | null;
  source?: string | null;
  cli_version?: string | null;
  status: string;
  active_flags: string[];
  ephemeral?: boolean | null;
  created_at?: number | null;
  updated_at?: number | null;
}
export interface StatusRuntime {
  app_server_version?: string | null;
  model?: string | null;
  model_provider?: string | null;
  reasoning_effort?: string | null;
  service_tier?: string | null;
  approval_policy?: string | null;
  sandbox_mode?: string | null;
  web_search?: string | null;
}
export interface StatusContext { used_tokens?: number | null; max_tokens?: number | null; percentage?: number | null }
export interface StatusAccount { auth_type: string; plan_type?: string | null; requires_openai_auth: boolean }
export interface StatusRateWindow { used_percent?: number | null; resets_at?: number | null; window_duration_mins?: number | null }
export interface StatusRateLimit { limit_id?: string | null; limit_name?: string | null; plan_type?: string | null; rate_limit_reached_type?: string | null; primary?: StatusRateWindow | null; secondary?: StatusRateWindow | null }
export interface StatusUsage { lifetime_tokens?: number | null; current_streak_days?: number | null; longest_streak_days?: number | null; peak_daily_tokens?: number | null; longest_running_turn_sec?: number | null }
export interface StatusReport extends Base {
  type: "status_report";
  thread: StatusThread;
  runtime: StatusRuntime;
  context: StatusContext;
  account?: StatusAccount | null;
  rate_limits: StatusRateLimit[];
  usage?: StatusUsage | null;
  component_errors: string[];
}
export interface Notice extends Base {
  type: "notice";
  notice_id: string;
  severity: NoticeSeverity;
  category: NoticeCategory;
  title: string;
  message: string;
  detail?: string | null;
  thread_id?: string | null;
}
export interface RateLimitUpdate extends Base {
  type: "rate_limit_update";
  limit_id?: string | null;
  name?: string | null;
  plan_type?: string | null;
  reached_type?: string | null;
  primary?: StatusRateWindow | null;
  secondary?: StatusRateWindow | null;
}
export interface ContextCategory { name: string; tokens: number; color: string; isDeferred?: boolean }
export interface ContextReport extends Base {
  type: "context_report";
  total_tokens: number;
  max_tokens: number;
  percentage: number;
  model?: string | null;
  is_auto_compact_enabled?: boolean | null;
  categories: ContextCategory[];
}

export type ServerEvent =
  | Pong | CommandAck | ReplayStart | ReplayEnd | Snapshot | StateEvent | Model | Effort | Fast | CollaborationMode | BtwOpened | Perm | ContextReport | DiffReport | History | Models | TakeoverState
  | AskUser | GoalState | StatusReport | Notice | RateLimitUpdate
  | SessionList | SessionFocus | SessionRekey | SessionForked
  | DirList
  | UserMsg | AssistantMsgStart | Delta | ToolUse | ToolDelta | ToolResult | AssistantMsgEnd
  | ProcessEvent | TurnPlan | TurnDiff
  | TurnEnd | ErrorMsg | WrapperDisconnected | WrapperReconnected | Hello;

export const PROTOCOL_VERSION = 8;

/** Local storage is user-controlled and may contain stale values from older
 * builds. Normalize before a value reaches a strict Pydantic command frame. */
export function normalizeEngine(value: string | null): Engine {
  return value === "codex" ? "codex" : "claude";
}

export function normalizeDiffTheme(value: string | null): DiffTheme {
  return value === "dark" ? "dark" : "light";
}

/** Build the correlated message-level fork command for either engine.
 * `last_turn_id` is the protocol-v5 legacy wire name; for Claude its value is
 * the selected reply's transcript UUID rather than a Codex turn id. */
export function makeForkSessionCommand(
  sessionId: string, forkPointId: string, requestId: string, ts: number,
): ForkSession {
  return {
    v: PROTOCOL_VERSION,
    type: "fork_session",
    session_id: sessionId,
    request_id: requestId,
    last_turn_id: forkPointId,
    ts,
  };
}

/** Build the correlated command used to open one ephemeral /btw fork. */
export function makeOpenBtwCommand(
  parentSid: string, requestId: string, ts: number,
): OpenBtw {
  return {
    v: PROTOCOL_VERSION,
    type: "open_btw",
    sid: parentSid,
    request_id: requestId,
    ts,
  };
}

/** Build the correlated command used to persistently fork a Codex session into
 * a new Git worktree. The wrapper owns worktree creation and name validation. */
export function makeForkSessionWorktreeCommand(
  sessionId: string, name: string, requestId: string, ts: number,
  lastTurnId?: string,
): ForkSessionWorktree {
  const command: ForkSessionWorktree = {
    v: PROTOCOL_VERSION,
    type: "fork_session_worktree",
    session_id: sessionId,
    name,
    request_id: requestId,
    ts,
  };
  if (lastTurnId) command.last_turn_id = lastTurnId;
  return command;
}

/** Exact guard for accepting a one-shot /btw response in the UI. */
export function matchesBtwRequest(
  pendingRequestId: string | null, responseRequestId: string | null | undefined,
): boolean {
  return pendingRequestId !== null && responseRequestId === pendingRequestId;
}

export type BtwOpenedDisposition = "accept" | "duplicate" | "stale";

/** Classify success replies so an ACK-loss replay cannot close the active fork. */
export function classifyBtwOpened(
  pendingRequestId: string | null,
  active: { requestId: string; sid: string } | null,
  response: Pick<BtwOpened, "request_id" | "btw_sid">,
): BtwOpenedDisposition {
  if (active?.requestId === response.request_id && active.sid === response.btw_sid) {
    return "duplicate";
  }
  return matchesBtwRequest(pendingRequestId, response.request_id)
    ? "accept" : "stale";
}

/** A successful OpenBtw reply is followed by a Snapshot.  When the success is
 * stale we close the fork and remember its sid so that trailing Snapshot cannot
 * recreate an unreferenced runtime in the reducer. */
export function consumeDiscardedBtwSnapshot(
  discardedSids: Set<string>,
  snapshot: Pick<Snapshot, "sid">,
): boolean {
  const sid = snapshot.sid;
  if (!sid || !discardedSids.has(sid)) return false;
  discardedSids.delete(sid);
  return true;
}
