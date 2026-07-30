// Mirror of cc_remote/protocol.py. Kept in sync manually (generate later).

export type State = "idle" | "running" | "interrupting" | "draining";
export type Engine = "claude" | "codex";
export type Space = "code" | "work";
export type RestoreMode = "conversation" | "files" | "both";
export type RestoreOutcome = "succeeded" | "failed" | "skipped";
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
export type ControlMode = "remote" | "codex_shared" | "claude_broker" | "external_cli" | "agent_view" | "desktop";
export type WriteState = "writable" | "read_only" | "takeover_pending" | "input_busy";
export type ServiceTier = "" | "default" | "fast" | "toggle";
export type DiffTheme = "light" | "dark";
export type CodexPermissionMode = "never" | "on-request" | "untrusted";
export type CodexPermissionProfileId = string;
export type CodexServiceTier = "default" | "fast";
export type CodexWebSearchMode = "cached" | "live";
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
  machine_id?: string | null;
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
export interface Query extends Base { type: "query"; prompt: string; msg_id: string; images?: QueryImg[] | null; files?: QueryFile[] | null; delivery?: "immediate" | "queue" | "replace" }
export interface CancelQueuedQuery extends Base { type: "cancel_queued_query"; sid: string; msg_id: string; cmd_id: string; client_id: string }
export interface GetQueuedQuery extends Base { type: "get_queued_query"; sid: string; msg_id: string; cmd_id: string; client_id: string }
export interface QueuedQueryDetail extends Base {
  type: "queued_query_detail";
  sid: string;
  msg_id: string;
  request_id: string;
  prompt?: string | null;
  kind?: "queue" | "replace" | null;
  image_count: number;
  file_count: number;
  error?: string | null;
}
export interface UpdateQueuedQuery extends Base { type: "update_queued_query"; sid: string; msg_id: string; prompt: string; cmd_id: string; client_id: string }
export interface QueuedQueryUpdated extends Base { type: "queued_query_updated"; sid: string; msg_id: string; request_id: string; updated: boolean; error?: string | null }
export interface QueuedQueryInfo {
  msg_id: string;
  kind: "queue" | "replace";
  prompt_preview: string;
  image_count: number;
  file_count: number;
  retained_bytes: number;
  error?: string | null;
}
export interface QueryQueueState extends Base {
  type: "query_queue";
  items: QueuedQueryInfo[];
  total_count: number;
  total_bytes: number;
}
export interface Steer extends Base { type: "steer"; sid: string; cmd_id: string; client_id: string; prompt: string; msg_id: string; images?: QueryImg[] | null; files?: QueryFile[] | null }
export interface Interrupt extends Base { type: "interrupt" }
export interface Takeover extends Base { type: "takeover"; sid: string; cmd_id: string }
export interface TakeoverState extends Base { type: "takeover_state"; pending: boolean; message?: string | null }
export interface SessionControl extends Base {
  type: "session_control";
  control_mode: ControlMode;
  write_state: WriteState;
  terminal_attached: boolean;
  reason?: string | null;
  generation?: string | null;
  revision: number;
  /** Display capability only; it does not mean migration is pre-authorized. */
  can_takeover?: boolean | null;
}
export interface SetModel extends Base { type: "set_model"; model: string }
export interface SetEffort extends Base { type: "set_effort"; effort: EffortLevel }
export interface SetServiceTier extends Base { type: "set_service_tier"; service_tier: ServiceTier }
export interface SetCollaborationMode extends Base { type: "set_collaboration_mode"; mode: CollaborationModeName }
export interface Ping extends Base { type: "ping"; n: number }
export interface Pong extends Base { type: "pong"; n: number }
export interface CommandAck extends Base { type: "command_ack"; cmd_id: string; client_id: string }
export interface ReplayStart extends Base { type: "replay_start"; from_seq: number; to_seq: number; truncated: boolean; rebuild?: boolean; generation?: string | null }
export interface ReplayEnd extends Base { type: "replay_end"; to_seq: number; truncated: boolean }
export interface Snapshot extends Base { type: "snapshot"; cc_session_id?: string | null; state: State; tail_text: string; cwd?: string | null; generation?: string | null; control?: SessionControl | null }
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
export interface MigrateSession extends Base {
  type: "migrate_session";
  session_id: string;
  cwd: string;
  request_id: string;
}
export interface SessionMigrated extends Base {
  type: "session_migrated";
  session_id: string;
  previous_cwd: string;
  cwd: string;
  request_id: string;
}
export interface UserMsg extends Base { type: "user_msg"; msg_id: string; client_msg_id?: string | null; prompt: string; images?: QueryImg[] | null; files?: { filename: string }[] | null }
export interface TurnSteered extends Base { type: "turn_steered"; msg_id: string; turn_id: string; prompt: string; images?: QueryImg[] | null; files?: { filename: string }[] | null }
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
export interface TurnBinding extends Base { type: "turn_binding"; msg_id: string; turn_id: string }
export interface TurnResult { subtype: string; duration_ms: number; is_error: boolean; total_cost_usd?: number | null; num_turns?: number | null }
export interface TurnNotificationContext { engine: Engine; space: Space; display_name?: string | null; parent_session_id?: string | null }
export interface TurnEnd extends Base { type: "turn_end"; result: TurnResult; turn_id?: string | null; checkpoint_id?: string | null; notification_context?: TurnNotificationContext | null }
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
  pinned?: boolean;
  state?: State | null;
  engine?: Engine | null;
  forked_from_id?: string | null;
  codex_status?: CodexThreadStatus | null;
  space?: Space | null;
  work_id?: string | null;
}
export interface ListSessions extends Base { type: "list_sessions"; engine?: Engine; space?: Space }
export interface SwitchSession extends Base { type: "switch_session"; session_id: string; engine?: Engine; space?: Space }
export interface NewSession extends Base {
  type: "new_session";
  request_id?: string | null;
  cwd?: string | null;
  engine?: "claude" | "codex";
  space?: Space;
  project_id?: string | null;
  model?: string | null;
  effort?: string | null;
  collaboration_mode?: CollaborationModeName | null;
  permission_mode?: CodexPermissionMode | null;
  permission_profile?: CodexPermissionProfileId | null;
  web_search?: CodexWebSearchMode | null;
  service_tier?: CodexServiceTier | null;
  prompt?: string | null;
  msg_id?: string | null;
  images?: QueryImg[] | null;
  files?: QueryFile[] | null;
}
export interface SessionList extends Base { type: "session_list"; engine: Engine; space?: Space; sessions: SessionInfo[] }
export interface SessionActivity extends Base { type: "session_activity"; engine: Engine; session_id: string; state: State }
export interface SessionFocus extends Base { type: "session_focus"; session_id: string; cwd?: string | null; request_id?: string | null }
// NON-focusing re-key: a temp-keyed new session captured its real cc id. Rename
// the runtime old_key -> session_id + migrate the cursor; focus only follows if
// we were already viewing old_key. Prevents focus-steal by background sessions.
export interface SessionRekey extends Base { type: "session_rekey"; old_key: string; session_id: string; cwd?: string | null }
export interface RenameSession extends Base { type: "rename_session"; session_id: string; title: string; engine?: Engine; space?: Space }
export interface ArchiveSession extends Base { type: "archive_session"; session_id: string; archived: boolean; engine?: Engine; space?: Space }
export interface PinSession extends Base { type: "pin_session"; session_id: string; pinned: boolean; engine?: Engine; space?: Space }
export interface DeleteWorkSession extends Base { type: "delete_work_session"; session_id: string; engine: Engine; space?: "work" }
export interface DeleteSession extends Base { type: "delete_session"; session_id: string; engine: Engine; space?: Space }
export interface RollbackSession extends Base {
  type: "rollback_session";
  session_id: string;
  engine: Engine;
  space?: "code";
  restore?: RestoreMode;
  num_turns?: number;
  checkpoint_id?: string | null;
}
export interface RollbackResult extends Base {
  type: "rollback_result";
  session_id: string;
  engine: Engine;
  restore: RestoreMode;
  conversation: RestoreOutcome;
  files: RestoreOutcome;
  restored_turns: number;
  conflicts: string[];
  prefill_text?: string | null;
  detail?: string | null;
}
export interface CompactSession extends Base { type: "compact_session"; session_id: string; engine?: "codex"; space?: "code" }
export interface StartReview extends Base { type: "start_review"; session_id: string; engine?: "codex"; space?: "code"; target: "uncommittedChanges" | "baseBranch" | "commit" | "custom"; value?: string | null }
export interface GetWorkDashboard extends Base { type: "get_work_dashboard"; engine?: Engine }
export interface CreateWorkProject extends Base { type: "create_work_project"; engine?: Engine; name: string; description?: string }
export interface DeleteWorkProject extends Base { type: "delete_work_project"; engine?: Engine; project_id: string }
export interface AddWorkSource extends Base { type: "add_work_source"; engine?: Engine; project_id: string; kind: "file" | "link" | "note"; title: string; uri?: string | null; file?: QueryFile | null }
export interface DeleteWorkSource extends Base { type: "delete_work_source"; engine?: Engine; source_id: string }
export interface CreateWorkPlugin extends Base { type: "create_work_plugin"; engine?: Engine; project_id?: string | null; name: string; instructions: string }
export interface DeleteWorkPlugin extends Base { type: "delete_work_plugin"; engine?: Engine; plugin_id: string }
export interface CreateWorkSchedule extends Base { type: "create_work_schedule"; engine?: Engine; project_id?: string | null; title: string; prompt: string; next_run_at: number; repeat_seconds?: number | null }
export interface DeleteWorkSchedule extends Base { type: "delete_work_schedule"; engine?: Engine; schedule_id: string }
export interface GetWorkArtifacts extends Base { type: "get_work_artifacts"; engine?: Engine; session_id: string }
export interface WorkArtifactInfo { path: string; size: number; modified_at: number; kind: "document" | "spreadsheet" | "presentation" | "image" | "pdf" | "file"; previewable: boolean }
export interface WorkArtifacts extends Base { type: "work_artifacts"; engine: Engine; session_id: string; artifacts: WorkArtifactInfo[] }
export interface WorkProjectInfo { project_id: string; name: string; description: string; created_at: number; updated_at: number }
export interface WorkSourceInfo { source_id: string; project_id: string; kind: "file" | "link" | "note"; title: string; uri?: string | null; created_at: number }
export interface WorkPluginInfo { plugin_id: string; project_id?: string | null; name: string; instructions: string; enabled: boolean; created_at: number; updated_at: number }
export interface WorkScheduleInfo { schedule_id: string; project_id?: string | null; title: string; prompt: string; next_run_at: number; repeat_seconds?: number | null; enabled: boolean; last_run_at?: number | null; last_session_id?: string | null; last_error?: string | null; last_run_id?: string | null; last_run_status?: "queued" | "claimed" | "running" | "succeeded" | "failed" | null; last_run_attempt?: number | null; created_at: number; updated_at: number }
export interface WorkDashboard extends Base { type: "work_dashboard"; engine: Engine; projects: WorkProjectInfo[]; sources: WorkSourceInfo[]; plugins: WorkPluginInfo[]; schedules: WorkScheduleInfo[] }
export interface DirEntry { name: string; path: string }
export interface ListDir extends Base { type: "list_dir"; path?: string | null }
export interface DirList extends Base { type: "dir_list"; path: string; parent?: string | null; dirs: DirEntry[]; request_id?: string | null }
export interface SetPerm extends Base { type: "set_perm"; mode: PermissionMode }
export interface Perm extends Base { type: "perm"; mode: string }
export interface PermissionProfileInfo {
  id: CodexPermissionProfileId;
  description?: string | null;
  allowed: boolean;
}
export interface GetPermissionProfiles extends Base {
  type: "get_permission_profiles";
  client_id?: string | null;
  cwd?: string | null;
}
export interface PermissionProfiles extends Base {
  type: "permission_profiles";
  profiles: PermissionProfileInfo[];
  request_id?: string | null;
  cwd?: string | null;
}
export interface SetPermissionProfile extends Base {
  type: "set_permission_profile";
  profile: CodexPermissionProfileId;
}
export interface PermissionProfile extends Base {
  type: "permission_profile";
  profile?: CodexPermissionProfileId | null;
}
export interface SetWebSearch extends Base {
  type: "set_web_search";
  mode: CodexWebSearchMode;
}
export interface WebSearch extends Base {
  type: "web_search";
  mode: CodexWebSearchMode;
}
export interface GetContext extends Base { type: "get_context" }
export interface GetDiff extends Base { type: "get_diff"; file: string; theme?: DiffTheme }
export interface DiffReport extends Base { type: "diff_report"; file: string; diff: string; request_id?: string }
export interface GetFilePreview extends Base { type: "get_file_preview"; path: string; request_id: string }
export interface FilePreview extends Base { type: "file_preview"; path: string; request_id: string; format: "markdown" | "text" | "html" | "image" | "pdf"; content: string; media_type?: "image/png" | "image/jpeg" | "image/gif" | "image/webp" | "image/avif" | "application/pdf" | null; data?: string | null; converted_from?: string | null; size: number; truncated: boolean; mtime_ns: string; revision?: string | null; error?: string | null }
export interface SaveMarkdown extends Base { type: "save_markdown"; path: string; request_id: string; content: string; expected_size: number; expected_mtime_ns: string; expected_revision: string }
export interface FileSaveResult extends Base { type: "file_save_result"; path: string; request_id: string; status: "saved" | "conflict" | "error"; size: number; mtime_ns: string; revision?: string | null; error?: string | null }
export interface GetPreviewAsset extends Base { type: "get_preview_asset"; path: string; preview_id: string; request_id: string }
export interface PreviewAsset extends Base { type: "preview_asset"; path: string; preview_id: string; request_id: string; media_type?: "image/png" | "image/jpeg" | "image/gif" | "image/webp" | "image/avif" | null; data?: string | null; error?: string | null }
// On-demand bulk history: fetched once when a session is opened (like a web
// chat's GET /conversation) instead of replaying the ring buffer on every hello.
export interface GetHistory extends Base { type: "get_history"; session_id: string; client_id?: string | null; cwd?: string | null; before?: string | null; limit?: number | null; detail?: "summary" | "full" }
// `external`: this session's transcript is being appended to by a native `claude`/
// `codex` in the user's terminal. The wrapper mirrors those appends by broadcasting
// a fresh History; we render the session read-only (a cc session has one owner).
export interface ConversationImageRef { image_id: string; media_type: QueryImg["media_type"]; width: number; height: number; byte_size: number }
export interface ConversationTurn { id: string; clientMsgId?: string | null; prompt: string; blocks: unknown[]; done: boolean; forkPointId?: string | null; checkpointId?: string | null; interrupted?: boolean | null; error?: string | null; images?: QueryImg[] | null; imageRefs?: ConversationImageRef[] | null; files?: QueryFile[] | null; ts?: number | null; doneTs?: number | null; durationMs?: number | null; detailEventCount: number; detailLoaded: boolean }
export interface History extends Base { type: "history"; session_id: string; revision: string; generation?: string | null; build_seq?: number; live_seq?: number | null; authoritative?: boolean; error?: string | null; events: ServerEvent[]; turns?: ConversationTurn[]; detail?: "summary" | "full"; has_more: boolean; oldest_id?: string | null; newest_id?: string | null; before?: string | null; control?: SessionControl | null; external?: boolean; takeover_pending?: boolean; in_progress?: boolean; reset?: boolean }
export interface GetTurnDetail extends Base { type: "get_turn_detail"; session_id: string; turn_id: string; client_id?: string | null; revision?: string | null; before?: string | null; limit?: number | null }
export interface TurnDetail extends Base { type: "turn_detail"; session_id: string; turn_id: string; revision: string; authoritative?: boolean; error?: string | null; events: ServerEvent[]; has_more?: boolean; oldest_cursor?: string | null; has_newer?: boolean; newer_cursor?: string | null; before?: string | null }
export interface GetHistoryImage extends Base { type: "get_history_image"; session_id: string; turn_id: string; image_id: string; variant: "thumbnail" | "full"; request_id: string; client_id?: string | null; revision?: string | null }
export interface HistoryImage extends Base { type: "history_image"; session_id: string; turn_id: string; image_id: string; variant: "thumbnail" | "full"; request_id: string; revision: string; media_type?: QueryImg["media_type"] | null; width?: number | null; height?: number | null; data?: string | null; error?: string | null }
// Replayable barrier for a destructive history mutation. The full History
// replacement is one-shot and may exceed the ring byte budget; this small frame
// guarantees that reconnecting clients never retain turns removed by rollback.
export interface HistoryInvalidated extends Base { type: "history_invalidated"; session_id: string; revision: string; reason: "rollback" }
// File/diff previews contain bytes that a workspace mutation may have replaced.
// This small replayable marker closes them on every client, including reconnects.
export interface ArtifactInvalidated extends Base { type: "artifact_invalidated"; session_id: string; reason: "rollback" | "session_migration" }
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
export interface GetEngineCapabilities extends Base { type: "get_engine_capabilities"; engine: Engine; space?: Space; client_id?: string | null; cwd?: string | null; skills_only?: boolean }
export interface ManageEnginePlugin extends Base { type: "manage_engine_plugin"; engine: Engine; action: "install" | "uninstall"; plugin_id: string; space?: Space; client_id?: string | null; cwd?: string | null }
export interface ManageEngineSkill extends Base { type: "manage_engine_skill"; engine: Engine; action: "create" | "remove" | "enable" | "disable"; skill_id?: string | null; name?: string | null; description?: string | null; instructions?: string | null; scope?: "user" | "project"; space?: Space; client_id?: string | null; cwd?: string | null }
export interface ManageEngineHook extends Base { type: "manage_engine_hook"; engine: Engine; action: "create" | "remove"; hook_id?: string | null; event?: string | null; matcher?: string | null; command?: string | null; timeout?: number | null; scope?: "user" | "project"; space?: Space; client_id?: string | null; cwd?: string | null }
export type EngineCapabilityKind = "skill" | "plugin" | "app" | "mcp" | "hook";
export type EngineCapabilityAction = "install" | "uninstall" | "enable" | "disable" | "remove";
export interface EngineCapabilityItem { kind: EngineCapabilityKind; id: string; name: string; description?: string | null; enabled?: boolean | null; installed?: boolean | null; status?: string | null; scope?: string | null; source?: string | null; tool_count?: number | null; resource_count?: number | null; install_url?: string | null; actions?: EngineCapabilityAction[]; event?: string | null; matcher?: string | null; handler_type?: string | null; detail?: string | null }
export interface EngineCapabilities extends Base { type: "engine_capabilities"; engine: Engine; space: Space; request_id?: string | null; cwd: string; items: EngineCapabilityItem[]; errors?: string[]; notes?: string[]; skills_only: boolean }
export interface AskOption { label: string; ds?: string }
export interface AskUser extends Base { type: "ask_user"; ask_id: string; header?: string | null; question: string; options: AskOption[]; allow_text?: boolean; secret?: boolean; multi_select?: boolean }
export interface AskUserClosed extends Base { type: "ask_user_closed"; ask_id: string; reason: "answered" | "cancelled" | "timeout" | "superseded" }
export interface AnswerQuestion extends Base { type: "answer_question"; ask_id: string; answer: string | string[] }
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
  permission_profile?: string | null;
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
  /** Echoes the GetStatus command id; absent for unsolicited reports. */
  request_id?: string | null;
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
  /** False when the engine has not emitted an authoritative tokenUsage yet. */
  available?: boolean | null;
  /** Work-only conversation growth after the fresh-session startup baseline. */
  session_tokens?: number | null;
  /** Work-only startup zero point; raw total_tokens remains authoritative. */
  fixed_tokens?: number | null;
  /** session_tokens as a percentage of max_tokens. */
  session_percentage?: number | null;
  model?: string | null;
  is_auto_compact_enabled?: boolean | null;
  categories: ContextCategory[];
}

export type ServerEvent =
  | Pong | CommandAck | ReplayStart | ReplayEnd | Snapshot | StateEvent | QueryQueueState | QueuedQueryDetail | QueuedQueryUpdated | Model | Effort | Fast | CollaborationMode | BtwOpened | Perm | PermissionProfiles | PermissionProfile | WebSearch | ContextReport | DiffReport | FilePreview | FileSaveResult | PreviewAsset | History | TurnDetail | HistoryImage | HistoryInvalidated | ArtifactInvalidated | Models | EngineCapabilities | TakeoverState | SessionControl
  | AskUser | AskUserClosed | GoalState | StatusReport | Notice | RateLimitUpdate | RollbackResult
  | SessionList | SessionActivity | SessionFocus | SessionRekey | SessionForked | SessionMigrated | WorkDashboard | WorkArtifacts
  | DirList
  | UserMsg | TurnSteered | AssistantMsgStart | Delta | ToolUse | ToolDelta | ToolResult | AssistantMsgEnd
  | ProcessEvent | TurnPlan | TurnDiff | TurnBinding
  | TurnEnd | ErrorMsg | WrapperDisconnected | WrapperReconnected | Hello;

export const PROTOCOL_VERSION = 27;

const CONTROL_MODES = new Set<ControlMode>([
  "remote", "codex_shared", "claude_broker", "external_cli", "agent_view", "desktop",
]);
const WRITE_STATES = new Set<WriteState>([
  "writable", "read_only", "takeover_pending", "input_busy",
]);

/** Validate the small control snapshot before accepting user-controlled IDB data. */
export function isSessionControl(value: unknown): value is SessionControl {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const control = value as Record<string, unknown>;
  return control.type === "session_control"
    && CONTROL_MODES.has(control.control_mode as ControlMode)
    && WRITE_STATES.has(control.write_state as WriteState)
    && typeof control.terminal_attached === "boolean"
    && Number.isSafeInteger(control.revision) && (control.revision as number) >= 0
    && (control.reason == null || (typeof control.reason === "string"
      && control.reason.length <= 4096))
    && (control.generation == null || (typeof control.generation === "string"
      && /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$/.test(control.generation)))
    && (control.can_takeover == null || typeof control.can_takeover === "boolean");
}

/** Embedded controls inherit routing from their Snapshot/History/cache row.
 * A producer may omit the redundant sid, but an explicit sid must agree with
 * that trusted outer route or the control belongs to another session. */
export function sessionControlTargetsSid(
  control: SessionControl, sid: string,
): boolean {
  return control.sid == null || control.sid === sid;
}

export type ControlRevisionDisposition = "newer" | "same" | "stale" | "conflict";

/** Compare only authoritative control fields, not transport-envelope metadata. */
export function compareSessionControl(
  current: SessionControl | null | undefined,
  incoming: SessionControl,
): ControlRevisionDisposition {
  if (!current) return "newer";
  if ((incoming.generation ?? null) !== (current.generation ?? null)) {
    return "conflict";
  }
  if (incoming.revision > current.revision) return "newer";
  if (incoming.revision < current.revision) return "stale";
  const same = incoming.control_mode === current.control_mode
    && incoming.write_state === current.write_state
    && incoming.terminal_attached === current.terminal_attached
    && (incoming.reason ?? null) === (current.reason ?? null)
    && (incoming.can_takeover ?? null) === (current.can_takeover ?? null);
  return same ? "same" : "conflict";
}

/** Fail closed for view-only surfaces even if a malformed producer says writable. */
export function sessionControlLocksInput(control: SessionControl): boolean {
  return control.control_mode === "external_cli"
    || control.control_mode === "agent_view"
    || control.control_mode === "desktop"
    || control.write_state !== "writable";
}

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

/** Continue one existing Codex thread in another working directory. */
export function makeMigrateSessionCommand(
  sessionId: string, cwd: string, requestId: string, ts: number,
): MigrateSession {
  return {
    v: PROTOCOL_VERSION,
    type: "migrate_session",
    session_id: sessionId,
    cwd,
    request_id: requestId,
    ts,
  };
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
