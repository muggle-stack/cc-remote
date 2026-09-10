import type {
  AssistantChannel,
  AsyncQuestionSpec,
  ConversationImageRef,
  PlanEntry,
  ProcessKind,
  ProcessStatus,
  QueryFile,
  QueryImg,
  ServerEvent,
  ToolCategory,
  TurnChangeSummary,
} from "../protocol";

/** Browser-only fallback used when an authoritative idle History snapshot
 * proves that a locally painted turn stopped, but no exact terminal frame was
 * observed. Kept beside Turn so reconnect repair can recognize legacy cached
 * projections without comparing arbitrary user-visible errors. */
export const UNKNOWN_TERMINAL_ERROR =
  "会话已结束，但未收到完整的终止状态。";

export interface TextBlock {
  kind: "text";
  message_id: string;
  text: string;
  done: boolean;
  channel?: AssistantChannel;
  delivery?: "async";
  questions?: AsyncQuestionSpec[];
  /** Source event time and detached-follow-up scope, preserved for chronology. */
  startedTs?: number;
  doneTs?: number;
  background?: boolean | null;
  /** Local source order for the bounded live spill archive. Never sent on wire. */
  liveOrder?: number;
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
  startedTs?: number;
  doneTs?: number;
  background?: boolean | null;
  /** Local source order for the bounded live spill archive. Never sent on wire. */
  liveOrder?: number;
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
  /** Background Claude Agent work stays visible in its card without reopening
   * the enclosing session after the parent ResultMessage. */
  background?: boolean | null;
  explanation?: string | null;
  plan?: PlanEntry[];
  done: boolean;
  startedTs?: number;
  updatedTs?: number;
  /** Completion is a later chronological boundary than a detached start. */
  terminalOrder?: number;
  terminalTs?: number;
  /** Local source order for the bounded live spill archive. Never sent on wire. */
  liveOrder?: number;
}

export type Block = TextBlock | ToolBlock | ProcessBlock;

export interface TurnDetailSegment {
  pageKey: string;
  before: string | null;
  events: ServerEvent[];
  hasMore: boolean;
  oldestCursor: string | null;
  hasNewer: boolean;
  newerCursor: string | null;
  encodedChars: number;
}

export interface TurnDetailProjection {
  segments: TurnDetailSegment[];
  blocks: Block[];
  capped: boolean;
  hasMore: boolean;
  oldestCursor: string | null;
  hasNewer: boolean;
  newerCursor: string | null;
}

export interface Turn {
  id: string;
  fileChanges?: TurnChangeSummary | null;
  fileChangesTurnId?: string;
  /** Codex turn/steer's browser id persisted beside a distinct history cursor. */
  clientMsgId?: string;
  /** Native history user id when it differs from the optimistic browser id. */
  historyTurnId?: string;
  /** Engine-specific authoritative branch point. */
  forkPointId?: string;
  /** Some native engines finish a visible steer segment before a forkable turn. */
  forkAvailable?: boolean;
  /** Claude's authoritative top-level user transcript UUID. */
  checkpointId?: string;
  /** @deprecated Read only while migrating CACHE_VER=5 entries. */
  codexTurnId?: string;
  /** Routing-only native task identity for a steered live segment. */
  liveTaskId?: string;
  prompt: string;
  blocks: Block[];
  done: boolean;
  interrupted?: boolean;
  error?: string;
  /** Browser-side provisional terminal classification. It lets a later
   * authoritative History snapshot repair reconnect/compaction boundaries;
   * it is never native terminal proof by itself. */
  terminalSource?: "unexpected_interrupt" | "remote_interrupt" | "failed"
    | "compact_continuation" | "idle_history_recovery";
  progress?: string;
  images?: QueryImg[];
  imageRefs?: ConversationImageRef[];
  files?: QueryFile[];
  ts?: number;
  doneTs?: number;
  durationMs?: number;
  /** Exact visible-process evidence, or `unknown` when a bounded native
   * summary proves only that more detail exists. */
  processDetailState?: "none" | "present" | "unknown";
  detailReasons?: Array<
    "process" | "prompt_truncated" | "answer_truncated" | "image_deferred"
  >;
  /** First/last trustworthy visible-process event, never the user-message
   * timestamp. These drive the process timer without inheriting prompt wait. */
  processStartedTs?: number;
  processDoneTs?: number;
  detailEventCount?: number;
  detailLoaded?: boolean;
  detailLoading?: boolean;
  /** Transient source/read failure for the heavyweight process disclosure. */
  detailError?: string;
  /** Exact failed page request retained only long enough for an in-place retry. */
  detailRetryBefore?: string | null;
  detailRetryDirection?: "initial" | "older" | "newer";
  /** A stale cursor is being replaced from the authoritative newest page. */
  detailResetPending?: boolean;
  detailHasMore?: boolean;
  detailOldestCursor?: string | null;
  detailHasNewer?: boolean;
  detailNewerCursor?: string | null;
  /** Heavy, cursor-paged process history; never subject to Turn.blocks caps. */
  detailProjection?: TurnDetailProjection;
  /** Initial disclosure click keeps fetching older pages until EOF or cap. */
  detailAutoLoad?: boolean;
  /** Same-revision IndexedDB process is painted provisionally while one
   * authoritative newest-page detail read replaces it after refresh. */
  detailRestorePending?: boolean;
  /** Reopen only the newest compact cached process after a full page refresh;
   * a user's subsequent disclosure choice overrides this hint. */
  detailRestoreOpen?: boolean;
  /** The automatic refresh repair installed only the newest server page; an
   * explicit disclosure may still request the remaining older pages. */
  detailRestoreIncomplete?: boolean;
  /** The live reducer evicted completed process blocks from its bounded tail.
   * Source-backed detail pages, rather than a visible omission card, own them. */
  liveBlocksSpilled?: boolean;
  /** Monotonic count of blocks evicted from the live in-memory tail. */
  liveSpilledBlockCount?: number;
  /** Bounded provisional archive bridging the last authoritative detail
   * snapshot and the current live tail. It is never a source authority. */
  liveSpillBlocks?: Block[];
  /** Spill count captured by the most recent coalesced detail refresh. */
  liveSpillRefreshCount?: number;
  /** Next local block ordinal; used only to preserve spill chronology. */
  nextLiveBlockOrder?: number;
}
