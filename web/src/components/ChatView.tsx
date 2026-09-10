import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type PointerEvent,
  type TouchEvent,
  type WheelEvent,
} from "react";
import {
  defaultRangeExtractor,
  useVirtualizer,
} from "@tanstack/react-virtual";
import type {
  Block, ProcessBlock, TextBlock, Turn,
} from "../domain/conversation";
import type { Space } from "../protocol";
import { MessageBlock } from "./MessageBlock";
import { Icon, ClaudeMark, ClaudeWorking, ClaudeSpark } from "../icons";
import { canForkTurn } from "../session-worktree";
import {
  BackgroundProcessDock,
  GeneratedImagePreview,
  ProcessTimeline,
} from "./ProcessTimeline";
import {
  finalTextBlocks,
  generatedImageIdentity,
  generatedOutputImages,
  hasActiveProcess,
  presentableProcessBlocks,
} from "../process-blocks";
import { isMarkdownPath } from "../preview-path";
import { collectTurnFileChanges } from "../file-changes";
import type { InlineImageAsset } from "../inline-image-assets";
import type { PreviewAuthorizationState } from "../reducer";
import {
  historyImageAssetKey,
  type HistoryImageAsset,
  type HistoryImageVariant,
} from "../history-image-assets";
import { ImageLightbox } from "./ImageLightbox";
import { presentHistoricalTurnProblem } from "../problem-presentation";
import { queryImageDimensions } from "../img";
import {
  updateTurnKeySnapshot,
  type TurnKeySnapshot,
} from "../virtual-turn-keys";
import { TurnImagePreviewCache } from "../turn-image-previews";
import type { TextSelectionGuard } from "../history-selection-guard";
import { HistoryUserImage } from "./HistoryUserImage";
import {
  HistoryAnchorController,
  HistoryPageActivityController,
  historyPageStatus,
  isAtHistoryEdge,
  isAtLatestEdge,
  measureBottom,
  OlderHistoryLoadGate,
  shouldAutoLoadOlderHistory,
  shouldAutoLoadNewerHistory,
  ScrollFollowController,
  type HistoryAnchorPoint,
  type HistoryPageActivity,
  type HistoryPageDirection,
  type ScrollFollowSnapshot,
  type ScrollMetrics,
} from "../scroll-follow";
import {
  ScrollCoordinator,
  type ScrollCommand,
} from "../scroll-coordinator";
import {
  HISTORY_DETAIL_REQUEST_TIMEOUT_MS,
  HISTORY_REQUEST_TIMEOUT_MS,
} from "../history-requests";
import { mergeDetailWithLiveTail } from "../history-merge";
import { asyncQuestionKey, presentAsyncQuestionReplies } from "../async-question-presentation";

const AsyncQuestionCard = lazy(() => import("./AsyncQuestionCard"));
const AsyncQuestionHost = lazy(() => import("./AsyncQuestionDialog"));
const PagePreviewLinks = lazy(() => import("./PagePreviewLinks").then((module) => ({ default: module.PagePreviewLinks })));

const WHEEL_GESTURE_IDLE_MS = 180;
const HISTORY_VIRTUAL_ESTIMATE_PX = 280;
const HISTORY_VIRTUAL_OVERSCAN = 6;
const HISTORY_TURN_GAP_PX = 22;
const HISTORY_LOAD_HEADER_PX = 52;
const THREAD_CONTENT_TOP_PX = 20;
const THREAD_CONTENT_BOTTOM_PX = 8;
const WORK_THREAD_CONTENT_TOP_PX = 26;
const WORK_THREAD_CONTENT_BOTTOM_PX = 20;
const USER_SCROLL_INTENT_IDLE_MS = 260;
// Overlay scrollbars do not subtract their painted width from clientWidth.
// Keep a small right-edge hit region for the native thumb while still
// requiring the event target to be the scroll container itself.
const SCROLLBAR_POINTER_GUTTER_PX = 14;
// HistoryRequestCoordinator allows replacement after 15 seconds. Release the
// local anchor just after that boundary so an unanswered command cannot lock
// pagination forever.
const HISTORY_PAGE_REQUEST_TIMEOUT_MS = HISTORY_REQUEST_TIMEOUT_MS + 1_000;
// Keep the exact process edge until all synchronous and shortly-delayed
// Markdown/image measurements settle, but never pin a huge virtual row
// indefinitely after a successful detail response.
const DETAIL_ANCHOR_QUIET_MS = 300;
const DETAIL_ANCHOR_MAX_SETTLE_MS = 2_000;

type UserScrollDirection = "history" | "latest" | "unknown";

interface CapturedHistoryBoundary extends HistoryAnchorPoint {
  anchorOffset: number;
}

interface TouchTransactionBoundary {
  generation: number;
  captureEventTimestamp: number;
  baselineY: number;
  movedAfterCapture: boolean;
}

interface RetainedMeasurementBoundary {
  scope: string;
  sid: string | null;
  revision: string | null;
  viewId: string;
  turnId: string;
  anchorOffset: number;
}

interface HistoryViewportPresentation {
  sid: string | null;
  scope: string;
  authorityScope: string;
  generation: string | null;
  turns: Turn[];
  hasMore: boolean;
  cursor: string | null;
  browseMode: boolean;
  hasNewer: boolean;
  windowEpoch: number;
}

interface HistoryPageRequestTransaction {
  sid: string | null;
  revision: string | null;
  authorityScope: string;
  viewId: string | null;
  direction: HistoryPageDirection;
  before: string | null;
  windowEpoch: number;
  activityKey: number;
}

interface HistoryViewportScopeTransition {
  source: HistoryViewportPresentation;
  request: HistoryPageRequestTransaction;
  presentation: HistoryViewportPresentation;
}

function sameHistoryViewportPresentation(
  left: HistoryViewportPresentation,
  right: HistoryViewportPresentation,
): boolean {
  return left.scope === right.scope
    && left.authorityScope === right.authorityScope
    && left.turns === right.turns
    && left.hasMore === right.hasMore
    && left.cursor === right.cursor
    && left.browseMode === right.browseMode
    && left.hasNewer === right.hasNewer
    && left.windowEpoch === right.windowEpoch;
}

function acceptedHistoryViewportTransition(
  leaseActive: boolean,
  transition: HistoryViewportScopeTransition | null,
  request: HistoryPageRequestTransaction | null,
  presented: HistoryViewportPresentation,
  incoming: HistoryViewportPresentation,
): HistoryViewportPresentation | null {
  if (!leaseActive || !transition || !request) return null;
  if (request !== transition.request
      || presented.scope !== transition.source.scope
      || presented.authorityScope !== transition.source.authorityScope
      || incoming.scope !== transition.presentation.scope
      || incoming.authorityScope !== transition.presentation.authorityScope
      || incoming.generation !== transition.presentation.generation
      || !incoming.browseMode) return null;
  return transition.presentation;
}

interface TextSelectionRetention {
  scope: string;
  anchorTurnId: string;
  focusTurnId: string;
  pointerId: number | null;
  interactionToken: number | null;
  dragging: boolean;
  releaseAnchorTurnId: string | null;
  releaseAnchorOffset: number | null;
}

interface TextSelectionCandidate {
  scope: string;
  pointerId: number;
}

function nativeSelectionBoundary(selection: Selection) {
  return {
    anchor: selection.anchorNode, anchorOffset: selection.anchorOffset,
    focus: selection.focusNode, focusOffset: selection.focusOffset,
  };
}

const TEXT_SELECTION_EXCLUDED_SELECTOR = [
  "button",
  "a",
  "input",
  "textarea",
  "select",
  "option",
  "summary",
  "[contenteditable='true']",
  "[role='button']",
  ".mermaid-block",
].join(",");

function selectionTurnId(
  root: HTMLElement | null,
  node: Node | null,
): string | null {
  if (!root || !node || !root.contains(node)) return null;
  const element = node instanceof Element ? node : node.parentElement;
  if (element?.closest("input, textarea, select, [contenteditable='true']")) return null;
  return element?.closest<HTMLElement>("[data-turn-id]")?.dataset.turnId ?? null;
}

type DetailPageDirection = "initial" | "older" | "newer";
type DetailAnchorEdge = "start" | "end";

interface DetailAnchorTransaction {
  scope: string;
  turnId: string;
  edge: DetailAnchorEdge;
  anchorOffset: number;
  token: number;
  initialFingerprint: string;
  sawLoading: boolean;
  responseSettled: boolean;
  requestTimer: number | null;
  quietTimer: number | null;
  maxSettleTimer: number | null;
  firstFrame: number | null;
  secondFrame: number | null;
  observer: ResizeObserver | null;
  observedNode: HTMLElement | null;
}

interface HistoryPageLoadAcceptance {
  accepted: true;
  /** The first runtime page creates its browse view synchronously in App. */
  viewId?: string;
  /** The first runtime page also freezes its new cache authority scope. */
  scopeKey?: string;
  generation?: string | null;
}

type HistoryPageLoadResult = boolean | HistoryPageLoadAcceptance;

function readScrollMetrics(el: HTMLDivElement): ScrollMetrics {
  return {
    scrollHeight: el.scrollHeight,
    scrollTop: el.scrollTop,
    clientHeight: el.clientHeight,
  };
}

function formatTime(ts: number): string {
  const d = new Date(ts);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

const BACKGROUND_FOLLOWUP_LABEL_MAX_CHARS = 160;

function backgroundFollowupLabel(process?: ProcessBlock): string {
  const raw = process?.summary || process?.title || "后台任务完成";
  return raw.length <= BACKGROUND_FOLLOWUP_LABEL_MAX_CHARS
    ? raw
    : `${raw.slice(0, BACKGROUND_FOLLOWUP_LABEL_MAX_CHARS - 1)}…`;
}

function backgroundFollowupBoundaries(
  finalBlocks: TextBlock[], timelineBlocks: Block[],
): Map<string, ProcessBlock | null> {
  const completions = timelineBlocks.filter(
    (block): block is ProcessBlock => block.kind === "process"
      && block.background === true && block.done
      && (block.processKind === "task" || block.processKind === "agent"),
  );
  const used = new Set<string>();
  const boundaries = new Map<string, ProcessBlock | null>();
  let emittedFallback = false;
  for (const block of finalBlocks) {
    if (block.background !== true) continue;
    const candidate = block.startedTs == null ? undefined : completions
      .filter((process) => !used.has(process.item_id)
        && process.terminalTs != null
        && process.terminalTs <= block.startedTs!)
      .sort((left, right) => (right.terminalTs ?? 0) - (left.terminalTs ?? 0))[0];
    if (candidate) {
      used.add(candidate.item_id);
      boundaries.set(block.message_id, candidate);
      continue;
    }
    // Legacy history can lack source clocks. Mark the first detached reply once
    // without manufacturing a relationship for every text fragment.
    if (boundaries.size === 0 && !emittedFallback) {
      boundaries.set(block.message_id, null);
      emittedFallback = true;
    }
  }
  return boundaries;
}

function detailTurnFingerprint(turn: Turn): string {
  return [
    turn.detailLoaded ? "1" : "0",
    turn.detailOldestCursor ?? "",
    turn.detailNewerCursor ?? "",
    turn.detailHasMore ? "1" : "0",
    turn.detailHasNewer ? "1" : "0",
    turn.detailAutoLoad ? "1" : "0",
    turn.detailProjection?.segments.length ?? 0,
    turn.detailProjection?.blocks.length ?? 0,
    turn.liveSpillBlocks?.length ?? 0,
    turn.liveSpilledBlockCount ?? 0,
    turn.blocks.length,
  ].join("\u0000");
}

export function ChatView({ sid, turns: incomingTurns, engine = "claude", loading,
  hasMore: incomingHasMore,
  historyPagingReady = true,
  historyRevision = null, historyViewRevision = historyRevision,
  historyGeneration = null,
  historyViewId = null, historyScopeKey = null,
  historyWindowEpoch: incomingHistoryWindowEpoch = 0,
  historyCursor: incomingHistoryCursor = null,
  browseMode: incomingBrowseMode = false, hasNewer: incomingHasNewer = false,
  onLoadMore, onLoadNewer, onReturnLatest,
  onLoadDetail, onEdit, onReplyAsyncQuestion, asyncReplyMode, onOpenTurnDiff, onPreviewMarkdown, onOpenFile,
  onOpenArtifacts, onFork, forkingPointId, imageAssets, onLoadImage,
  onAuthorizeImage,
  historyImageAssets, onLoadHistoryImage,
  onTextSelectionGuardChange,
  externalPlanProgress,
  backgroundProcesses = [],
  onOpenAgent,
  activeTurnId = null,
  ambiguousActiveTurnIds = [],
  surface = "code" }: {
  sid: string | null;
  turns: Turn[];
  surface?: Space;
  engine?: "claude" | "codex";
  loading?: boolean;
  hasMore?: boolean;
  historyPagingReady?: boolean;
  historyRevision?: string | null;
  // A non-destructive replay recovery keeps the prior view scope across the
  // atomic History swap so the virtualizer preserves the current reading row.
  // Pagination/detail still use the authoritative historyRevision above.
  historyViewRevision?: string | null;
  historyGeneration?: string | null;
  historyViewId?: string | null;
  historyScopeKey?: string | null;
  historyWindowEpoch?: number;
  historyCursor?: string | null;
  browseMode?: boolean;
  hasNewer?: boolean;
  onLoadMore?: (anchorTurnId?: string) => HistoryPageLoadResult;
  onLoadNewer?: (anchorTurnId?: string) => HistoryPageLoadResult;
  onReturnLatest?: () => void;
  onLoadDetail?: (
    turnId: string,
    before?: string | null,
    autoLoad?: boolean,
  ) => boolean;
  onEdit?: (prompt: string) => void;
  onReplyAsyncQuestion?: (prompt: string) => boolean;
  asyncReplyMode?: "query" | "steer";
  onGetDiff?: (file: string) => void;
  onOpenTurnDiff?: (files: string[], diff: string) => void;
  onPreviewMarkdown?: (file: string) => void;
  onOpenFile?: (file: string, line?: number) => void;
  onOpenArtifacts?: () => void;
  onFork?: (forkPointId: string) => void;
  forkingPointId?: string | null;
  imageAssets?: Record<string, InlineImageAsset>;
  onLoadImage?: (path: string, previewId?: string) => boolean;
  onAuthorizeImage?: (
    authorization: PreviewAuthorizationState,
    decision: "allow" | "deny",
  ) => boolean;
  historyImageAssets?: Record<string, HistoryImageAsset>;
  onLoadHistoryImage?: (
    turnId: string, imageId: string, variant: HistoryImageVariant,
  ) => boolean;
  onTextSelectionGuardChange?: (guard: TextSelectionGuard | null) => void;
  onOpenAgent?: (runId: string, title?: string) => void;
  externalPlanProgress?: {
    turnId: string;
    itemId: string;
  } | null;
  backgroundProcesses?: ProcessBlock[];
  /** Exact displayed row owned by the still-running native task. Runtime-only:
   * never infer this from array position, final text, or historical activity. */
  activeTurnId?: string | null;
  /** More than one displayed row exactly aliases the active native owner.
   * Empty for idle/browse/missing-owner states, which must stay fail-closed. */
  ambiguousActiveTurnIds?: readonly string[];
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const contentSizerRef = useRef<HTMLDivElement>(null);
  const controllerRef = useRef<ScrollFollowController | null>(null);
  if (!controllerRef.current) controllerRef.current = new ScrollFollowController();
  const scrollCoordinatorRef = useRef(new ScrollCoordinator());
  const [scrollPolicyEpoch, setScrollPolicyEpoch] = useState(0);
  const [scrollState, setScrollState] = useState<ScrollFollowSnapshot>(() =>
    controllerRef.current!.snapshot());
  const [zoom, setZoom] = useState<
    | { kind: "data"; src: string; alt: string }
    | { kind: "history"; turnId: string; imageId: string; alt: string }
    | null
  >(null);
  const historyAnchorRef = useRef(new HistoryAnchorController());
  const historyPageActivityRef = useRef(new HistoryPageActivityController());
  const [historyPageActivity, setHistoryPageActivity] =
    useState<HistoryPageActivity | null>(null);
  const beginHistoryPageActivity = useCallback((
    input: Omit<HistoryPageActivity, "key">,
  ): HistoryPageActivity => {
    const activity = historyPageActivityRef.current.begin(input);
    setHistoryPageActivity(activity);
    return activity;
  }, []);
  const completeHistoryPageActivity = useCallback((key?: number): boolean => {
    if (!historyPageActivityRef.current.complete(key)) return false;
    setHistoryPageActivity(null);
    return true;
  }, []);
  const historyRequestRef = useRef<HistoryPageRequestTransaction | null>(null);
  const turnNodeRefs = useRef(new Map<string, HTMLDivElement>());
  const textSelectionCandidateRef = useRef<TextSelectionCandidate | null>(null);
  const textSelectionRef = useRef<TextSelectionRetention | null>(null);
  const nativeSelectionBoundaryRef = useRef<ReturnType<typeof nativeSelectionBoundary> | null>(null);
  const textSelectionReleaseFrameRef = useRef<number | null>(null);
  const selectionScrollIntentFnRef = useRef<(() => void) | null>(null);
  const [textSelection, setTextSelection] =
    useState<TextSelectionRetention | null>(null);
  const detailAnchorRef = useRef<DetailAnchorTransaction | null>(null);
  const cancelDetailAnchorFnRef = useRef<
    ((releaseInteraction?: boolean) => void) | null
  >(null);
  const historyReleaseFrameRef = useRef<number | null>(null);
  const historyRequestTimeoutRef = useRef<{
    generation: number | null;
    activityKey: number;
    timer: number;
  } | null>(null);
  const historyLoadGateRef = useRef(new OlderHistoryLoadGate());
  const wheelHistoryLoadGateRef = useRef(new OlderHistoryLoadGate());
  const wheelGestureTimerRef = useRef<number | null>(null);
  const wheelGestureActiveRef = useRef(false);
  const autoLoadedBoundaryRef = useRef<string | null>(null);
  const lastScrollTopRef = useRef(0);
  const renderedScrollScopeRef = useRef<string | null>(null);
  const touchYRef = useRef<number | null>(null);
  const touchMomentumActiveRef = useRef(false);
  const touchEventClockOffsetRef = useRef<number | null>(null);
  const touchTransactionBoundaryRef =
    useRef<TouchTransactionBoundary | null>(null);
  const touchHistoryGenerationRef = useRef<number | null>(null);
  const userScrollIntentRef = useRef(false);
  const userScrollDirectionRef = useRef<UserScrollDirection | null>(null);
  const userScrollIntentTimerRef = useRef<number | null>(null);
  const scrollbarDragIntentRef = useRef<{
    scope: string;
    pointerId: number;
  } | null>(null);
  // A real downward gesture may finish while the final cached-newer page is
  // still installing. Remember only that exact viewport scope so the settled
  // page can hand back to the live runtime without requiring a second scroll.
  const manualLatestIntentRef = useRef<string | null>(null);
  const turnKeySnapshotRef = useRef<TurnKeySnapshot | null>(null);
  const turnImagePreviewCacheRef = useRef(new TurnImagePreviewCache());
  // `historyViewRevision` is kept as a compatibility scope for the
  // non-destructive recovery path. Deep-history browsing supplies an explicit
  // stable view id: revision/view changes reset, window paging does not.
  const resolvedHistoryViewId = historyViewId ?? historyViewRevision ?? "";
  const incomingScrollScope = historyViewId == null
    ? `${historyScopeKey ?? ""}\u0000${sid ?? ""}\u0000${resolvedHistoryViewId}`
    : `${historyScopeKey ?? ""}\u0000${sid ?? ""}\u0000${historyRevision ?? ""}\u0000${resolvedHistoryViewId}`;
  const incomingHistoryPresentation: HistoryViewportPresentation = {
    sid,
    scope: incomingScrollScope,
    authorityScope: [
      historyScopeKey ?? "", sid ?? "", historyRevision ?? "",
      historyGeneration ?? "",
    ].join("\u0000"),
    generation: historyGeneration,
    turns: incomingTurns,
    hasMore: !!incomingHasMore,
    cursor: incomingHistoryCursor,
    browseMode: incomingBrowseMode,
    hasNewer: !!incomingHasNewer,
    windowEpoch: incomingHistoryWindowEpoch,
  };
  const latestHistoryPresentationRef =
    useRef<HistoryViewportPresentation>(incomingHistoryPresentation);
  latestHistoryPresentationRef.current = incomingHistoryPresentation;
  const pendingHistoryPresentationRef =
    useRef<HistoryViewportPresentation | null>(null);
  const historyViewportLeaseRef = useRef(false);
  const historyViewportTransitionRef =
    useRef<HistoryViewportScopeTransition | null>(null);
  const [presentedHistory, setPresentedHistory] =
    useState<HistoryViewportPresentation>(incomingHistoryPresentation);
  const transitionPresentation = acceptedHistoryViewportTransition(
    historyViewportLeaseRef.current,
    historyViewportTransitionRef.current,
    historyRequestRef.current,
    presentedHistory,
    incomingHistoryPresentation,
  );
  // A revision/generation handoff can briefly expose an empty replacement.
  // Keep the same session's complete old scope mounted until its rows arrive;
  // same-authority rollback invalidation deliberately does not qualify.
  const retainPendingHistory = loading && sid
    && presentedHistory.sid === sid && presentedHistory.turns.length
    && !incomingTurns.length
    && presentedHistory.authorityScope
      !== incomingHistoryPresentation.authorityScope;
  const scopedPresentedHistory = retainPendingHistory
    ? presentedHistory
    : (presentedHistory.scope === incomingScrollScope
      ? presentedHistory
      : transitionPresentation ?? incomingHistoryPresentation);
  // Every scroll/virtualization transaction must use the presentation's own
  // scope. During a pending empty handoff that is intentionally the old scope,
  // never the incoming revision's scope.
  const scrollScope = scopedPresentedHistory.scope;
  const {
    turns,
    hasMore,
    cursor: historyCursor,
    browseMode,
    hasNewer,
    windowEpoch: historyWindowEpoch,
  } = scopedPresentedHistory;
  const supplemental = useMemo(() => presentAsyncQuestionReplies(turns), [turns]);
  const asyncQuestionScope = JSON.stringify([historyScopeKey ?? "", sid]);
  const [openAsyncQuestion, setOpenAsyncQuestion] = useState<{
    scope: string; messageId: string | null;
  } | null>(null);
  useLayoutEffect(() => { setOpenAsyncQuestion(null); }, [asyncQuestionScope]);
  turnImagePreviewCacheRef.current.update(sid, turns);

  useLayoutEffect(() => {
    const incoming = latestHistoryPresentationRef.current;
    if (retainPendingHistory) {
      pendingHistoryPresentationRef.current = incoming;
      return;
    }
    if (presentedHistory.scope !== incoming.scope) {
      const retained = acceptedHistoryViewportTransition(
        historyViewportLeaseRef.current,
        historyViewportTransitionRef.current,
        historyRequestRef.current,
        presentedHistory,
        incoming,
      );
      if (retained) {
        // The first runtime page creates a cache-backed browse scope. Keep the
        // exact pre-request rows mounted under that accepted target scope until
        // the physical gesture ends; even a zero-latency response is staged.
        pendingHistoryPresentationRef.current =
          sameHistoryViewportPresentation(retained, incoming) ? null : incoming;
        historyViewportTransitionRef.current = null;
        setPresentedHistory(retained);
        return;
      }
      historyViewportLeaseRef.current = false;
      historyViewportTransitionRef.current = null;
      pendingHistoryPresentationRef.current = null;
      setPresentedHistory(incoming);
      return;
    }
    if (historyViewportLeaseRef.current) {
      pendingHistoryPresentationRef.current = incoming;
      return;
    }
    setPresentedHistory((current) =>
      sameHistoryViewportPresentation(current, incoming) ? current : incoming);
  }, [
    incomingBrowseMode, incomingHasMore, incomingHasNewer,
    incomingHistoryCursor, incomingHistoryWindowEpoch, incomingTurns,
    incomingHistoryPresentation.authorityScope,
    incomingScrollScope, presentedHistory, retainPendingHistory,
  ]);

  const beginHistoryViewportLease = useCallback(() => {
    historyViewportLeaseRef.current = true;
  }, []);
  const flushHistoryViewportLease = useCallback(() => {
    historyViewportLeaseRef.current = false;
    historyViewportTransitionRef.current = null;
    const pending = pendingHistoryPresentationRef.current
      ?? latestHistoryPresentationRef.current;
    pendingHistoryPresentationRef.current = null;
    setPresentedHistory((current) =>
      sameHistoryViewportPresentation(current, pending) ? current : pending);
  }, []);
  const activeScrollScopeRef = useRef(scrollScope);
  activeScrollScopeRef.current = scrollScope;
  const hasOpenTailRef = useRef(false);
  const newestTurn = turns.at(-1);
  const hasOpenTail = !!newestTurn && (
    !newestTurn.done || newestTurn.blocks.some((block) => !block.done)
  );
  hasOpenTailRef.current = hasOpenTail;
  const turnKeySnapshot = updateTurnKeySnapshot(
    turnKeySnapshotRef.current,
    turns,
    scrollScope,
  );
  turnKeySnapshotRef.current = turnKeySnapshot;
  const [activeHistoryGeneration, setActiveHistoryGeneration] = useState<number | null>(null);
  const [measurementBoundary, setMeasurementBoundary] =
    useState<RetainedMeasurementBoundary | null>(null);
  const [processDisclosureOpen, setProcessDisclosureOpen] = useState<
    Record<string, boolean>
  >({});
  const rememberProcessDisclosure = useCallback((key: string, open: boolean) => {
    setProcessDisclosureOpen((current) => {
      const next = { ...current, [key]: open };
      const keys = Object.keys(next);
      if (keys.length > 512) delete next[keys[0]];
      return next;
    });
  }, []);
  const stagedOlderMetadata = (
    scopedPresentedHistory.scope !== incomingHistoryPresentation.scope
    || scopedPresentedHistory.generation
      !== incomingHistoryPresentation.generation
    || scopedPresentedHistory.cursor !== incomingHistoryPresentation.cursor
    || scopedPresentedHistory.hasMore !== incomingHistoryPresentation.hasMore
  );
  const olderHistoryAvailability: "unavailable" | "pending" | "ready" =
    !incomingHistoryPresentation.hasMore
      ? "unavailable"
      : stagedOlderMetadata || !historyPagingReady || !hasMore
          || !incomingHistoryPresentation.cursor
        ? "pending"
        : "ready";
  const showOlderHistory = olderHistoryAvailability !== "unavailable";
  const canLoadOlder = olderHistoryAvailability === "ready";
  const deferredOlderIntentRef = useRef<{
    authorityScope: string;
    viewId: string;
    cursor: string | null;
  } | null>(null);
  const canLoadNewer = browseMode && !!hasNewer;
  const historyInsetRef = useRef({
    scope: scrollScope,
    enabled: !!hasMore,
  });
  if (historyInsetRef.current.scope !== scrollScope) {
    historyInsetRef.current = {
      scope: scrollScope,
      enabled: !!hasMore,
    };
  } else if (hasMore) {
    historyInsetRef.current.enabled = true;
  }
  const contentTopInset = surface === "work"
    ? WORK_THREAD_CONTENT_TOP_PX : THREAD_CONTENT_TOP_PX;
  const contentBottomInset = surface === "work"
    ? WORK_THREAD_CONTENT_BOTTOM_PX : THREAD_CONTENT_BOTTOM_PX;
  const historyTopInset = contentTopInset + (
    historyInsetRef.current.enabled ? HISTORY_LOAD_HEADER_PX : 0
  );
  const historyBottomInsetRef = useRef({
    scope: scrollScope,
    enabled: canLoadNewer,
  });
  if (historyBottomInsetRef.current.scope !== scrollScope) {
    historyBottomInsetRef.current = { scope: scrollScope, enabled: canLoadNewer };
  } else if (canLoadNewer) {
    historyBottomInsetRef.current.enabled = true;
  }
  const historyBottomInset = contentBottomInset + (
    historyBottomInsetRef.current.enabled
      ? HISTORY_LOAD_HEADER_PX + 8 : 8
  );
  const activeHistoryAnchor = historyAnchorRef.current.current();
  const keyedPrependActive = activeHistoryGeneration !== null
    && activeHistoryAnchor?.generation === activeHistoryGeneration
    && activeHistoryAnchor.sid === sid
    && activeHistoryAnchor.revision === historyRevision
    && (activeHistoryAnchor.viewId ?? null) === resolvedHistoryViewId;
  const keyedPrependResponseReady = keyedPrependActive
    && historyPageStatus(activeHistoryAnchor, {
      sid, revision: historyRevision, cursor: historyCursor,
      viewId: resolvedHistoryViewId, hasMore: !!hasMore,
      windowEpoch: historyWindowEpoch, hasNewer: canLoadNewer,
    }) === "complete";
  const scopedMeasurementBoundary = measurementBoundary?.scope === scrollScope
    ? measurementBoundary : null;
  const activeTextSelection = textSelection?.scope === scrollScope
    ? textSelection : null;
  const retainedSelectionBoundary =
    activeTextSelection?.releaseAnchorTurnId != null
    && activeTextSelection.releaseAnchorOffset != null
      ? {
        scope: scrollScope,
        sid,
        revision: historyRevision,
        viewId: resolvedHistoryViewId,
        turnId: activeTextSelection.releaseAnchorTurnId,
        anchorOffset: activeTextSelection.releaseAnchorOffset,
      }
      : null;
  const retainedMeasurementBoundary = retainedSelectionBoundary
    ?? scopedMeasurementBoundary
    ?? (keyedPrependActive && activeHistoryAnchor ? {
      scope: scrollScope,
      sid,
      revision: historyRevision,
      viewId: resolvedHistoryViewId,
      turnId: activeHistoryAnchor.anchorTurnId,
      anchorOffset: activeHistoryAnchor.anchorOffset,
    } : null);
  const retainedMeasurementBoundaryRef =
    useRef<RetainedMeasurementBoundary | null>(retainedMeasurementBoundary);
  retainedMeasurementBoundaryRef.current = retainedMeasurementBoundary;
  const activeDetailAnchor = detailAnchorRef.current?.scope === scrollScope
    ? detailAnchorRef.current : null;
  // Read the epoch so pointer interaction changes synchronously reconfigure
  // the virtualizer even when no other chat state changed.
  void scrollPolicyEpoch;
  const virtualScrollPolicy = scrollCoordinatorRef.current.policy(
    scrollState.followOutput,
  );
  const virtualAnchorTo = keyedPrependActive
      && activeHistoryAnchor?.direction === "newer"
    ? "end" : "start";
  const virtualizer = useVirtualizer({
    count: turns.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => HISTORY_VIRTUAL_ESTIMATE_PX,
    getItemKey: turnKeySnapshot.getItemKey,
    // In-flow rows let the browser own prepend/resize anchoring. A second keyed
    // virtual-core anchor can apply after touchend and undo the browser's
    // correction, so history always uses a start anchor here. Live-tail follow
    // remains owned by ChatView's measured-tail observer below.
    anchorTo: virtualAnchorTo,
    followOnAppend: virtualScrollPolicy.followOnAppend,
    scrollEndThreshold: 80,
    overscan: HISTORY_VIRTUAL_OVERSCAN,
    rangeExtractor: (range) => {
      const indexes = defaultRangeExtractor(range);
      let first = indexes[0] ?? 0;
      let last = indexes[indexes.length - 1] ?? 0;
      const retain = (index: number) => {
        if (index < 0) return;
        first = Math.min(first, index);
        last = Math.max(last, index);
      };
      retain(retainedMeasurementBoundary
        ? turns.findIndex((turn) => turn.id === retainedMeasurementBoundary.turnId)
        : -1);
      retain(activeDetailAnchor
        ? turns.findIndex((turn) => turn.id === activeDetailAnchor.turnId)
        : -1);
      if (activeTextSelection) {
        retain(turns.findIndex(
          (turn) => turn.id === activeTextSelection.anchorTurnId));
        retain(turns.findIndex(
          (turn) => turn.id === activeTextSelection.focusTurnId));
      }
      // Normal-flow virtual rows must be contiguous. During the short bounded
      // history/detail/selection transaction, keep the complete span to the
      // retained row mounted: clipping that span can remove the only native
      // anchor exactly when a page is prepended. Transactions already have
      // explicit release/timeout paths, so this does not permanently expand
      // the resident DOM.
      const span: number[] = [];
      for (let index = first; index <= last; index += 1) span.push(index);
      return span;
    },
    gap: HISTORY_TURN_GAP_PX,
    paddingStart: historyTopInset,
    paddingEnd: historyBottomInset,
    // ResizeObserver already runs before paint. Deferring dynamic-row
    // measurement through another animation frame exposes an estimate-only
    // prepend for one frame before the retained row can be restored.
    useAnimationFrameWithResizeObserver: false,
  });
  // CSS anchoring owns in-flow row growth. TanStack resize correction would be
  // a second scroll writer and can replay an old delta after native momentum.
  virtualizer.shouldAdjustScrollPositionOnItemSizeChange = () => false;

  const measureTurnOffset = useCallback((turnId: string): number | null => {
    const el = scrollRef.current;
    const node = turnNodeRefs.current.get(turnId);
    if (!el || !node) return null;
    return node.getBoundingClientRect().top - el.getBoundingClientRect().top;
  }, []);

  const restoreRetainedMeasurementBoundary = useCallback((): boolean => {
    const boundary = retainedMeasurementBoundaryRef.current;
    const el = scrollRef.current;
    const controller = controllerRef.current;
    if (!boundary || boundary.scope !== activeScrollScopeRef.current
        || !el || !controller
        || userScrollIntentRef.current
        || touchYRef.current !== null
        || wheelGestureActiveRef.current
        || scrollCoordinatorRef.current.isInteractionLocked()) return false;
    const currentOffset = measureTurnOffset(boundary.turnId);
    if (currentOffset == null) return false;
    const delta = currentOffset - boundary.anchorOffset;
    if (Math.abs(delta) <= 0.5) return false;
    const target = Math.max(
      0,
      Math.min(
        Math.max(0, el.scrollHeight - el.clientHeight),
        el.scrollTop + delta,
      ),
    );
    if (Math.abs(target - el.scrollTop) <= 0.5) return false;
    el.scrollTop = target;
    lastScrollTopRef.current = el.scrollTop;
    controller.recordProgrammaticScroll(readScrollMetrics(el));
    return true;
  }, [measureTurnOffset]);

  const measureDetailEdge = useCallback((
    turnId: string,
    edge: DetailAnchorEdge,
  ): number | null => {
    const el = scrollRef.current;
    const turnNode = turnNodeRefs.current.get(turnId);
    if (!el || !turnNode) return null;
    const processNode = turnNode.querySelector<HTMLElement>(
      "[data-process-detail-root]",
    ) ?? turnNode;
    const viewportRect = el.getBoundingClientRect();
    const processRect = processNode.getBoundingClientRect();
    return (edge === "end" ? processRect.bottom : processRect.top)
      - viewportRect.top;
  }, []);

  const firstTurnId = turns[0]?.id ?? null;
  const captureHistoryBoundary = useCallback(
    (): CapturedHistoryBoundary | null => {
      const el = scrollRef.current;
      const viewportTop = el?.getBoundingClientRect().top;
      let anchorTurnId: string | null = null;
      let anchorOffset = 0;
      let bestDistance = Number.POSITIVE_INFINITY;
      if (el && viewportTop != null) {
        for (const [turnId, node] of turnNodeRefs.current) {
          const rect = node.getBoundingClientRect();
          if (rect.bottom <= viewportTop
              || rect.top >= viewportTop + el.clientHeight) continue;
          const distance = Math.abs(rect.top - viewportTop);
          if (distance < bestDistance) {
            anchorTurnId = turnId;
            anchorOffset = rect.top - viewportTop;
            bestDistance = distance;
          }
        }
      }
      anchorTurnId ??= firstTurnId;
      if (!anchorTurnId) return null;
      anchorOffset = measureTurnOffset(anchorTurnId) ?? anchorOffset;
      return {
        anchorTurnId,
        oldestTurnId: firstTurnId,
        anchorOffset,
      };
    },
    [firstTurnId, measureTurnOffset],
  );

  const clearHistoryRequestTimeout = useCallback((generation?: number) => {
    const pending = historyRequestTimeoutRef.current;
    if (!pending || (generation != null
        && pending.generation !== generation)) return;
    window.clearTimeout(pending.timer);
    historyRequestTimeoutRef.current = null;
  }, []);

  const cancelHistoryAnchor = useCallback((generation?: number): boolean => {
    const activeGeneration =
      historyAnchorRef.current.current()?.generation ?? null;
    const cancelled = historyAnchorRef.current.cancel(generation);
    if (!cancelled) return false;
    if (touchHistoryGenerationRef.current === activeGeneration) {
      touchHistoryGenerationRef.current = null;
      touchTransactionBoundaryRef.current = null;
    }
    if (historyReleaseFrameRef.current !== null) {
      window.cancelAnimationFrame(historyReleaseFrameRef.current);
      historyReleaseFrameRef.current = null;
    }
    setActiveHistoryGeneration(null);
    return cancelled;
  }, []);

  const syncScrollState = useCallback((next: ScrollFollowSnapshot) => {
    setScrollState((previous) =>
      previous.followOutput === next.followOutput && previous.nearBottom === next.nearBottom
        ? previous
        : next);
  }, []);

  const applyScrollCommand = useCallback((command: ScrollCommand | null) => {
    if (!command) return;
    const el = scrollRef.current;
    const controller = controllerRef.current;
    if (!el || !controller) return;
    if (command.kind === "bottom") {
      virtualizer.scrollToEnd({ behavior: command.behavior });
    } else {
      virtualizer.scrollToOffset(command.offset, { behavior: "auto" });
    }
    lastScrollTopRef.current = el.scrollTop;
    syncScrollState(controller.recordProgrammaticScroll(readScrollMetrics(el)));
  }, [syncScrollState, virtualizer]);

  const maintainFollowedLiveTail = useCallback(() => {
    const el = scrollRef.current;
    const controller = controllerRef.current;
    if (!el || !controller
        || activeScrollScopeRef.current !== scrollScope
        || renderedScrollScopeRef.current !== scrollScope
        || browseMode
        || !controller.isFollowing()
        || userScrollIntentRef.current
        || touchYRef.current !== null
        || wheelGestureActiveRef.current
        || textSelectionRef.current !== null
        || historyAnchorRef.current.current() !== null
        || scrollCoordinatorRef.current.isInteractionLocked()) return;
    if (measureBottom(readScrollMetrics(el)).distance <= 0.5) return;
    applyScrollCommand(
      scrollCoordinatorRef.current.requestBottom("auto"),
    );
  }, [applyScrollCommand, browseMode, scrollScope]);

  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    let previousHeight = el.clientHeight;
    const observer = new ResizeObserver(() => {
      const nextHeight = el.clientHeight;
      if (Math.abs(nextHeight - previousHeight) < 0.5) return;
      previousHeight = nextHeight;
      // Composer actions and the mobile keyboard resize the thread from
      // outside the virtual list. ResizeObserver runs before paint, so retain
      // the live-tail intent here instead of exposing one wrong-height frame.
      maintainFollowedLiveTail();
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [maintainFollowedLiveTail, scrollScope]);

  useLayoutEffect(() => {
    const content = contentSizerRef.current;
    if (!content || typeof ResizeObserver === "undefined") return;
    const followMeasuredTail = () => {
      // TanStack updates the in-flow leading spacer from ResizeObserver
      // measurements. Correct the retained reading row in this same
      // pre-paint notification so the spacer update never becomes a visible
      // intermediate jump.
      restoreRetainedMeasurementBoundary();
      if (!hasOpenTailRef.current) return;
      maintainFollowedLiveTail();
    };
    const observer = new ResizeObserver(followMeasuredTail);
    observer.observe(content);
    // WebKit can commit TanStack's in-flow spacer mutation after its current
    // ResizeObserver delivery, leaving one painted estimate-height gap before
    // the next delivery. DOM mutations run at the pre-paint microtask boundary,
    // so observe the actual streamed text/tool/spacer commit as well. The same
    // strict follow/gesture/selection/transaction gates above still own whether
    // a bottom command is allowed.
    const mutationObserver = !hasOpenTail
        || typeof MutationObserver === "undefined"
      ? null
      : new MutationObserver(followMeasuredTail);
    mutationObserver?.observe(content, {
      childList: true,
      characterData: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["style"],
    });
    // Session/revision switches request bottom before every virtual row has
    // necessarily been measured. Re-assert the same follow intent on the next
    // frame after the replacement sizer is mounted.
    followMeasuredTail();
    return () => {
      observer.disconnect();
      mutationObserver?.disconnect();
    };
  }, [
    hasOpenTail, maintainFollowedLiveTail,
    restoreRetainedMeasurementBoundary, scrollScope,
  ]);

  const pauseOutputFollow = useCallback(() => {
    const el = scrollRef.current;
    const controller = controllerRef.current;
    if (!el || !controller) return;
    syncScrollState(controller.pause(readScrollMetrics(el)));
  }, [syncScrollState]);

  const publishTextSelection = useCallback((
    selection: TextSelectionRetention | null,
  ) => {
    if (!onTextSelectionGuardChange) return;
    if (!selection || selection.scope !== scrollScope || !sid) {
      onTextSelectionGuardChange(null);
      return;
    }
    onTextSelectionGuardChange({
      sid,
      revision: historyRevision,
      viewId: resolvedHistoryViewId,
      scopeKey: historyScopeKey,
      turnIds: [selection.anchorTurnId, selection.focusTurnId],
    });
  }, [
    historyRevision, historyScopeKey, onTextSelectionGuardChange,
    resolvedHistoryViewId, scrollScope, sid,
  ]);

  const commitTextSelection = useCallback((
    selection: TextSelectionRetention | null,
  ) => {
    textSelectionRef.current = selection;
    setTextSelection(selection);
    publishTextSelection(selection);
  }, [publishTextSelection]);

  const releaseTextSelectionInteraction = useCallback((
    pointerId?: number,
  ) => {
    const active = textSelectionRef.current;
    if (!active || !active.dragging
        || (pointerId != null && active.pointerId !== pointerId)) return;
    if (textSelectionReleaseFrameRef.current !== null) return;
    // Mouseup's native default action can finish one last auto-scroll / range
    // update. Keep ownership until that action has settled, then snapshot both
    // the selection and viewport together; its queued selectionchange is not
    // a new gesture and must not disable stationary resize protection.
    textSelectionReleaseFrameRef.current = window.requestAnimationFrame(() => {
      textSelectionReleaseFrameRef.current = null;
      const current = textSelectionRef.current;
      if (!current || current.interactionToken !== active.interactionToken) return;
      const releaseBoundary = captureHistoryBoundary();
      const native = window.getSelection();
      const valid = !!native && !native.isCollapsed && native.rangeCount > 0;
      nativeSelectionBoundaryRef.current = valid ? nativeSelectionBoundary(native) : null;
      if (current.interactionToken !== null) {
        // Finishing a text drag must never replay a bottom command queued while
        // the browser owned native selection auto-scroll.
        scrollCoordinatorRef.current.endInteraction(
          current.interactionToken, false,
        );
        setScrollPolicyEpoch((value) => value + 1);
      }
      commitTextSelection(valid ? {
        ...current,
        dragging: false,
        interactionToken: null,
        releaseAnchorTurnId: releaseBoundary?.anchorTurnId ?? null,
        releaseAnchorOffset: releaseBoundary?.anchorOffset ?? null,
      } : null);
    });
  }, [captureHistoryBoundary, commitTextSelection]);

  const releaseTextSelectionViewportAnchor = useCallback(() => {
    const active = textSelectionRef.current;
    if (!active || active.dragging
        || (active.releaseAnchorTurnId == null
          && active.releaseAnchorOffset == null)) return;
    // The release boundary only protects the viewport from late measurements
    // while the reader is stationary. Once a new scroll gesture starts, keep
    // the selected turns mounted but stop correcting back to the old viewport.
    commitTextSelection({
      ...active,
      releaseAnchorTurnId: null,
      releaseAnchorOffset: null,
    });
  }, [commitTextSelection]);

  const clearTextSelection = useCallback(() => {
    if (textSelectionReleaseFrameRef.current !== null) {
      window.cancelAnimationFrame(textSelectionReleaseFrameRef.current);
      textSelectionReleaseFrameRef.current = null;
    }
    textSelectionCandidateRef.current = null;
    nativeSelectionBoundaryRef.current = null;
    const active = textSelectionRef.current;
    if (active?.interactionToken != null) {
      scrollCoordinatorRef.current.endInteraction(
        active.interactionToken, false,
      );
      setScrollPolicyEpoch((value) => value + 1);
    }
    if (active) {
      commitTextSelection(null);
    } else {
      publishTextSelection(null);
    }
  }, [commitTextSelection, publishTextSelection]);

  const disposeTextSelection = useCallback(() => {
    if (textSelectionReleaseFrameRef.current !== null) {
      window.cancelAnimationFrame(textSelectionReleaseFrameRef.current);
      textSelectionReleaseFrameRef.current = null;
    }
    const selection = textSelectionRef.current;
    if (selection?.interactionToken != null) {
      scrollCoordinatorRef.current.endInteraction(
        selection.interactionToken, false,
      );
    }
    textSelectionRef.current = null;
    textSelectionCandidateRef.current = null;
    nativeSelectionBoundaryRef.current = null;
    onTextSelectionGuardChange?.(null);
  }, [onTextSelectionGuardChange]);

  const observeNativeTextSelection = useCallback(() => {
    const nativeSelection = window.getSelection();
    const candidate = textSelectionCandidateRef.current;
    const active = textSelectionRef.current;
    if (!nativeSelection || nativeSelection.isCollapsed
        || nativeSelection.rangeCount === 0) {
      // Native edge selection can transiently expose an empty range while
      // the mouse remains held. Release on the real pointer boundary instead.
      if (active && !active.dragging) clearTextSelection();
      return;
    }
    const expectedScope = active?.scope ?? candidate?.scope ?? scrollScope;
    if (expectedScope !== scrollScope) {
      clearTextSelection();
      return;
    }
    const root = scrollRef.current;
    const anchorTurnId = selectionTurnId(root, nativeSelection.anchorNode);
    const focusTurnId = selectionTurnId(root, nativeSelection.focusNode);
    if (!anchorTurnId || !focusTurnId) {
      // While the mouse is held just outside the scrollport Chromium may put
      // the focus in the surrounding document for one selectionchange. Keep
      // the last valid in-thread boundary until the next in-thread move.
      if (!active || !active.dragging) clearTextSelection();
      return;
    }
    const previous = nativeSelectionBoundaryRef.current;
    const boundary = nativeSelectionBoundary(nativeSelection);
    nativeSelectionBoundaryRef.current = boundary;
    if (active) {
      if (previous && previous.anchor === boundary.anchor
          && previous.anchorOffset === boundary.anchorOffset
          && previous.focus === boundary.focus
          && previous.focusOffset === boundary.focusOffset) return;
      // Native selection handles and keyboard extension can move without a
      // pointerdown/wheel reaching this element, including within ONE turn.
      // Yield the old stationary viewport anchor before native auto-scroll.
      if (!active.dragging) selectionScrollIntentFnRef.current?.();
      if (active.anchorTurnId === anchorTurnId && active.focusTurnId === focusTurnId) return;
      commitTextSelection({
        ...textSelectionRef.current!,
        anchorTurnId,
        focusTurnId,
      });
      return;
    }
    if (!sid) return;
    cancelDetailAnchorFnRef.current?.();
    setMeasurementBoundary(null);
    pauseOutputFollow();
    // Touch handles / a selectionchange delivered after mouseup have no
    // pressed mouse candidate. Retain their DOM without a never-ending lock.
    const interactionToken = candidate
      ? scrollCoordinatorRef.current.beginInteraction(false) : null;
    if (!candidate) selectionScrollIntentFnRef.current?.();
    const selection: TextSelectionRetention = {
      scope: scrollScope,
      anchorTurnId,
      focusTurnId,
      pointerId: candidate?.pointerId ?? null,
      interactionToken,
      dragging: !!candidate,
      releaseAnchorTurnId: null,
      releaseAnchorOffset: null,
    };
    commitTextSelection(selection);
    setScrollPolicyEpoch((value) => value + 1);
  }, [
    clearTextSelection, commitTextSelection, pauseOutputFollow, scrollScope,
    sid,
  ]);

  useEffect(() => {
    const handlePointerEnd = (event: globalThis.PointerEvent) => {
      if (scrollbarDragIntentRef.current?.pointerId === event.pointerId) {
        scrollbarDragIntentRef.current = null;
      }
      const candidate = textSelectionCandidateRef.current;
      if (candidate?.pointerId === event.pointerId) {
        textSelectionCandidateRef.current = null;
      }
      releaseTextSelectionInteraction(event.pointerId);
    };
    const handleDocumentPointerDown = (event: globalThis.PointerEvent) => {
      const active = textSelectionRef.current;
      if (!active || active.dragging || event.button !== 0) return;
      const target = event.target;
      if (target instanceof Node && !scrollRef.current?.contains(target)) {
        clearTextSelection();
      }
    };
    const handleCopy = () => {
      if (!textSelectionRef.current) return;
      window.requestAnimationFrame(() => clearTextSelection());
    };
    document.addEventListener("selectionchange", observeNativeTextSelection);
    document.addEventListener("pointerup", handlePointerEnd);
    document.addEventListener("pointercancel", handlePointerEnd);
    document.addEventListener("pointerdown", handleDocumentPointerDown, true);
    document.addEventListener("copy", handleCopy);
    return () => {
      document.removeEventListener(
        "selectionchange", observeNativeTextSelection,
      );
      document.removeEventListener("pointerup", handlePointerEnd);
      document.removeEventListener("pointercancel", handlePointerEnd);
      document.removeEventListener(
        "pointerdown", handleDocumentPointerDown, true,
      );
      document.removeEventListener("copy", handleCopy);
    };
  }, [
    clearTextSelection, observeNativeTextSelection,
    releaseTextSelectionInteraction,
  ]);

  const completeHistoryLoadGates = useCallback(() => {
    historyLoadGateRef.current.complete();
    wheelHistoryLoadGateRef.current.complete();
  }, []);

  const scheduleHistoryAnchorRelease = useCallback((generation: number) => {
    if (userScrollIntentRef.current
        || touchYRef.current !== null
        || wheelGestureActiveRef.current
        || scrollCoordinatorRef.current.isInteractionLocked()) return;
    if (historyReleaseFrameRef.current !== null) {
      window.cancelAnimationFrame(historyReleaseFrameRef.current);
    }
    const releaseAfterLayout = () => {
      historyReleaseFrameRef.current = null;
      const anchor = historyAnchorRef.current.current();
      if (!anchor || anchor.generation !== generation) return;
      if (userScrollIntentRef.current
          || touchYRef.current !== null
          || wheelGestureActiveRef.current
          || scrollCoordinatorRef.current.isInteractionLocked()) {
        // The matching gesture/interaction release schedules this again.
        return;
      }
      cancelHistoryAnchor(generation);
      completeHistoryLoadGates();
    };
    historyReleaseFrameRef.current =
      window.requestAnimationFrame(releaseAfterLayout);
  }, [cancelHistoryAnchor, completeHistoryLoadGates]);

  // Freeze one retained row before either asynchronous window mutation.
  // Cached-newer paging may append at the tail and evict rows at the head, so
  // it deliberately uses the same keyed measurement transaction as prepend.
  const doLoadPage = (direction: HistoryPageDirection): boolean => {
    if (historyAnchorRef.current.current() || historyRequestRef.current) {
      return false;
    }
    const el = scrollRef.current;
    const point = el ? captureHistoryBoundary() : null;
    const sourcePresentation = scopedPresentedHistory;
    if (el) pauseOutputFollow();
    const loadResult = direction === "older"
      ? onLoadMore?.(point?.anchorTurnId)
      : onLoadNewer?.(point?.anchorTurnId);
    const accepted = loadResult === true
      || (typeof loadResult === "object" && loadResult.accepted);
    const acceptance = typeof loadResult === "object" ? loadResult : null;
    const requestViewId = acceptance
      ? acceptance.viewId ?? resolvedHistoryViewId
      : resolvedHistoryViewId;
    if (!accepted) {
      clearHistoryRequestTimeout();
      completeHistoryLoadGates();
      return false;
    }
    const requestGeneration = acceptance
        && Object.prototype.hasOwnProperty.call(acceptance, "generation")
      ? acceptance.generation ?? null
      : historyGeneration;
    const requestScopeKey = acceptance?.scopeKey ?? historyScopeKey ?? "";
    const requestAuthorityScope = [
      requestScopeKey,
      sid ?? "",
      historyRevision ?? "",
      requestGeneration ?? "",
    ].join("\u0000");
    const requestScrollScope = acceptance?.viewId != null
      ? [
          requestScopeKey,
          sid ?? "",
          historyRevision ?? "",
          requestViewId,
        ].join("\u0000")
      : scrollScope;
    const activity = beginHistoryPageActivity({
      direction,
    });
    const request: HistoryPageRequestTransaction = {
      sid,
      revision: historyRevision,
      authorityScope: requestAuthorityScope,
      viewId: requestViewId,
      direction,
      before: direction === "older" ? historyCursor : null,
      windowEpoch: historyWindowEpoch,
      activityKey: activity.key,
    };
    historyRequestRef.current = request;
    if (historyViewportLeaseRef.current
        && direction === "older"
        && acceptance?.viewId != null
        && requestScrollScope !== scrollScope
        && sourcePresentation.scope === scrollScope
        && sourcePresentation.authorityScope
          === incomingHistoryPresentation.authorityScope
        && sourcePresentation.cursor === historyCursor
        && sourcePresentation.windowEpoch === historyWindowEpoch) {
      historyViewportTransitionRef.current = {
        source: sourcePresentation,
        request,
        presentation: {
          ...sourcePresentation,
          scope: requestScrollScope,
          authorityScope: requestAuthorityScope,
          generation: requestGeneration,
          browseMode: true,
          hasNewer: false,
        },
      };
    } else {
      historyViewportTransitionRef.current = null;
    }
    let generation: number | null = null;
    if (point) {
      setMeasurementBoundary({
        scope: requestScrollScope,
        sid,
        revision: historyRevision,
        viewId: requestViewId,
        turnId: point.anchorTurnId,
        anchorOffset: point.anchorOffset,
      });
      generation = historyAnchorRef.current.begin({
        sid, revision: historyRevision, viewId: requestViewId,
        before: direction === "older" ? historyCursor : null,
        windowEpoch: historyWindowEpoch,
        direction,
        source: direction === "older" ? "server" : "local",
        anchorTurnId: point.anchorTurnId,
        oldestTurnId: point.oldestTurnId,
        anchorOffset: point.anchorOffset,
      });
      if (touchYRef.current !== null) {
        touchHistoryGenerationRef.current = generation;
        const clockOffset = touchEventClockOffsetRef.current;
        touchTransactionBoundaryRef.current = {
          generation,
          captureEventTimestamp: clockOffset == null
            ? Number.POSITIVE_INFINITY
            : window.performance.now() - clockOffset,
          baselineY: touchYRef.current,
          movedAfterCapture: false,
        };
      } else {
        touchHistoryGenerationRef.current = null;
        touchTransactionBoundaryRef.current = null;
      }
      setActiveHistoryGeneration(generation);
    }
    clearHistoryRequestTimeout();
    const timeoutGeneration = generation;
    const timer = window.setTimeout(() => {
      const pending = historyRequestTimeoutRef.current;
      if (!pending || pending.timer !== timer
          || pending.activityKey !== activity.key) return;
      historyRequestTimeoutRef.current = null;
      historyRequestRef.current = null;
      if (timeoutGeneration != null) cancelHistoryAnchor(timeoutGeneration);
      completeHistoryPageActivity(activity.key);
      completeHistoryLoadGates();
    }, HISTORY_PAGE_REQUEST_TIMEOUT_MS);
    historyRequestTimeoutRef.current = {
      generation: timeoutGeneration,
      activityKey: activity.key,
      timer,
    };
    return true;
  };
  const doLoadMore = (): boolean => doLoadPage("older");
  const doLoadNewer = (): boolean => doLoadPage("newer");
  const doLoadMoreRef = useRef(doLoadMore);
  doLoadMoreRef.current = doLoadMore;

  const stageDeferredOlderIntent = useCallback((
    source: "touch" | "wheel" | "other",
  ): boolean => {
    const gestureGate = source === "touch"
      ? historyLoadGateRef.current
      : source === "wheel" ? wheelHistoryLoadGateRef.current : null;
    if (gestureGate && !gestureGate.acquire()) return false;
    deferredOlderIntentRef.current = {
      authorityScope: incomingHistoryPresentation.authorityScope,
      viewId: resolvedHistoryViewId,
      cursor: incomingHistoryPresentation.cursor,
    };
    return true;
  }, [
    incomingHistoryPresentation.authorityScope,
    incomingHistoryPresentation.cursor,
    resolvedHistoryViewId,
  ]);

  useLayoutEffect(() => {
    const pending = deferredOlderIntentRef.current;
    const currentAuthority = incomingHistoryPresentation.authorityScope;
    if (pending && (pending.authorityScope !== currentAuthority
        || pending.viewId !== resolvedHistoryViewId)) {
      deferredOlderIntentRef.current = null;
      completeHistoryLoadGates();
    }
    if (olderHistoryAvailability === "unavailable") {
      if (deferredOlderIntentRef.current) {
        deferredOlderIntentRef.current = null;
        completeHistoryLoadGates();
      }
      return;
    }
    if (pending && pending.cursor !== incomingHistoryPresentation.cursor) {
      pending.cursor = incomingHistoryPresentation.cursor;
    }
    const el = scrollRef.current;
    const source = touchYRef.current !== null
      ? "touch" : wheelGestureActiveRef.current ? "wheel" : null;
    if (olderHistoryAvailability === "pending"
        && !deferredOlderIntentRef.current
        && source
        && userScrollIntentRef.current
        && userScrollDirectionRef.current === "history"
        && el && isAtHistoryEdge(readScrollMetrics(el))) {
      stageDeferredOlderIntent(source);
      return;
    }
    const intent = deferredOlderIntentRef.current;
    if (olderHistoryAvailability !== "ready" || !intent
        || intent.authorityScope !== currentAuthority
        || intent.viewId !== resolvedHistoryViewId
        || intent.cursor !== historyCursor) return;
    deferredOlderIntentRef.current = null;
    if (!doLoadMoreRef.current()) completeHistoryLoadGates();
  }, [
    completeHistoryLoadGates, historyCursor,
    incomingHistoryPresentation.authorityScope,
    incomingHistoryPresentation.cursor,
    olderHistoryAvailability, resolvedHistoryViewId,
    stageDeferredOlderIntent,
  ]);

  // Scroll/touch events can repeat many times while a finger or wheel remains
  // pinned at the top. Touch/wheel gates allow one request per gesture; plain
  // scroll/keyboard events additionally use the visible boundary as their gate.
  const maybeAutoLoadOlder = (
    movingTowardHistory: boolean,
    source: "touch" | "wheel" | "other",
  ) => {
    const el = scrollRef.current;
    if (!el || !movingTowardHistory
        || !isAtHistoryEdge(readScrollMetrics(el))) return;
    if (olderHistoryAvailability === "pending") {
      stageDeferredOlderIntent(source);
      return;
    }
    if (!shouldAutoLoadOlderHistory(
      readScrollMetrics(el), true, canLoadOlder,
    )) return;
    const boundary = [
      "older", incomingHistoryPresentation.authorityScope,
      sid ?? "", resolvedHistoryViewId, turns[0]?.id ?? "",
      turns.length, historyRevision ?? "", historyCursor ?? "", hasMore ? 1 : 0,
    ].join("\u0000");
    if (source === "other" && autoLoadedBoundaryRef.current === boundary) return;
    const gestureGate = source === "touch"
      ? historyLoadGateRef.current
      : source === "wheel" ? wheelHistoryLoadGateRef.current : null;
    if (gestureGate && !gestureGate.acquire()) return;
    if (doLoadMore()) {
      autoLoadedBoundaryRef.current = boundary;
    } else {
      gestureGate?.complete();
    }
  };

  const maybeAutoLoadNewer = (
    movingTowardLatest: boolean,
    source: "touch" | "wheel" | "other",
  ) => {
    const el = scrollRef.current;
    if (!el || !shouldAutoLoadNewerHistory(
      readScrollMetrics(el), movingTowardLatest, canLoadNewer,
    )) return;
    const boundary = [
      "newer", incomingHistoryPresentation.authorityScope,
      sid ?? "", resolvedHistoryViewId,
      turns.at(-1)?.id ?? "", turns.length,
      historyRevision ?? "", historyWindowEpoch, hasNewer ? 1 : 0,
    ].join("\u0000");
    if (source === "other" && autoLoadedBoundaryRef.current === boundary) return;
    const gestureGate = source === "touch"
      ? historyLoadGateRef.current
      : source === "wheel" ? wheelHistoryLoadGateRef.current : null;
    if (gestureGate && !gestureGate.acquire()) return;
    if (doLoadNewer()) {
      autoLoadedBoundaryRef.current = boundary;
    } else {
      gestureGate?.complete();
    }
  };

  // WebKit does not reliably implement CSS scroll anchoring for a virtualized
  // prepend. Restore the keyed reading row synchronously in the same layout
  // commit, before the browser can paint the inserted page. This is a bounded
  // residual correction, not a frame loop: later renders write only when a
  // real row measurement changed the retained offset.
  useLayoutEffect(() => {
    restoreRetainedMeasurementBoundary();
  });

  useLayoutEffect(() => {
    const request = historyRequestRef.current;
    const requestScopeChanged = request && (
      request.sid !== sid
      || request.revision !== historyRevision
      || request.authorityScope
        !== incomingHistoryPresentation.authorityScope
      || request.viewId !== resolvedHistoryViewId
    );
    const requestCompleted = request && (
      request.direction === "older"
        ? request.before !== historyCursor || !hasMore
        : request.windowEpoch !== historyWindowEpoch || !canLoadNewer
    );
    if (requestScopeChanged || requestCompleted) {
      historyRequestRef.current = null;
      clearHistoryRequestTimeout();
      completeHistoryPageActivity(request?.activityKey);
    }
    const anchor = historyAnchorRef.current.current();
    if (!anchor) {
      if (requestScopeChanged || requestCompleted) completeHistoryLoadGates();
      return;
    }
    if (requestScopeChanged) {
      // Authority/generation are not fields on the physical row anchor. Once
      // the accepted request loses that authority, its timeout and activity
      // are already gone above, so retaining the anchor would block every
      // subsequent page forever with no self-healing path.
      cancelHistoryAnchor(anchor.generation);
      completeHistoryLoadGates();
      return;
    }
    if (anchor.sid !== sid || anchor.revision !== historyRevision
        || (anchor.viewId ?? null) !== resolvedHistoryViewId) {
      cancelHistoryAnchor(anchor.generation);
      completeHistoryLoadGates();
      return;
    }
    if (anchor.phase === "applied") {
      scheduleHistoryAnchorRelease(anchor.generation);
      return;
    }
    if (anchor.phase !== "pending") return;
    const pageStatus = historyPageStatus(anchor, {
      sid, revision: historyRevision, cursor: historyCursor,
      viewId: resolvedHistoryViewId, hasMore: !!hasMore,
      windowEpoch: historyWindowEpoch, hasNewer: canLoadNewer,
    });
    if (pageStatus === "pending") return;
    if (pageStatus === "stale") {
      const activityKey = historyRequestRef.current?.activityKey;
      historyRequestRef.current = null;
      clearHistoryRequestTimeout();
      completeHistoryPageActivity(activityKey);
      cancelHistoryAnchor(anchor.generation);
      completeHistoryLoadGates();
      return;
    }

    if (!turns.some((turn) => turn.id === anchor.anchorTurnId)) {
      // The bounded projection no longer contains the reading row. Never
      // manufacture a viewport movement from cursor metadata alone.
      const activityKey = historyRequestRef.current?.activityKey;
      historyRequestRef.current = null;
      clearHistoryRequestTimeout();
      completeHistoryPageActivity(activityKey);
      cancelHistoryAnchor(anchor.generation);
      completeHistoryLoadGates();
      return;
    }
    if (historyAnchorRef.current.markRendering(anchor.generation)) {
      if (historyAnchorRef.current.markApplied(anchor.generation)
          && touchHistoryGenerationRef.current === anchor.generation
          && touchYRef.current !== null
          && touchTransactionBoundaryRef.current?.generation
            !== anchor.generation) {
        const clockOffset = touchEventClockOffsetRef.current;
        touchTransactionBoundaryRef.current = {
          generation: anchor.generation,
          captureEventTimestamp: clockOffset == null
            ? Number.POSITIVE_INFINITY
            : window.performance.now() - clockOffset,
          baselineY: touchYRef.current,
          movedAfterCapture: false,
        };
      }
      scheduleHistoryAnchorRelease(anchor.generation);
    }
  }, [
    cancelHistoryAnchor, canLoadNewer, clearHistoryRequestTimeout,
    completeHistoryLoadGates, completeHistoryPageActivity, hasMore,
    historyCursor, historyRevision, historyWindowEpoch,
    incomingHistoryPresentation.authorityScope, resolvedHistoryViewId,
    scheduleHistoryAnchorRelease, sid, turns,
  ]);

  useLayoutEffect(() => {
    const el = scrollRef.current;
    const controller = controllerRef.current;
    if (!el || !controller) return;

    // Initial mount, session switches, and authoritative revision replacements
    // are anchored synchronously before paint. Commit the scope only once the
    // replacement thread exists; an intermediate empty projection has no
    // viewport on which the reset command can be consumed.
    if (renderedScrollScopeRef.current !== scrollScope) {
      renderedScrollScopeRef.current = scrollScope;
      textSelectionCandidateRef.current = null;
      if (textSelectionRef.current) clearTextSelection();
      const request = historyRequestRef.current;
      const requestActivityKey = request?.activityKey;
      const anchor = historyAnchorRef.current.current();
      const enteringBrowse = browseMode
        && request?.direction === "older"
        && request.sid === sid
        && request.revision === historyRevision
        && request.viewId === resolvedHistoryViewId
        && anchor?.generation === activeHistoryGeneration
        && anchor.sid === sid
        && anchor.revision === historyRevision
        && anchor.viewId === resolvedHistoryViewId;
      if (enteringBrowse) {
        syncScrollState(controller.pause(readScrollMetrics(el)));
        return;
      }
      cancelHistoryAnchor();
      clearHistoryRequestTimeout();
      touchYRef.current = null;
      touchTransactionBoundaryRef.current = null;
      touchEventClockOffsetRef.current = null;
      userScrollIntentRef.current = false;
      userScrollDirectionRef.current = null;
      scrollbarDragIntentRef.current = null;
      manualLatestIntentRef.current = null;
      if (userScrollIntentTimerRef.current !== null) {
        window.clearTimeout(userScrollIntentTimerRef.current);
        userScrollIntentTimerRef.current = null;
      }
      if (wheelGestureTimerRef.current !== null) {
        window.clearTimeout(wheelGestureTimerRef.current);
        wheelGestureTimerRef.current = null;
      }
      wheelGestureActiveRef.current = false;
      historyRequestRef.current = null;
      completeHistoryPageActivity(requestActivityKey);
      autoLoadedBoundaryRef.current = null;
      historyLoadGateRef.current.complete();
      historyLoadGateRef.current.endGesture();
      wheelHistoryLoadGateRef.current.complete();
      wheelHistoryLoadGateRef.current.endGesture();
      setMeasurementBoundary(null);
      cancelDetailAnchorFnRef.current?.(false);
      applyScrollCommand(scrollCoordinatorRef.current.reset());
      syncScrollState(browseMode
        ? controller.pause(readScrollMetrics(el))
        : controller.reset(readScrollMetrics(el)));
      return;
    }

    if (browseMode && controller.isFollowing()) {
      syncScrollState(controller.pause(readScrollMetrics(el)));
      return;
    }
    if (!controller.isFollowing()) {
      syncScrollState(controller.observeLayout(readScrollMetrics(el)));
    }
  }, [
    activeHistoryGeneration, applyScrollCommand, browseMode, cancelHistoryAnchor,
    clearHistoryRequestTimeout, clearTextSelection,
    completeHistoryPageActivity, historyRevision,
    resolvedHistoryViewId,
    scrollScope, sid, syncScrollState, turns,
  ]);

  // React commits streamed text/tool rows before paint. Keep the followed tail
  // physically pinned in this same commit; ResizeObserver above covers later
  // font/image/virtualizer measurements without introducing a frame-delayed
  // second writer.
  useLayoutEffect(() => {
    if (!hasOpenTailRef.current) return;
    maintainFollowedLiveTail();
  });

  useEffect(() => {
    return () => {
      cancelHistoryAnchor();
      clearHistoryRequestTimeout();
      if (wheelGestureTimerRef.current !== null) {
        window.clearTimeout(wheelGestureTimerRef.current);
      }
      if (userScrollIntentTimerRef.current !== null) {
        window.clearTimeout(userScrollIntentTimerRef.current);
      }
      disposeTextSelection();
      cancelDetailAnchorFnRef.current?.(false);
    };
  }, [
    cancelHistoryAnchor, clearHistoryRequestTimeout, disposeTextSelection,
  ]);

  useEffect(() => setZoom(null), [sid]);

  const settleUserScrollIntent = () => {
    if (touchYRef.current !== null || wheelGestureActiveRef.current) {
      if (userScrollIntentTimerRef.current !== null) {
        window.clearTimeout(userScrollIntentTimerRef.current);
      }
      userScrollIntentTimerRef.current = window.setTimeout(
        settleUserScrollIntent,
        USER_SCROLL_INTENT_IDLE_MS,
      );
      return;
    }
    touchMomentumActiveRef.current = false;
    if (userScrollIntentTimerRef.current !== null) {
      window.clearTimeout(userScrollIntentTimerRef.current);
      userScrollIntentTimerRef.current = null;
    }
    userScrollIntentRef.current = false;
    userScrollDirectionRef.current = null;
    flushHistoryViewportLease();
    // A page transaction owns an older exact row until its keyed correction
    // settles. Capturing the temporarily shifted DOM here would replace that
    // authority with the very jump we are trying to undo. Explicit movement
    // after attachment rebases the transaction in onScroll instead.
    const activeAnchor = historyAnchorRef.current.current();
    if (activeAnchor) {
      if (activeAnchor.phase === "applied") {
        scheduleHistoryAnchorRelease(activeAnchor.generation);
      }
      return;
    }
    const controller = controllerRef.current;
    const point = captureHistoryBoundary();
    const request = historyRequestRef.current;
    if (point && (!controller?.isFollowing()
        || (request?.sid === sid
          && request.revision === historyRevision
          && request.viewId === resolvedHistoryViewId))) {
      setMeasurementBoundary({
        scope: scrollScope,
        sid,
        revision: historyRevision,
        viewId: resolvedHistoryViewId,
        turnId: point.anchorTurnId,
        anchorOffset: point.anchorOffset,
      });
    }
  };

  const markUserScrollIntent = (direction: UserScrollDirection) => {
    // A real wheel/touch/key/pointer action transfers ownership back to the
    // reader. Late detail responses and ResizeObserver callbacks must not pull
    // the viewport back to the edge captured before that gesture. Keep the
    // prior measurement boundary until the first real scroll replaces it:
    // clearing it here leaves a gap in which a delayed row measurement can
    // move a viewport that the reader has already stopped.
    releaseTextSelectionViewportAnchor();
    cancelDetailAnchorFnRef.current?.();
    userScrollIntentRef.current = true;
    userScrollDirectionRef.current = direction;
    if (userScrollIntentTimerRef.current !== null) {
      window.clearTimeout(userScrollIntentTimerRef.current);
    }
    userScrollIntentTimerRef.current = window.setTimeout(
      settleUserScrollIntent,
      USER_SCROLL_INTENT_IDLE_MS,
    );
  };
  selectionScrollIntentFnRef.current = () => markUserScrollIntent("unknown");

  const leaveHistoryBrowse = useCallback((): boolean => {
    if (!browseMode || !onReturnLatest) return false;
    const requestActivityKey = historyRequestRef.current?.activityKey;
    scrollbarDragIntentRef.current = null;
    manualLatestIntentRef.current = null;
    cancelDetailAnchorFnRef.current?.();
    cancelHistoryAnchor();
    historyRequestRef.current = null;
    clearHistoryRequestTimeout();
    completeHistoryPageActivity(requestActivityKey);
    completeHistoryLoadGates();
    setMeasurementBoundary(null);
    onReturnLatest();
    return true;
  }, [
    browseMode, cancelHistoryAnchor, clearHistoryRequestTimeout,
    completeHistoryLoadGates, completeHistoryPageActivity, onReturnLatest,
  ]);

  const onThreadPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    markUserScrollIntent("unknown");
    scrollbarDragIntentRef.current = null;
    if (event.pointerType !== "mouse" || event.button !== 0
        || !event.isPrimary) {
      textSelectionCandidateRef.current = null;
      return;
    }
    const thread = event.currentTarget;
    const rect = thread.getBoundingClientRect();
    const paintedScrollbarWidth = Math.max(
      thread.offsetWidth - thread.clientWidth,
      SCROLLBAR_POINTER_GUTTER_PX,
    );
    const ownsVerticalScrollbar = event.target === thread
      && thread.scrollHeight > thread.clientHeight + 0.5
      && event.clientX >= rect.right - paintedScrollbarWidth
      && event.clientX <= rect.right;
    if (ownsVerticalScrollbar) {
      scrollbarDragIntentRef.current = {
        scope: scrollScope,
        pointerId: event.pointerId,
      };
      textSelectionCandidateRef.current = null;
      return;
    }
    if (textSelectionRef.current) clearTextSelection();
    const target = event.target instanceof Element ? event.target : null;
    const turn = target?.closest<HTMLElement>("[data-turn-id]");
    if (!target || !turn || target.closest(TEXT_SELECTION_EXCLUDED_SELECTOR)) {
      textSelectionCandidateRef.current = null;
      return;
    }
    textSelectionCandidateRef.current = {
      scope: scrollScope,
      pointerId: event.pointerId,
    };
  };

  const onScroll = () => {
    const el = scrollRef.current;
    const controller = controllerRef.current;
    if (!el || !controller) return;
    const metrics = readScrollMetrics(el);
    const movingTowardHistory = metrics.scrollTop < lastScrollTopRef.current - 0.5;
    const movingTowardLatest = metrics.scrollTop > lastScrollTopRef.current + 0.5;
    const movementDirection: UserScrollDirection | null = movingTowardHistory
      ? "history" : movingTowardLatest ? "latest" : null;
    const intendedDirection = userScrollDirectionRef.current;
    const textSelectionDragging =
      textSelectionRef.current?.scope === scrollScope
      && textSelectionRef.current.dragging;
    const currentHistoryAnchor = historyAnchorRef.current.current();
    const touchOwnsHistoryAnchor = currentHistoryAnchor != null
      && touchHistoryGenerationRef.current === currentHistoryAnchor.generation;
    const postTouchMomentum = movementDirection !== null
      && userScrollIntentRef.current
      && touchYRef.current === null
      && touchMomentumActiveRef.current;
    const explicitTransactionMovement = keyedPrependResponseReady
      && currentHistoryAnchor?.phase === "applied"
      && intendedDirection !== null
      && intendedDirection !== "unknown"
      // Mobile WebKit can deliver the scroll that reached the paging edge
      // only after the response has already rendered. For a touch-owned page,
      // only a touchmove observed after the request captured its transaction
      // boundary may replace the original retained row. clearTouch
      // synchronously captures that explicit move before clearing this marker.
      && (!touchOwnsHistoryAnchor
        || (touchTransactionBoundaryRef.current?.generation
            === currentHistoryAnchor.generation
          && touchTransactionBoundaryRef.current.movedAfterCapture));
    const verifiedTouchMovement = !touchOwnsHistoryAnchor
      || currentHistoryAnchor?.phase === "applied"
      || touchTransactionBoundaryRef.current?.movedAfterCapture === true;
    const userDrivenScroll = movementDirection !== null && (
      textSelectionDragging
      || postTouchMomentum
      || (userScrollIntentRef.current
        && (intendedDirection === "unknown"
          || intendedDirection === movementDirection)
        && verifiedTouchMovement
        && (!keyedPrependResponseReady || explicitTransactionMovement))
    );
    const userBoundary = userDrivenScroll ? captureHistoryBoundary() : null;
    if (userBoundary && !textSelectionDragging) {
      // Publish the DOM position reached by this native scroll before any
      // asynchronous measurement or page response can commit. The idle timer
      // refreshes it once more at the final resting position.
      setMeasurementBoundary({
        scope: scrollScope,
        sid,
        revision: historyRevision,
        viewId: resolvedHistoryViewId,
        turnId: userBoundary.anchorTurnId,
        anchorOffset: userBoundary.anchorOffset,
      });
    }
    if (userDrivenScroll && (postTouchMomentum || intendedDirection)) {
      markUserScrollIntent(postTouchMomentum
        ? movementDirection! : intendedDirection!);
    }
    if (userDrivenScroll && currentHistoryAnchor) {
      if (currentHistoryAnchor.phase === "applied") {
        if (userBoundary) historyAnchorRef.current.rebase(
          currentHistoryAnchor.generation,
          userBoundary,
        );
      } else if (postTouchMomentum) {
        if (userBoundary) historyAnchorRef.current.rebasePending(
          currentHistoryAnchor.generation,
          userBoundary,
        );
      } else if (touchOwnsHistoryAnchor
          && touchTransactionBoundaryRef.current?.movedAfterCapture) {
        if (userBoundary) historyAnchorRef.current.rebasePending(
          currentHistoryAnchor.generation,
          userBoundary,
        );
      } else {
        const stillAtRequestedEdge = currentHistoryAnchor.direction === "newer"
          ? isAtLatestEdge(metrics) : isAtHistoryEdge(metrics);
        historyAnchorRef.current.observeUserScroll(stillAtRequestedEdge);
        if (!historyAnchorRef.current.current()) {
          setActiveHistoryGeneration(null);
          completeHistoryLoadGates();
        }
      }
    }
    if (userDrivenScroll && movingTowardHistory) {
      manualLatestIntentRef.current = null;
    }
    const scrollbarReachedBrowseTail = browseMode
      && !!onReturnLatest
      && scrollbarDragIntentRef.current?.scope === scrollScope
      && movingTowardLatest
      && isAtLatestEdge(metrics);
    if (scrollbarReachedBrowseTail) {
      lastScrollTopRef.current = metrics.scrollTop;
      leaveHistoryBrowse();
      return;
    }
    const reachedBrowseTail = browseMode
      && !!onReturnLatest
      && movingTowardLatest
      && !textSelectionDragging
      && isAtLatestEdge(metrics)
      && userDrivenScroll;
    if (reachedBrowseTail) {
      if (userDrivenScroll) manualLatestIntentRef.current = scrollScope;
      if (!hasNewer
          && !historyRequestRef.current
          && !historyAnchorRef.current.current()) {
        lastScrollTopRef.current = metrics.scrollTop;
        leaveHistoryBrowse();
        return;
      }
    }
    lastScrollTopRef.current = metrics.scrollTop;
    const nextScrollState = userDrivenScroll
      ? controller.observeScroll(
        metrics, !browseMode && !textSelectionDragging,
      )
      : controller.recordProgrammaticScroll(metrics);
    if (nextScrollState.followOutput && !scrollState.followOutput
        && !historyRequestRef.current) {
      setMeasurementBoundary(null);
    }
    syncScrollState(nextScrollState);
    if (!textSelectionDragging) {
      maybeAutoLoadOlder(
        movingTowardHistory && userDrivenScroll,
        touchYRef.current != null ? "touch"
          : wheelGestureActiveRef.current ? "wheel" : "other",
      );
      maybeAutoLoadNewer(
        movingTowardLatest && userDrivenScroll,
        touchYRef.current != null ? "touch"
          : wheelGestureActiveRef.current ? "wheel" : "other",
      );
    }
  };

  useEffect(() => {
    const intentScope = manualLatestIntentRef.current;
    if (!browseMode || intentScope !== scrollScope) {
      manualLatestIntentRef.current = null;
      return;
    }
    // The final cached-newer response clears its keyed request/anchor in the
    // preceding layout effects. If the user's gesture already left the DOM at
    // the real bottom, complete the same action as the explicit button.
    if (hasNewer || historyRequestRef.current
        || historyAnchorRef.current.current()) return;
    const el = scrollRef.current;
    if (!el || !isAtLatestEdge(readScrollMetrics(el))
        || textSelectionRef.current?.dragging) return;
    leaveHistoryBrowse();
  }, [
    activeHistoryGeneration, browseMode, hasNewer, historyWindowEpoch,
    leaveHistoryBrowse, scrollScope, turns,
  ]);

  const onWheel = (event: WheelEvent<HTMLDivElement>) => {
    if (Math.abs(event.deltaY) <= 0.5) return;
    beginHistoryViewportLease();
    const direction: HistoryPageDirection =
      event.deltaY < 0 ? "older" : "newer";
    markUserScrollIntent(direction === "older" ? "history" : "latest");
    const anchor = historyAnchorRef.current.current();
    if (anchor?.direction && anchor.direction !== direction
        && (anchor.phase === "pending" || anchor.phase === "rendering")) {
      cancelHistoryAnchor(anchor.generation);
      completeHistoryLoadGates();
    }
    if (event.deltaY < 0 || browseMode) {
      pauseOutputFollow();
    }
    if (!wheelGestureActiveRef.current) {
      wheelGestureActiveRef.current = true;
      wheelHistoryLoadGateRef.current.beginGesture();
    }
    if (wheelGestureTimerRef.current !== null) {
      window.clearTimeout(wheelGestureTimerRef.current);
    }
    wheelGestureTimerRef.current = window.setTimeout(() => {
      wheelGestureTimerRef.current = null;
      wheelGestureActiveRef.current = false;
      wheelHistoryLoadGateRef.current.endGesture();
      // Wheel-idle is a stronger terminal signal than the generic intent
      // debounce. Publish the final resting row now so late measurements do
      // not fall into the remaining debounce window.
      settleUserScrollIntent();
      const current = historyAnchorRef.current.current();
      if (current?.phase === "applied") {
        scheduleHistoryAnchorRelease(current.generation);
      }
    }, WHEEL_GESTURE_IDLE_MS);
    const el = scrollRef.current;
    if (el) {
      const metrics = readScrollMetrics(el);
      if (direction === "older" && isAtHistoryEdge(metrics)) {
        maybeAutoLoadOlder(true, "wheel");
      } else if (direction === "newer" && isAtLatestEdge(metrics)) {
        maybeAutoLoadNewer(true, "wheel");
      }
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    // Native keyboard scrolling can update scrollTop before React receives the
    // preceding scroll event (and browsers may coalesce that event with the
    // next key's movement). Use the physical position at this input boundary
    // as the baseline so End -> Home still registers as historyward movement.
    const el = scrollRef.current;
    if (el) lastScrollTopRef.current = el.scrollTop;
    if (["ArrowUp", "PageUp", "Home"].includes(event.key)) {
      markUserScrollIntent("history");
    } else if (["ArrowDown", "PageDown", "End", " "].includes(event.key)) {
      markUserScrollIntent("latest");
    }
  };

  const onTouchStart = (event: TouchEvent<HTMLDivElement>) => {
    touchMomentumActiveRef.current = false;
    beginHistoryViewportLease();
    markUserScrollIntent("unknown");
    historyLoadGateRef.current.beginGesture();
    touchTransactionBoundaryRef.current = null;
    touchEventClockOffsetRef.current =
      window.performance.now() - event.timeStamp;
    touchYRef.current = event.touches[0]?.clientY ?? null;
  };

  const onTouchMove = (event: TouchEvent<HTMLDivElement>) => {
    const currentY = event.touches[0]?.clientY;
    const previousY = touchYRef.current;
    if (currentY == null || previousY == null) return;
    // Publish the newest finger position before a paging request can
    // synchronously capture its transaction boundary.
    touchYRef.current = currentY;
    // Movement after the request captured its boundary means the reader has
    // left that row, in EITHER direction. Continuing toward history is the
    // ordinary way a reader walks back through a long conversation; testing
    // this only on the toward-latest reversal left that gesture restoring the
    // pre-page row on release and pinning the viewport for the whole settle
    // window. The event-order and displacement checks distinguish new movement
    // from delayed delivery of a touchmove that caused the request.
    const transactionBoundary = touchTransactionBoundaryRef.current;
    const transactionAnchor = historyAnchorRef.current.current();
    if (transactionAnchor
        && transactionBoundary?.generation === transactionAnchor.generation
        && event.timeStamp > transactionBoundary.captureEventTimestamp
        && Math.abs(currentY - transactionBoundary.baselineY) > 0.5) {
      transactionBoundary.movedAfterCapture = true;
    }
    // A finger moving down scrolls the viewport toward earlier messages.
    if (currentY > previousY) {
      markUserScrollIntent("history");
      pauseOutputFollow();
      const anchor = historyAnchorRef.current.current();
      if (anchor?.direction === "newer"
          && (anchor.phase === "pending" || anchor.phase === "rendering")
          && touchHistoryGenerationRef.current !== anchor.generation) {
        cancelHistoryAnchor(anchor.generation);
        completeHistoryLoadGates();
      }
      const el = scrollRef.current;
      if (el && isAtHistoryEdge(readScrollMetrics(el))) {
        maybeAutoLoadOlder(true, "touch");
      }
    } else if (currentY < previousY) {
      markUserScrollIntent("latest");
      const anchor = historyAnchorRef.current.current();
      if (anchor?.direction !== "newer"
          && (anchor?.phase === "pending" || anchor?.phase === "rendering")
          && touchHistoryGenerationRef.current !== anchor.generation) {
        cancelHistoryAnchor(anchor.generation);
        completeHistoryLoadGates();
      }
      const el = scrollRef.current;
      if (el && isAtLatestEdge(readScrollMetrics(el))) {
        maybeAutoLoadNewer(true, "touch");
      }
    }
  };

  const rebaseTouchHistoryAnchor = () => {
    const anchor = historyAnchorRef.current.current();
    const point = anchor ? captureHistoryBoundary() : null;
    if (!anchor || !point) return;
    const rebased = anchor.phase === "applied"
      ? historyAnchorRef.current.rebase(anchor.generation, point)
      : anchor.phase === "pending"
        ? historyAnchorRef.current.rebasePending(anchor.generation, point)
        : false;
    if (!rebased) return;
    setMeasurementBoundary({
      scope: scrollScope,
      sid,
      revision: historyRevision,
      viewId: resolvedHistoryViewId,
      turnId: point.anchorTurnId,
      anchorOffset: point.anchorOffset,
    });
  };

  const clearTouch = () => {
    // Mobile WebKit may defer React's scroll event until touchend. Capture the
    // DOM's already-moved reading row before clearing the touch lock, otherwise
    // the residual prepend correction can restore the pre-gesture row first.
    if (touchTransactionBoundaryRef.current?.movedAfterCapture) {
      rebaseTouchHistoryAnchor();
    }
    touchTransactionBoundaryRef.current = null;
    touchEventClockOffsetRef.current = null;
    touchYRef.current = null;
    historyLoadGateRef.current.endGesture();
    // touchend only releases the finger. Mobile WebKit can keep scrolling for
    // several native frames afterwards, so restart the complete idle window
    // here instead of inheriting a timer armed by the last touchmove. Every
    // subsequent momentum scroll extends this same lease in onScroll.
    touchMomentumActiveRef.current = true;
    markUserScrollIntent(userScrollDirectionRef.current ?? "unknown");
    // A page can finish while the finger is still down. Re-render after the
    // native touch ends so the retained history transaction can correct and
    // release its exact reading boundary without fighting the gesture.
    setScrollPolicyEpoch((value) => value + 1);
    const anchor = historyAnchorRef.current.current();
    if (anchor?.phase === "applied") {
      scheduleHistoryAnchorRelease(anchor.generation);
    }
  };

  const scrollToBottom = () => {
    const el = scrollRef.current;
    const controller = controllerRef.current;
    if (!el || !controller) return;
    const requestActivityKey = historyRequestRef.current?.activityKey;
    cancelDetailAnchorFnRef.current?.();
    cancelHistoryAnchor();
    historyRequestRef.current = null;
    clearHistoryRequestTimeout();
    completeHistoryPageActivity(requestActivityKey);
    completeHistoryLoadGates();
    setMeasurementBoundary(null);
    syncScrollState(controller.resume(readScrollMetrics(el)));
    applyScrollCommand(
      scrollCoordinatorRef.current.requestBottom("smooth"),
    );
  };

  const returnToLatest = () => {
    if (leaveHistoryBrowse()) return;
    scrollToBottom();
  };

  const beginProcessInteraction = useCallback((): number => {
    const el = scrollRef.current;
    const resumeAtBottom = !!el
      && (controllerRef.current?.isFollowing() ?? false)
      && measureBottom(readScrollMetrics(el)).atBottom;
    const token = scrollCoordinatorRef.current.beginInteraction(resumeAtBottom);
    setScrollPolicyEpoch((value) => value + 1);
    return token;
  }, []);

  const endProcessInteraction = useCallback((
    token: number,
    followOutput?: boolean,
  ): void => {
    const command = scrollCoordinatorRef.current.endInteraction(
      token,
      followOutput ?? (controllerRef.current?.isFollowing() ?? false),
    );
    setScrollPolicyEpoch((value) => value + 1);
    if (command) {
      window.requestAnimationFrame(() => applyScrollCommand(command));
    }
  }, [applyScrollCommand]);

  const disposeDetailResources = useCallback((
    transaction: DetailAnchorTransaction,
  ): void => {
    if (transaction.requestTimer !== null) {
      window.clearTimeout(transaction.requestTimer);
      transaction.requestTimer = null;
    }
    if (transaction.quietTimer !== null) {
      window.clearTimeout(transaction.quietTimer);
      transaction.quietTimer = null;
    }
    if (transaction.maxSettleTimer !== null) {
      window.clearTimeout(transaction.maxSettleTimer);
      transaction.maxSettleTimer = null;
    }
    if (transaction.firstFrame !== null) {
      window.cancelAnimationFrame(transaction.firstFrame);
      transaction.firstFrame = null;
    }
    if (transaction.secondFrame !== null) {
      window.cancelAnimationFrame(transaction.secondFrame);
      transaction.secondFrame = null;
    }
    transaction.observer?.disconnect();
    transaction.observer = null;
    transaction.observedNode = null;
  }, []);

  const releaseDetailAnchor = useCallback((
    releaseInteraction = true,
  ): void => {
    const transaction = detailAnchorRef.current;
    if (!transaction) return;
    detailAnchorRef.current = null;
    disposeDetailResources(transaction);
    if (releaseInteraction) {
      endProcessInteraction(transaction.token);
    }
  }, [disposeDetailResources, endProcessInteraction]);
  cancelDetailAnchorFnRef.current = releaseDetailAnchor;

  const scheduleDetailCorrection = useCallback((
    transaction: DetailAnchorTransaction,
  ): void => {
    if (detailAnchorRef.current !== transaction) return;
    if (transaction.quietTimer !== null) {
      window.clearTimeout(transaction.quietTimer);
      transaction.quietTimer = null;
    }
    if (transaction.firstFrame !== null
        || transaction.secondFrame !== null) return;
    transaction.firstFrame = window.requestAnimationFrame(() => {
      transaction.firstFrame = null;
      if (detailAnchorRef.current !== transaction) return;
      transaction.secondFrame = window.requestAnimationFrame(() => {
        transaction.secondFrame = null;
        if (detailAnchorRef.current !== transaction) return;
        if (userScrollIntentRef.current || touchYRef.current !== null) {
          releaseDetailAnchor();
          return;
        }
        const el = scrollRef.current;
        const currentOffset = measureDetailEdge(
          transaction.turnId, transaction.edge,
        );
        if (el && currentOffset != null) {
          const delta = currentOffset - transaction.anchorOffset;
          if (Math.abs(delta) > 0.5) {
            applyScrollCommand(
              scrollCoordinatorRef.current.requestInteractionOffset(
                transaction.token,
                el.scrollTop + delta,
              ),
            );
          }
        }
        if (transaction.responseSettled) {
          transaction.quietTimer = window.setTimeout(() => {
            transaction.quietTimer = null;
            if (detailAnchorRef.current === transaction) {
              releaseDetailAnchor();
            }
          }, DETAIL_ANCHOR_QUIET_MS);
        }
      });
    });
  }, [applyScrollCommand, measureDetailEdge, releaseDetailAnchor]);

  const requestProcessDetail = useCallback((
    turnId: string,
    before: string | null | undefined,
    direction: DetailPageDirection,
    autoLoad = false,
  ): boolean => {
    if (!onLoadDetail) return false;
    const edge: DetailAnchorEdge = direction === "newer" ? "end" : "start";
    const anchorOffset = measureDetailEdge(turnId, edge);
    const turn = turns.find((candidate) => candidate.id === turnId);
    if (anchorOffset == null || !turn
        || !onLoadDetail(turnId, before, autoLoad)) {
      return false;
    }

    // The request has been accepted, so this is now an explicit reading
    // action. Freeze the exact process edge only after acceptance; rejected
    // clicks leave the previous follow intent untouched.
    pauseOutputFollow();
    const token = scrollCoordinatorRef.current.beginInteraction(false);
    const previous = detailAnchorRef.current;
    if (previous) {
      detailAnchorRef.current = null;
      disposeDetailResources(previous);
      // The new token already holds the viewport, so releasing the old token
      // cannot replay a pending bottom command between transactions.
      endProcessInteraction(previous.token);
    }
    const transaction: DetailAnchorTransaction = {
      scope: scrollScope,
      turnId,
      edge,
      anchorOffset,
      token,
      initialFingerprint: detailTurnFingerprint(turn),
      sawLoading: false,
      responseSettled: false,
      requestTimer: null,
      quietTimer: null,
      maxSettleTimer: null,
      firstFrame: null,
      secondFrame: null,
      observer: null,
      observedNode: null,
    };
    transaction.requestTimer = window.setTimeout(() => {
      if (detailAnchorRef.current === transaction) releaseDetailAnchor();
    }, HISTORY_DETAIL_REQUEST_TIMEOUT_MS + 1_000);
    detailAnchorRef.current = transaction;
    setScrollPolicyEpoch((value) => value + 1);
    return true;
  }, [
    disposeDetailResources, endProcessInteraction, measureDetailEdge,
    onLoadDetail, pauseOutputFollow, releaseDetailAnchor, scrollScope, turns,
  ]);

  useLayoutEffect(() => {
    const transaction = detailAnchorRef.current;
    if (!transaction || transaction.scope !== scrollScope) return;
    const turn = turns.find((candidate) => candidate.id === transaction.turnId);
    if (!turn) {
      releaseDetailAnchor();
      return;
    }
    if (turn.detailLoading) {
      transaction.sawLoading = true;
      transaction.responseSettled = false;
      if (transaction.quietTimer !== null) {
        window.clearTimeout(transaction.quietTimer);
        transaction.quietTimer = null;
      }
      if (transaction.maxSettleTimer !== null) {
        window.clearTimeout(transaction.maxSettleTimer);
        transaction.maxSettleTimer = null;
      }
      if (transaction.requestTimer !== null) {
        window.clearTimeout(transaction.requestTimer);
      }
      // Automatic "load the whole process" may span many bounded responses.
      // Each accepted next page refreshes this inactivity timeout.
      transaction.requestTimer = window.setTimeout(() => {
        if (detailAnchorRef.current === transaction) releaseDetailAnchor();
      }, HISTORY_DETAIL_REQUEST_TIMEOUT_MS + 1_000);
      return;
    }
    const fingerprint = detailTurnFingerprint(turn);
    if (!transaction.sawLoading
        && fingerprint === transaction.initialFingerprint) return;
    if (fingerprint === transaction.initialFingerprint) {
      // The correlated request ended without replacing the detail page.
      releaseDetailAnchor();
      return;
    }
    const followingOlderPage = !!turn.detailAutoLoad
      && !!turn.detailHasMore && !!turn.detailOldestCursor;
    if (!followingOlderPage && !transaction.responseSettled) {
      transaction.responseSettled = true;
      if (transaction.requestTimer !== null) {
        window.clearTimeout(transaction.requestTimer);
        transaction.requestTimer = null;
      }
      transaction.maxSettleTimer = window.setTimeout(() => {
        transaction.maxSettleTimer = null;
        if (detailAnchorRef.current === transaction) releaseDetailAnchor();
      }, DETAIL_ANCHOR_MAX_SETTLE_MS);
    }
    const turnNode = turnNodeRefs.current.get(transaction.turnId);
    const processNode = turnNode?.querySelector<HTMLElement>(
      "[data-process-detail-root]",
    ) ?? null;
    if (processNode && transaction.observedNode !== processNode) {
      transaction.observer?.disconnect();
      transaction.observedNode = processNode;
      if (typeof ResizeObserver !== "undefined") {
        transaction.observer = new ResizeObserver(() => {
          scheduleDetailCorrection(transaction);
        });
        transaction.observer.observe(processNode);
      }
    }
    // Run after both React layout and TanStack's animation-frame ResizeObserver
    // measurement. ResizeObserver refreshes a short quiet window for delayed
    // image/Markdown growth; a hard deadline guarantees eventual unpin.
    scheduleDetailCorrection(transaction);
  }, [
    releaseDetailAnchor, scheduleDetailCorrection, scrollScope, turns,
  ]);

  const [copiedId, setCopiedId] = useState<string | null>(null);
  const copyText = (id: string, text: string) => {
    navigator.clipboard?.writeText(text);
    setCopiedId(id);
    setTimeout(() => setCopiedId(null), 1500);
  };
  const aiText = (t: Turn) => finalTextBlocks(t.blocks).map((block) => block.text).join("\n\n");

  // Collect engine-neutral file mutations. The helper also understands old
  // Claude file_path and Codex changes payloads already stored in browser cache.
  const fileChips = (t: Turn) => {
    const changes = collectTurnFileChanges([
      ...t.blocks,
      ...(t.liveSpillBlocks ?? []),
      ...(t.detailProjection?.blocks ?? []),
    ]);
    if (!changes.paths.length) return null;
    const arr = changes.paths;
    const canOpenSummary = surface !== "work"
      ? !!changes.diff && !!onOpenTurnDiff
      : (arr.length === 1 && !!onOpenFile) || !!onOpenArtifacts;
    const openSummary = () => {
      if (surface !== "work") {
        if (changes.diff && onOpenTurnDiff) onOpenTurnDiff(arr, changes.diff);
        return;
      }
      if (arr.length === 1 && onOpenFile) {
        onOpenFile(arr[0]);
        return;
      }
      onOpenArtifacts?.();
    };
    return (
      <div className="turn-files">
        <button className="turn-files-summary" onClick={openSummary}
          disabled={!canOpenSummary}
          title={surface === "work" ? "预览 Artifacts" : "查看本轮改动"}>
          <Icon name={surface === "work" ? "folder" : "edit"} size={13} />{
            surface === "work" ? `Artifacts · ${arr.length} 个文件` : `改动 ${arr.length} 个文件`
          }
        </button>
        <div className="turn-files-list">
          {arr.map((f) => {
            const markdown = surface !== "work" && isMarkdownPath(f) && !!onPreviewMarkdown;
            const canOpenFile = markdown || (!!changes.diff && !!onOpenTurnDiff);
            return <button key={f} className={"turn-file-chip" + (markdown ? " markdown" : "")}
              disabled={!canOpenFile}
              onClick={() => markdown
                ? onPreviewMarkdown(f)
                : onOpenTurnDiff?.(arr, changes.diff)}
              title={markdown ? `预览 ${f}`
                : changes.diff ? "查看本轮原生 diff" : "本轮没有可用的原生 diff"}>
              <Icon name={markdown ? "read" : "edit"} size={12} />
              {f.split("/").pop()}
              {markdown && <span className="turn-file-action">预览</span>}
            </button>;
          })}
        </div>
      </div>
    );
  };

  const measuredVirtualItems = virtualizer.getVirtualItems();
  const virtualLeadingPad = measuredVirtualItems[0]?.start ?? 0;
  const virtualLastItem = measuredVirtualItems[measuredVirtualItems.length - 1];
  const virtualTrailingPad = virtualLastItem
    ? Math.max(0, virtualizer.getTotalSize()
      - (virtualLastItem.start + virtualLastItem.size) - HISTORY_TURN_GAP_PX)
    : virtualizer.getTotalSize();
  const renderedVirtualItems = measuredVirtualItems.length > 0
    ? measuredVirtualItems
    : turns.slice(-4).map((_, offset) => {
      const index = Math.max(0, turns.length - 4) + offset;
      return {
        index,
        key: turnKeySnapshot.getItemKey(index),
        start: historyTopInset
          + index * (HISTORY_VIRTUAL_ESTIMATE_PX + HISTORY_TURN_GAP_PX),
      };
    });
  // A shared native Codex task can make two steer segments match the same
  // history alias. App passes those exact active candidates separately so an
  // idle, browsed, missing, or stale owner can never revive an arbitrary row.
  const ambiguousActiveTurnIdSet = new Set(ambiguousActiveTurnIds);
  const fallbackWorkingTurnId = activeTurnId == null
      && ambiguousActiveTurnIdSet.size > 1
    ? [...turns].reverse().find((turn) => {
        if (!ambiguousActiveTurnIdSet.has(turn.id)) return false;
        if (turn.done && (turn.interrupted || turn.error)) return false;
        const archived = mergeDetailWithLiveTail(
          turn.detailProjection?.blocks ?? [],
          turn.liveSpillBlocks ?? [],
          turn.done && !turn.detailRestorePending
            && !turn.detailRestoreIncomplete,
        );
        const timeline = mergeDetailWithLiveTail(
          archived,
          turn.blocks,
          turn.done && !turn.detailRestorePending
            && !turn.detailRestoreIncomplete,
        );
        const processItems = presentableProcessBlocks(timeline, engine);
        const foreground = turn.done
          ? processItems.filter((block) => !(
              block.kind === "process" && block.background === true
            ))
          : processItems;
        return !turn.done || hasActiveProcess(foreground)
          || foreground.some((block) => !block.done);
      })?.id ?? null
    : null;
  const activityRequest = historyRequestRef.current;
  const visibleHistoryPageActivity = historyPageActivity && (
    activityRequest?.activityKey === historyPageActivity.key
      && activityRequest.sid === sid)
    ? historyPageActivity : null;

  if (turns.length === 0) {
    if (loading) {
      return (
        <div className={surface === "work"
          ? "thread-shell work-thread-shell" : "thread-shell"}>
          <div className="empty">
            <div className="spinner" aria-label="加载中" />
            <p className="loading-tx">加载会话历史…</p>
          </div>
          <BackgroundProcessDock processes={backgroundProcesses}
            onOpenFile={onOpenFile} onOpenAgent={onOpenAgent} />
        </div>
      );
    }
    return (
      <div className={surface === "work"
        ? "thread-shell work-thread-shell" : "thread-shell"}>
        <div className="empty">
          <div className="glyph"><ClaudeMark size={30} /></div>
          <h2>{surface === "work" ? "工作区已就绪" : "已连接"}</h2>
          <p>{surface === "work"
            ? "添加资料并描述成果，我会把生成的文档和文件留在这项工作的私有目录。"
            : <>发一条消息开始，或用 <code>/</code> 唤起命令面板（Plan mode、review、技能…）。</>}</p>
        </div>
        <BackgroundProcessDock processes={backgroundProcesses}
          onOpenFile={onOpenFile} onOpenAgent={onOpenAgent} />
      </div>
    );
  }

  return (
    <div className={surface === "work" ? "thread-shell work-thread-shell" : "thread-shell"}>
      {visibleHistoryPageActivity && (
        <div className="history-page-activity"
          data-testid="history-page-activity" role="status" aria-live="polite">
          <span className="history-page-activity-spinner" aria-hidden="true" />
          <span>{visibleHistoryPageActivity.direction === "older"
            ? "正在加载更早历史…" : "正在加载更新历史…"}</span>
        </div>
      )}
      <div className="thread-frame">
        <div className="thread" ref={scrollRef}
        data-detail-anchor-active={activeDetailAnchor ? "true" : "false"}
        data-text-selection-dragging={
          activeTextSelection?.dragging ? "true" : "false"
        }
        data-text-selection-retained={activeTextSelection ? "true" : "false"}
        onScroll={onScroll} onWheel={onWheel}
        onKeyDownCapture={onKeyDown}
        onPointerDown={onThreadPointerDown}
        onTouchStart={onTouchStart} onTouchMove={onTouchMove}
        onTouchEnd={clearTouch} onTouchCancel={clearTouch}>
        <div ref={contentSizerRef} className="thread-in virtual-thread-in"
          style={{ position: "relative" }}
          data-lead={virtualLeadingPad} data-trail={virtualTrailingPad}
          data-total={virtualizer.getTotalSize()}
          data-count={measuredVirtualItems.length}>
          <div aria-hidden="true" style={{
            height: `${virtualLeadingPad}px`,
            overflowAnchor: "none",
          }} />
          {showOlderHistory
              && visibleHistoryPageActivity?.direction !== "older" && (
            <div className="load-more-wrap virtual-history-loader">
              {olderHistoryAvailability === "ready"
                ? (
                    <button className="load-more-btn"
                      data-testid="load-older-history" onClick={doLoadMore}>
                      加载更早的历史
                    </button>
                  )
                : (
                    <span className="load-more-status" role="status">
                      正在恢复历史…
                    </span>
                  )}
            </div>
          )}
          {canLoadNewer
              && visibleHistoryPageActivity?.direction !== "newer" && (
            <div className="load-more-wrap virtual-history-loader"
              style={{ top: "auto", bottom: 0 }}>
              <button className="load-more-btn" onClick={doLoadNewer}>
                加载更新的历史
              </button>
            </div>
          )}
          {renderedVirtualItems.map((virtualItem) => {
            const t = turns[virtualItem.index];
            if (!t) return null;
            const ti = virtualItem.index;
            const timelineWithArchive = mergeDetailWithLiveTail(
              t.detailProjection?.blocks ?? [],
              t.liveSpillBlocks ?? [],
              t.done && !t.detailRestorePending
                && !t.detailRestoreIncomplete,
            );
            const timelineBlocks = mergeDetailWithLiveTail(
              timelineWithArchive,
              t.blocks,
              t.done && !t.detailRestorePending
                && !t.detailRestoreIncomplete,
            );
            const processItems = presentableProcessBlocks(
              timelineBlocks, engine);
            const foregroundProcessItems = t.done
              ? processItems.filter((block) => !(
                  block.kind === "process" && block.background === true
                ))
              : processItems;
            const activeProcess = hasActiveProcess(foregroundProcessItems);
            const activeTimeline = activeProcess
              || foregroundProcessItems.some((block) => !block.done);
            const finalBlocks = finalTextBlocks(t.blocks);
            const generatedImages = generatedOutputImages(timelineBlocks);
            const followupBoundaries = backgroundFollowupBoundaries(
              finalBlocks, timelineBlocks);
            const enclosingTaskActive = activeTurnId === t.id;
            const processDetailState = processItems.length > 0
              ? "present"
              : t.processDetailState
                ?? ((t.detailEventCount ?? 0) > 0 ? "unknown" : "none");
            const hasDeferredContent = t.detailReasons?.some(
              (reason) => reason !== "process") ?? false;
            // A failed/interrupted enclosing terminal is authoritative. A
            // successful answer may still own genuine background agent work,
            // but a stale child flag must never animate beside "已打断".
            const terminalProblem = t.done && (!!t.interrupted || !!t.error);
            const working = !terminalProblem && (
              enclosingTaskActive || !t.done || activeTimeline);
            // A source can positively report process presence while an
            // incomplete detail response contains only the final answer. Do
            // not turn that contradiction into an endless loading row: keep
            // the disclosure and expose a retryable error instead.
            const missingLoadedProcess = t.done && !!t.detailLoaded
              && !t.detailLoading
              && !t.detailHasMore && !t.detailHasNewer
              && processItems.length === 0
              && processDetailState === "present";
            const processDetailError = t.detailError
              ?? (missingLoadedProcess
                ? "详细过程未完整返回，请重试" : undefined);
            const hasProcessTimeline = processItems.length > 0
              || processDetailState === "present";
            const activePhase = !working
              ? "complete"
              : hasProcessTimeline
                  && (activeTimeline
                    || (enclosingTaskActive
                      && (finalBlocks.length === 0 || t.done)))
                ? "process"
                : finalBlocks.length > 0 ? "answering" : "waiting";
            const showProcessTimeline = hasProcessTimeline;
            // Unknown native summaries don't prove that there is anything to
            // expand. Keep that uncertainty in the projection, not as a button
            // which disappears after an empty read. Real deferred content,
            // unread detail pages and explicit failures retain their controls.
            const canReadOlderDetail = !!t.detailHasMore && !!t.detailOldestCursor;
            const canReadNewerDetail = !!t.detailHasNewer && !!t.detailNewerCursor;
            const hasUnreadDetailPages = canReadOlderDetail || canReadNewerDetail;
            // The process disclosure already owns its paging controls. A second
            // entry below the answer would load invisible, collapsed process
            // rows. Keep a separate entry only for genuinely deferred content
            // or pages which don't yet have a process disclosure.
            const showStandaloneDetail = t.done && !t.detailLoaded
              && (hasDeferredContent || (!showProcessTimeline && hasUnreadDetailPages));
            const standaloneDetailLabel = hasDeferredContent
              ? "查看完整内容" : hasUnreadDetailPages ? "查看更多内容" : "重试加载详情";
            // Keep the live affordance at the physical tail of the turn. The
            // process disclosure can be far above the viewport once a long
            // tool stream grows, so it must not be the only place which tells
            // the reader that the turn is still active.
            // A steered Codex task can span several visible narrative rows.
            // Once the runtime has an exact owner, only that row may render
            // the session-level working affordance. Older rows can still keep
            // their own background/process lifecycle inside the disclosure,
            // but a delayed child update must not create a second top-level
            // spark beside the current steer row.
            const showWorking = working && (
              enclosingTaskActive
              || (ambiguousActiveTurnIdSet.has(t.id)
                && fallbackWorkingTurnId === t.id)
            );
            // Compact can close a display segment before the enclosing native
            // task reaches its terminal boundary. Completion time, copy and
            // fork belong to that real boundary, never to the segment bit.
            const showCompletionFooter = t.done && !working;
            const workingLabel = t.progress
              ?? (activePhase === "answering"
                ? "回答中"
                : activePhase === "process" ? "处理中" : "思考中");
            const processDisclosureId =
              t.clientMsgId ?? t.historyTurnId ?? t.id;
            const processOpenKey =
              `${scrollScope}\u0000turn:${processDisclosureId}`;
            const historyTurnId = t.historyTurnId ?? t.id;
            const externalPlanItemId = externalPlanProgress
              && externalPlanProgress.itemId
              && [t.id, historyTurnId, t.clientMsgId].includes(
                externalPlanProgress.turnId)
                ? externalPlanProgress.itemId : null;
            const detailRetryBefore = t.detailRetryBefore;
            const detailRetryDirection = t.detailRetryDirection;
            const standaloneDetailPage = detailRetryDirection
              ? [detailRetryBefore, detailRetryDirection] as const
              : !hasDeferredContent && canReadOlderDetail
                ? [t.detailOldestCursor, "older"] as const
                : !hasDeferredContent && canReadNewerDetail
                  ? [t.detailNewerCursor, "newer"] as const
                  : [undefined, "initial"] as const;
            const deferredProcessCount = (!t.detailLoaded || !!t.detailLoading)
                && processDetailState === "present"
              ? processItems.length === 0
                ? Math.max(1, t.detailEventCount ?? 0)
                : t.detailEventCount ?? 0
              : 0;
            const historyImagesReady = !!t.imageRefs?.length
              && t.imageRefs.every((image) => (
                historyImageAssets?.[historyImageAssetKey(
                  historyTurnId, image.image_id, "thumbnail")
                ]?.status === "ready"
              ));
            if (historyImagesReady) {
              turnImagePreviewCacheRef.current.release(t.id);
            }
            return (
            <div className="turn" key={virtualItem.key}
              data-index={virtualItem.index} data-turn-id={t.id}
              style={{
                marginBottom: `${HISTORY_TURN_GAP_PX}px`,
                overflowAnchor: "none",
              }}
              ref={(node) => {
                virtualizer.measureElement(node);
                if (node) {
                  turnNodeRefs.current.set(t.id, node);
                } else {
                  turnNodeRefs.current.delete(t.id);
                }
              }}>
            {(t.prompt || (t.images && t.images.length) || (t.imageRefs && t.imageRefs.length) || (t.files && t.files.length)) && (
              <div className="ubub-wrap">
                {t.prompt && <div className="ubub">{supplemental.replies.has(t.id)
                  ? <div className="supplemental-answer">
                      <details className="supplemental-answer-context">
                        <summary><Icon name="message" size={13} />查看问题<Icon name="chev" size={12} /></summary>
                        <div>{supplemental.replies.get(t.id)!.map((answer, index) =>
                          <p key={index}>{answer.question}</p>)}</div>
                      </details>
                      {supplemental.replies.get(t.id)!.map((answer, index) =>
                        <p key={index}>{answer.answer}</p>)}
                    </div>
                  : t.prompt}</div>}
                {t.images && t.images.length > 0 && (
                  <div className="ubub-imgs">
                    {t.images.map((img, i) => {
                      const src = `data:${img.media_type};base64,${img.data}`;
                      const [width, height] = queryImageDimensions(img)
                        ?? [180, 180];
                      return <button key={i} type="button" className="ubub-image-trigger"
                        style={{ aspectRatio: `${width} / ${height}` }}
                        aria-label="预览用户发送的图片"
                        onClick={() => setZoom({ kind: "data", src, alt: "用户发送的图片" })}>
                        <img src={src} className="ubub-img" width={width}
                          height={height} alt="用户发送的图片" />
                      </button>;
                    })}
                  </div>
                )}
                {(!t.images || t.images.length === 0)
                  && t.imageRefs && t.imageRefs.length > 0 && (
                  <div className="ubub-imgs">
                    {t.imageRefs.map((image, imageIndex) => {
                      const thumbnail = historyImageAssets?.[
                        historyImageAssetKey(
                          historyTurnId, image.image_id, "thumbnail")
                      ];
                      const fallback = turnImagePreviewCacheRef.current.get(
                        t.id, imageIndex);
                      return <HistoryUserImage key={image.image_id}
                        turnId={historyTurnId} imageId={image.image_id}
                        width={image.width} height={image.height}
                        asset={thumbnail} fallback={fallback}
                        onLoad={onLoadHistoryImage}
                        onPreview={() => {
                          if (fallback) {
                            setZoom({
                              kind: "data",
                              src: `data:${fallback.media_type};base64,${fallback.data}`,
                              alt: "用户发送的图片",
                            });
                            return;
                          }
                          onLoadHistoryImage?.(
                            historyTurnId, image.image_id, "full");
                          setZoom({
                            kind: "history", turnId: historyTurnId,
                            imageId: image.image_id, alt: "用户发送的图片",
                          });
                        }} />;
                    })}
                  </div>
                )}
                {t.files && t.files.length > 0 && (
                  <div className="ubub-files">
                    {t.files.map((f, i) => (
                      <span key={i} className="ubub-file"><Icon name="read" size={14} />{f.filename}</span>
                    ))}
                  </div>
                )}
                <div className="ubub-meta">
                  {t.ts && <span className="ubub-time">{formatTime(t.ts)}</span>}
                  {t.prompt && onEdit && <button className="ubub-act" onClick={() => onEdit(t.prompt!)} aria-label="编辑"><Icon name="edit" size={13} /></button>}
                  {t.prompt && <button className={"ubub-act" + (copiedId === t.id ? " copied" : "")} onClick={() => copyText(t.id, t.prompt!)} aria-label="复制"><Icon name="copy" size={13} /></button>}
                </div>
              </div>
            )}
            {showProcessTimeline && (
              <ProcessTimeline blocks={timelineBlocks} done={t.done}
                active={activePhase === "process"} engine={engine}
                durationMs={engine === "codex" ? undefined : t.durationMs}
                startTs={engine === "codex" ? t.processStartedTs : t.ts}
                doneTs={engine === "codex" ? t.processDoneTs : t.doneTs}
                deferredCount={deferredProcessCount}
                detailLoading={t.detailLoading}
                detailError={processDetailError}
                externalPlanItemId={externalPlanItemId}
                onLoadDetail={onLoadDetail
                  ? () => requestProcessDetail(
                      t.id, undefined, "initial", false)
                  : undefined}
                onRetryDetail={onLoadDetail && detailRetryDirection
                  ? () => requestProcessDetail(
                      t.id,
                      detailRetryBefore,
                      detailRetryDirection,
                      false)
                  : undefined}
                canLoadEarlier={canReadOlderDetail}
                canLoadNewer={canReadNewerDetail}
                onLoadEarlier={onLoadDetail && canReadOlderDetail
                  ? () => requestProcessDetail(
                      t.id, t.detailOldestCursor, "older")
                  : undefined}
                onLoadNewer={onLoadDetail && canReadNewerDetail
                  ? () => requestProcessDetail(
                      t.id, t.detailNewerCursor, "newer")
                  : undefined}
                onOpenFile={onOpenFile} imageAssets={imageAssets}
                onLoadImage={onLoadImage}
                onAuthorizeImage={onAuthorizeImage}
                historyTurnId={historyTurnId}
                historyImageAssets={historyImageAssets}
                onLoadHistoryImage={onLoadHistoryImage}
                onPreviewHistoryImage={(turnId, imageId) => setZoom({
                  kind: "history",
                  turnId,
                  imageId,
                  alt: "查看过的图片",
                })}
                onOpenAgent={onOpenAgent}
                onInteractionStart={beginProcessInteraction}
                onInteractionEnd={endProcessInteraction}
                openOverride={
                  processDisclosureOpen[`${processOpenKey}\u0000outer`]
                  ?? (t.detailRestoreOpen ? true : undefined)
                }
                onOpenChange={(open) => rememberProcessDisclosure(
                  `${processOpenKey}\u0000outer`, open,
                )}
                itemOpen={(key) =>
                  processDisclosureOpen[`${processOpenKey}\u0000${key}`]}
                onItemOpenChange={(key, open) => rememberProcessDisclosure(
                  `${processOpenKey}\u0000${key}`, open,
                )}
                onPreviewImage={(src, alt) => setZoom({ kind: "data", src, alt })} />
            )}
            {generatedImages.length > 0 && <div className="generated-image-gallery">
              {generatedImages.map((block) => <GeneratedImagePreview key={generatedImageIdentity(block)}
                block={block} imageAssets={imageAssets} onLoadImage={onLoadImage}
                onAuthorizeImage={onAuthorizeImage}
                historyTurnId={historyTurnId} historyImageAssets={historyImageAssets}
                onLoadHistoryImage={onLoadHistoryImage}
                onPreviewImage={(src, alt) => setZoom({ kind: "data", src, alt })}
                onPreviewHistoryImage={(turnId, imageId) => setZoom({
                  kind: "history", turnId, imageId, alt: "生成的图片",
                })} />)}
            </div>}
            {t.blocks.length > 0 && (
              <>
                {finalBlocks.map((block) => (
                  <div key={block.message_id} className="assistant-answer-segment">
                    {followupBoundaries.has(block.message_id) && (
                      <div className="background-followup-boundary">
                        <span className="background-followup-icon">
                          <Icon name="check" size={13} />
                        </span>
                        <span>{backgroundFollowupLabel(
                          followupBoundaries.get(block.message_id)
                            ?? undefined,
                        )} · Claude 随后继续回复</span>
                        {(followupBoundaries.get(block.message_id)?.terminalTs
                            || block.startedTs) && (
                          <time>{formatTime(
                            followupBoundaries.get(block.message_id)?.terminalTs
                              ?? block.startedTs!,
                          )}</time>
                        )}
                      </div>
                    )}
                    {block.delivery === "async" && block.questions?.length
                      ? <Suspense fallback={<span className="async-question-hint">助手询问…</span>}>
                          <AsyncQuestionCard questions={block.questions}
                            answered={supplemental.answered.has(asyncQuestionKey(t.id, block.message_id))}
                            onOpen={() => {
                              pauseOutputFollow();
                              setOpenAsyncQuestion({ scope: asyncQuestionScope, messageId: block.message_id });
                            }} />
                        </Suspense>
                      : <MessageBlock text={block.text}
                      done={block.done} onOpenFile={onOpenFile}
                      imageAssets={imageAssets} onLoadImage={onLoadImage}
                      onAuthorizeImage={onAuthorizeImage}
                      onPreviewImage={(src, alt) => setZoom({ kind: "data", src, alt })} />}
                  </div>
                ))}
                {/* Late page metadata shares a stable slot; completion metadata
                    still requires a real terminal, including after compaction. */}
                <div className={`ubub-meta ${showCompletionFooter ? "ai-meta" : "page-meta"}`}>
                  {showCompletionFooter && t.doneTs && <span className="ubub-time">{formatTime(t.doneTs)}</span>}
                  {showCompletionFooter && finalBlocks.length > 0 && <button
                    className={"ubub-act" + (copiedId === t.id + "-ai" ? " copied" : "")}
                    onClick={() => copyText(t.id + "-ai", aiText(t))}
                    aria-label="复制">
                    <Icon name="copy" size={13} />
                  </button>}
                  {showCompletionFooter && onFork && canForkTurn(engine, t) && (
                    <button className="ubub-act" aria-label="派生"
                      data-tooltip="从此回复派生新会话"
                      aria-busy={forkingPointId === t.forkPointId}
                      disabled={!!forkingPointId}
                      onClick={() => onFork(t.forkPointId)}>
                      <Icon name="branch" size={13} />
                    </button>
                  )}
                  <Suspense fallback={null}><PagePreviewLinks turn={t} sid={sid} /></Suspense>
                </div>
                {showCompletionFooter && ti === turns.length - 1
                  && <div className="turn-done-mark"><ClaudeSpark size={22} /></div>}
              </>
            )}
              {(showStandaloneDetail
                  || (!!t.detailError && !showProcessTimeline)) && (
                <div className="turn-detail-entry">
                  <button type="button" className="turn-detail-entry-btn"
                    disabled={!onLoadDetail || !!t.detailLoading}
                    aria-busy={!!t.detailLoading}
                    onClick={() => requestProcessDetail(
                      t.id,
                      ...standaloneDetailPage,
                      false,
                    )}>
                    {t.detailLoading
                      ? <span className="process-spin" aria-hidden="true" />
                      : <Icon name="chev" size={14} />}
                    <span>{t.detailLoading
                      ? "正在加载详情…" : standaloneDetailLabel}</span>
                  </button>
                  {t.detailError && (
                    <span className="turn-detail-entry-error">
                      {t.detailError}
                    </span>
                  )}
                </div>
              )}
              {showWorking && (
                <div className="turn-working" role="status" aria-live="polite">
                  <ClaudeWorking size={24} />
                  <span className="turn-working-tx">{workingLabel}</span>
                </div>
              )}
              {fileChips(t)}
              {t.done && t.interrupted && !t.error
                && <div className="note interrupted">— 已打断 —</div>}
              {t.error && <div className="note interrupted turn-failure">{
                presentHistoricalTurnProblem(t.error)
              }</div>}
            </div>
            );
          })}
          <div aria-hidden="true" style={{
            height: `${virtualTrailingPad}px`,
            overflowAnchor: "none",
          }} />
        </div>
        </div>
        {(!scrollState.followOutput || !scrollState.nearBottom) && (
          <div className="scroll-bottom-wrap">
            <button className="scroll-bottom-btn" onClick={returnToLatest}
              aria-label={browseMode ? "回到最新" : "滚动到底部"}
              data-tooltip={browseMode ? "回到最新" : undefined}>
              <Icon name="chev" size={20} />
            </button>
          </div>
        )}
      </div>
      <BackgroundProcessDock processes={backgroundProcesses}
        onOpenFile={onOpenFile} onOpenAgent={onOpenAgent} />
      {openAsyncQuestion?.scope === asyncQuestionScope && <Suspense fallback={null}>
        <AsyncQuestionHost key={asyncQuestionScope}
          messageId={openAsyncQuestion.messageId}
          turns={turns} answeredKeys={supplemental.answered}
          replyMode={asyncReplyMode} onReply={onReplyAsyncQuestion}
          onClose={() => setOpenAsyncQuestion(q => q ? { ...q, messageId: null } : null)} />
      </Suspense>}
      {zoom && (() => {
        const asset = zoom.kind === "history" ? historyImageAssets?.[
          historyImageAssetKey(zoom.turnId, zoom.imageId, "full")
        ] ?? historyImageAssets?.[
          historyImageAssetKey(zoom.turnId, zoom.imageId, "thumbnail")
        ] : null;
        const src = zoom.kind === "data" ? zoom.src
          : asset?.status === "ready" && asset.data && asset.mediaType
            ? `data:${asset.mediaType};base64,${asset.data}` : null;
        return src ? (
        <ImageLightbox key={sid ?? ""} src={src} alt={zoom.alt}
          onClose={() => setZoom(null)} />
        ) : null;
      })()}
    </div>
  );
}
