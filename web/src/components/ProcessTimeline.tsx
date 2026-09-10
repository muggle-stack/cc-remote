import {
  lazy,
  Suspense,
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  type ComponentProps,
} from "react";
import type {
  Block,
  ProcessBlock,
  TextBlock,
  ToolBlock,
} from "../domain/conversation";
import { Icon } from "../icons";
import { presentTurnOutcome } from "../problem-presentation";
import { MessageBlock } from "./MessageBlock";
import { PreviewAuthorizationPrompt } from "./PreviewAuthorizationPrompt";
import { HistoryUserImage } from "./HistoryUserImage";
import { ToolGroup } from "./ToolGroup";
import {
  hasActiveProcess,
  presentableProcessBlocks,
} from "../process-blocks";
import {
  filePathsFromInput,
  presentFileOperation,
} from "../file-changes";
import type { InlineImageAsset } from "../inline-image-assets";
import type { PreviewAuthorizationState } from "../reducer";
import {
  historyImageAssetKey,
  readyGeneratedImageAsset,
  type HistoryImageAsset,
  type HistoryImageVariant,
} from "../history-image-assets";
import {
  cancelDraggedPointer,
  PointerTapGuard,
  releaseDraggedPointer,
} from "../pointer-tap";

const DETAIL_REQUEST_ERROR = "详情请求失败";

const PlanProgressPopover = lazy(() => import("./PlanProgressPopover").then(
  ({ PlanProgressPopover: Popover }) => ({ default: Popover }),
));

function durationLabel(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  if (minutes < 60) return `${minutes}m ${rest}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

function statusIcon(status: ProcessBlock["status"], done: boolean) {
  if (!done && (status === "running" || status === "pending" || status === "unknown")) {
    return <span className="process-spin" />;
  }
  if (status === "failed" || status === "declined" || status === "cancelled"
      || status === "interrupted") {
    return <Icon name="close" size={14} />;
  }
  return <Icon name="verify" size={14} />;
}

function ProcessDisclosure({ className, summary, children, openOverride,
  onOpenChange, onInteractionStart, onInteractionEnd }: {
  className: string;
  summary: ReactNode;
  children: ReactNode;
  openOverride?: boolean;
  onOpenChange?: (open: boolean) => void;
  onInteractionStart?: () => number;
  onInteractionEnd?: (token: number, followOutput?: boolean) => void;
}) {
  const [uncontrolledOpen, setUncontrolledOpen] = useState(false);
  const open = openOverride ?? uncontrolledOpen;
  const tapGuard = useRef(new PointerTapGuard());
  const interactionTokens = useRef(new Map<number, number>());
  const pendingInteractionTokens = useRef(new Map<number, number>());
  const releaseInteractionFrame = useRef<number | null>(null);
  useEffect(() => () => {
    if (releaseInteractionFrame.current !== null) {
      window.cancelAnimationFrame(releaseInteractionFrame.current);
    }
    for (const token of interactionTokens.current.values()) {
      onInteractionEnd?.(token, false);
    }
    for (const token of pendingInteractionTokens.current.values()) {
      onInteractionEnd?.(token, false);
    }
    interactionTokens.current.clear();
    pendingInteractionTokens.current.clear();
  }, [onInteractionEnd]);
  const setOpen = (next: boolean) => {
    setUncontrolledOpen(next);
    onOpenChange?.(next);
  };
  const releaseInteraction = (pointerId: number) => {
    const token = interactionTokens.current.get(pointerId);
    if (token == null) return;
    interactionTokens.current.delete(pointerId);
    pendingInteractionTokens.current.set(pointerId, token);
    if (releaseInteractionFrame.current !== null) {
      window.cancelAnimationFrame(releaseInteractionFrame.current);
    }
    releaseInteractionFrame.current = window.requestAnimationFrame(() => {
      releaseInteractionFrame.current = window.requestAnimationFrame(() => {
        releaseInteractionFrame.current = null;
        for (const token of pendingInteractionTokens.current.values()) {
          onInteractionEnd?.(token);
        }
        pendingInteractionTokens.current.clear();
      });
    });
  };
  const cancelInteraction = (pointerId: number) => {
    const token = interactionTokens.current.get(pointerId)
      ?? pendingInteractionTokens.current.get(pointerId);
    if (token == null) return;
    interactionTokens.current.delete(pointerId);
    pendingInteractionTokens.current.delete(pointerId);
    onInteractionEnd?.(token, false);
  };
  return (
    <details className={className} open={open}>
      <summary
        onPointerDown={(event) => {
          if (event.pointerType === "mouse" && event.button === 0) {
            // WebKit can start a range in the next selectable paragraph even
            // when this control has user-select:none. Suppress only the mouse
            // selection default, preserving focus, clicks and native touch pan.
            event.preventDefault();
            event.currentTarget.focus({ preventScroll: true });
          }
          tapGuard.current.pointerDown(
            event.pointerId, event.clientX, event.clientY,
          );
          event.currentTarget.setPointerCapture?.(event.pointerId);
          cancelInteraction(event.pointerId);
          const token = onInteractionStart?.();
          if (token != null) interactionTokens.current.set(event.pointerId, token);
        }}
        onPointerMove={(event) => {
          if (tapGuard.current.pointerMove(
            event.pointerId, event.clientX, event.clientY,
          )) {
            cancelInteraction(event.pointerId);
            releaseDraggedPointer(
              event.currentTarget, event.pointerId, event.pointerType);
          }
        }}
        onPointerUp={(event) => {
          tapGuard.current.pointerUp(event.pointerId);
          releaseInteraction(event.pointerId);
        }}
        onPointerCancel={(event) => {
          cancelInteraction(event.pointerId);
          cancelDraggedPointer(
            tapGuard.current,
            event.currentTarget, event.pointerId, event.pointerType);
        }}
        onClick={(event) => {
          event.preventDefault();
          if (tapGuard.current.consumeClick(event.detail)) setOpen(!open);
        }}>
        {summary}
      </summary>
      {children}
    </details>
  );
}

const PROCESS_IC: Record<ProcessBlock["processKind"], string> = {
  reasoning: "spark",
  plan: "plan",
  command: "bash",
  file_change: "code",
  mcp: "term",
  agent: "spark",
  hook: "shield",
  server_tool: "term",
  web_search: "research",
  task: "plan",
  terminal: "bash",
  model: "cpu",
  safety: "shield",
  diff: "code",
  compaction: "simplify",
};

interface ProcessHistoryImageRef {
  image_id: string;
  media_type: string;
  width: number;
  height: number;
  byte_size: number;
}

function processImageRef(input?: Record<string, unknown> | null):
  ProcessHistoryImageRef | null {
  const value = input?.history_image;
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const ref = value as Record<string, unknown>;
  if (typeof ref.image_id !== "string"
      || typeof ref.media_type !== "string"
      || typeof ref.width !== "number"
      || typeof ref.height !== "number"
      || typeof ref.byte_size !== "number") return null;
  return ref as unknown as ProcessHistoryImageRef;
}

function ProcessImagePreview({
  path,
  previewId,
  imageAssets,
  onLoadImage,
  onAuthorizeImage,
  historyTurnId,
  historyRef,
  historyImageAssets,
  onLoadHistoryImage,
  onPreviewImage,
  onPreviewHistoryImage,
  generated = false,
}: {
  path: string;
  previewId?: string;
  imageAssets?: Record<string, InlineImageAsset>;
  onLoadImage?: (path: string, previewId?: string) => boolean;
  onAuthorizeImage?: (
    authorization: PreviewAuthorizationState,
    decision: "allow" | "deny",
  ) => boolean;
  historyTurnId?: string;
  historyRef?: ProcessHistoryImageRef | null;
  historyImageAssets?: Record<string, HistoryImageAsset>;
  onLoadHistoryImage?: (
    turnId: string,
    imageId: string,
    variant: HistoryImageVariant,
  ) => boolean;
  onPreviewImage?: (src: string, alt: string) => void;
  onPreviewHistoryImage?: (turnId: string, imageId: string) => void;
  generated?: boolean;
}) {
  const liveAsset = !historyRef && previewId ? imageAssets?.[previewId] : undefined;
  const useHistory = !!historyRef;
  const historyAsset = historyTurnId && historyRef && useHistory
    ? historyImageAssets?.[historyImageAssetKey(
        historyTurnId, historyRef.image_id, "thumbnail")]
    : undefined;
  useEffect(() => {
    if (useHistory && historyTurnId && historyRef) {
      if (!historyAsset) {
        onLoadHistoryImage?.(
          historyTurnId, historyRef.image_id, "thumbnail");
      }
      return;
    }
    if (previewId && !liveAsset && path) {
      onLoadImage?.(path, previewId);
    }
  }, [
    historyAsset,
    historyRef,
    historyTurnId,
    liveAsset,
    onLoadHistoryImage,
    onLoadImage,
    path,
    previewId,
    useHistory,
  ]);
  const asset = useHistory ? historyAsset : liveAsset;
  const src = asset?.status === "ready" && asset.data && asset.mediaType
    ? `data:${asset.mediaType};base64,${asset.data}`
    : null;
  const canLoad = Boolean(
    (historyTurnId && historyRef) || previewId,
  );
  if (!useHistory && liveAsset?.authorization) {
    return <PreviewAuthorizationPrompt
      authorization={liveAsset.authorization}
      compact
      onDecision={onAuthorizeImage} />;
  }
  return (
    <button type="button" className={"process-image-preview" + (generated ? " generated-image-preview" : "")}
      disabled={!canLoad}
      aria-label={
        generated ? (src ? "预览生成的图片" : "加载生成的图片") : src
          ? "预览查看过的图片"
          : canLoad ? "加载查看过的图片" : "等待图片读取完成"
      }
      onClick={() => {
        if (useHistory && historyTurnId && historyRef) {
          onLoadHistoryImage?.(
            historyTurnId, historyRef.image_id, "full");
          onPreviewHistoryImage?.(historyTurnId, historyRef.image_id);
          return;
        }
        if (src) {
          onPreviewImage?.(src, path || (generated ? "生成的图片" : "查看过的图片"));
        } else if (previewId) {
          onLoadImage?.(path, previewId);
        }
      }}>
      {src
        ? <img src={src} alt="" width={asset?.width} height={asset?.height} />
        : <span className="process-image-placeholder">
            <Icon name="read" size={16} />
          </span>}
      {(!generated || !src) && <span>{generated
        ? (asset?.status === "error" ? "图片加载失败 · 点击重试"
          : !canLoad ? "生成的图片暂不可用" : "正在加载图片…")
        : path || "查看过的图片"}</span>}
    </button>
  );
}

export function GeneratedImagePreview({ block, ...props }: {
  block: ProcessBlock;
} & Omit<ComponentProps<typeof ProcessImagePreview>, "path" | "previewId" | "historyRef" | "generated">) {
  const ref = processImageRef(block.input);
  // Retain only the snapshot id through live -> canonical projection changes.
  // Bytes remain in the scoped LRU: eviction and invalidation still win.
  const snapshotRef = useRef<{ imageId?: string; previewId?: string }>({});
  if (snapshotRef.current.imageId !== ref?.image_id) {
    snapshotRef.current = { imageId: ref?.image_id };
  }
  if (typeof block.input?.preview_id === "string") {
    snapshotRef.current.previewId = block.input.preview_id;
  }
  const previewId = snapshotRef.current.previewId;
  const live = previewId ? props.imageAssets?.[previewId] : undefined;
  const readyHistory = ref
    ? readyGeneratedImageAsset(props.historyImageAssets, ref.image_id) : undefined;
  const historyAsset = readyHistory ?? (ref && props.historyTurnId
    ? props.historyImageAssets?.[historyImageAssetKey(props.historyTurnId, ref.image_id, "full")]
    : undefined);
  const history = ref && props.historyTurnId && (readyHistory || !previewId || live?.status === "error");
  const dimensions = history ? ref : live;
  const path = filePathsFromInput(block.input)[0] ?? "";
  const open = () => {
    if (readyHistory) {
      props.onPreviewImage?.(`data:${readyHistory.mediaType};base64,${readyHistory.data}`, "生成的图片");
    } else if (history && ref && props.historyTurnId) {
      props.onLoadHistoryImage?.(props.historyTurnId, ref.image_id, "full");
      props.onPreviewHistoryImage?.(props.historyTurnId, ref.image_id);
    } else if (live?.status === "ready" && live.data && live.mediaType) {
      props.onPreviewImage?.(`data:${live.mediaType};base64,${live.data}`, "生成的图片");
    } else if (previewId) props.onLoadImage?.(path, previewId);
  };
  return <figure className="generated-output">
    {history && ref && props.historyTurnId ? <div className="generated-history-image">
      {/* Reuse history intersection loading and eviction guards. */}
      <HistoryUserImage turnId={props.historyTurnId} imageId={ref.image_id}
        width={ref.width} height={ref.height} label="生成的图片" variant="full"
        asset={historyAsset}
        onLoad={props.onLoadHistoryImage}
        onPreview={open} />
    </div> : <ProcessImagePreview {...props} generated path={path} previewId={previewId} />}
    <figcaption><Icon name="read" size={14} /><span>生成的图片</span>
      {!!dimensions?.width && !!dimensions.height && <span className="generated-image-size">· {dimensions.width} × {dimensions.height}</span>}
      {(history || previewId) && <button type="button" className="generated-image-open" onClick={open}>
        <Icon name="expand" size={13} />查看大图</button>}
    </figcaption>
  </figure>;
}

export function ProcessActivity({ block, onOpenFile, imageAssets, onLoadImage,
  onAuthorizeImage,
  historyTurnId, historyImageAssets, onLoadHistoryImage,
  onPreviewImage, onPreviewHistoryImage, openOverride, onOpenChange,
  onInteractionStart, onInteractionEnd, onOpenAgent }: {
  block: ProcessBlock;
  onOpenFile?: (path: string, line?: number) => void;
  imageAssets?: Record<string, InlineImageAsset>;
  onLoadImage?: (path: string, previewId?: string) => boolean;
  onAuthorizeImage?: (
    authorization: PreviewAuthorizationState,
    decision: "allow" | "deny",
  ) => boolean;
  historyTurnId?: string;
  historyImageAssets?: Record<string, HistoryImageAsset>;
  onLoadHistoryImage?: (
    turnId: string,
    imageId: string,
    variant: HistoryImageVariant,
  ) => boolean;
  onPreviewImage?: (src: string, alt: string) => void;
  onPreviewHistoryImage?: (turnId: string, imageId: string) => void;
  openOverride?: boolean;
  onOpenChange?: (open: boolean) => void;
  onInteractionStart?: () => number;
  onInteractionEnd?: (token: number, followOutput?: boolean) => void;
  onOpenAgent?: (runId: string, title?: string) => void;
}) {
  if (block.processKind === "agent" && onOpenAgent) {
    return (
      <button type="button"
        className={`process-activity process-agent-card process-${block.status}`}
        onClick={() => onOpenAgent(block.item_id, block.title)}>
        <span className="process-item-ic"><Icon name="spark" size={15} /></span>
        <span className="process-agent-copy">
          <span className="process-item-title">{block.title}</span>
          {(block.progress || block.summary) && (
            <span className="process-agent-summary">
              {block.progress || block.summary}
            </span>
          )}
        </span>
        <span className="process-item-status">{statusIcon(block.status, block.done)}</span>
        <span className="process-item-chev"><Icon name="chev" size={14} /></span>
      </button>
    );
  }
  const imageView = block.tool?.toLowerCase().replaceAll("_", "") === "viewimage";
  const imagePath = imageView
    ? filePathsFromInput(block.input)[0] ?? ""
    : "";
  const previewId = imageView && typeof block.input?.preview_id === "string"
    ? block.input.preview_id
    : undefined;
  const historyRef = imageView ? processImageRef(block.input) : null;
  const filePaths = block.processKind === "file_change"
    ? filePathsFromInput(block.input) : [];
  const semanticIcon = (
    block.processKind === "file_change" || block.processKind === "diff"
    || imageView
  )
    ? presentFileOperation(
        imageView
          ? "view_image"
          : block.processKind === "file_change" ? "filechange" : "apply_patch",
        block.input ?? {},
      )?.icon
    : undefined;
  const icon = semanticIcon ?? PROCESS_IC[block.processKind];
  const hasBody = !!(block.summary || block.detail || block.output || block.diff
    || block.progress || block.explanation || block.command || block.cwd
    || block.plan?.length || block.exit_code != null || block.duration_ms != null
    || (block.input && Object.keys(block.input).length));
  const body = (
    <>
      {block.progress && <div className="process-progress">{block.progress}</div>}
      {block.explanation && <div className="process-copy">{block.explanation}</div>}
      {block.plan && block.plan.length > 0 && (
        <ol className="process-plan">
          {block.plan.map((entry, index) => (
            <li key={`${index}-${entry.step}`} className={`plan-${entry.status}`}>
              <span>{entry.status === "completed" ? "✓" : entry.status === "inProgress" ? "•" : "○"}</span>
              <span>{entry.step}</span>
            </li>
          ))}
        </ol>
      )}
      {block.command && <pre className="tool-pre process-command">$ {block.command}</pre>}
      {block.cwd && <div className="process-meta">{block.cwd}</div>}
      {block.summary && !imageView
        && <div className="process-copy">{block.summary}</div>}
      {block.detail && <pre className="tool-pre">{block.detail}</pre>}
      {onOpenFile && filePaths.map((filePath) => (
        <button key={filePath} type="button" className="process-file-link"
          onClick={() => onOpenFile(filePath)}>
          <Icon name="file" size={14} /><span>{filePath}</span>
        </button>
      ))}
      {imageView && (
        <ProcessImagePreview path={imagePath} previewId={previewId}
          imageAssets={imageAssets} onLoadImage={onLoadImage}
          onAuthorizeImage={onAuthorizeImage}
          historyTurnId={historyTurnId} historyRef={historyRef}
          historyImageAssets={historyImageAssets}
          onLoadHistoryImage={onLoadHistoryImage}
          onPreviewImage={onPreviewImage}
          onPreviewHistoryImage={onPreviewHistoryImage} />
      )}
      {block.input && Object.keys(block.input).length > 0
        && filePaths.length === 0 && !imageView && (
        <pre className="tool-pre">{JSON.stringify(block.input, null, 2)}</pre>
      )}
      {block.output && <pre className="tool-pre">{block.output}{block.truncated ? "\n…(truncated)" : ""}</pre>}
      {block.diff && <pre className="tool-pre tool-diff">{block.diff}</pre>}
      {(block.exit_code != null || block.duration_ms != null) && (
        <div className="tool-meta">
          {block.exit_code != null && <span>exit {block.exit_code}</span>}
          {block.duration_ms != null && <span>{durationLabel(block.duration_ms)}</span>}
        </div>
      )}
    </>
  );

  if (!hasBody) {
    return (
      <div className={`process-activity process-${block.status}`}>
        <span className="process-item-ic"><Icon name={icon} size={15} /></span>
        <span className="process-item-title">{block.title}</span>
        <span className="process-item-status">{statusIcon(block.status, block.done)}</span>
      </div>
    );
  }
  return (
    <ProcessDisclosure className={`process-activity process-${block.status}`}
      openOverride={openOverride} onOpenChange={onOpenChange}
      onInteractionStart={onInteractionStart}
      onInteractionEnd={onInteractionEnd}
      summary={
        <>
        <span className="process-item-ic"><Icon name={icon} size={15} /></span>
        <span className="process-item-title">{block.title}</span>
        <span className="process-item-status">{statusIcon(block.status, block.done)}</span>
        <span className="process-item-chev"><Icon name="chev" size={14} /></span>
        </>
      }>
        <div className="process-item-body">{body}</div>
    </ProcessDisclosure>
  );
}

export function BackgroundProcessDock({ processes, onOpenFile, onOpenAgent }: {
  processes: ProcessBlock[];
  onOpenFile?: (path: string, line?: number) => void;
  onOpenAgent?: (runId: string, title?: string) => void;
}) {
  if (processes.length === 0) return null;
  return (
    <aside className="background-process-dock">
      <div className="background-process-head">
        <span className="background-process-pulse" />
        <span>后台任务正在运行</span>
        <span className="background-process-count">{processes.length}</span>
      </div>
      <div className="background-process-items">
        {processes.map((process) => (
          <ProcessActivity key={process.item_id} block={process}
            onOpenFile={onOpenFile} onOpenAgent={onOpenAgent} />
        ))}
      </div>
    </aside>
  );
}

function TimelineItem({ block, onOpenFile, imageAssets, onLoadImage,
  onAuthorizeImage, onPreviewImage,
  historyTurnId, historyImageAssets, onLoadHistoryImage,
  onPreviewHistoryImage,
  itemOpen, onItemOpenChange, onInteractionStart, onInteractionEnd,
  onOpenAgent }: {
  block: Block;
  onOpenFile?: (path: string, line?: number) => void;
  imageAssets?: Record<string, InlineImageAsset>;
  onLoadImage?: (path: string, previewId?: string) => boolean;
  onAuthorizeImage?: (
    authorization: PreviewAuthorizationState,
    decision: "allow" | "deny",
  ) => boolean;
  onPreviewImage?: (src: string, alt: string) => void;
  historyTurnId?: string;
  historyImageAssets?: Record<string, HistoryImageAsset>;
  onLoadHistoryImage?: (
    turnId: string,
    imageId: string,
    variant: HistoryImageVariant,
  ) => boolean;
  onPreviewHistoryImage?: (turnId: string, imageId: string) => void;
  itemOpen?: (key: string) => boolean | undefined;
  onItemOpenChange?: (key: string, open: boolean) => void;
  onInteractionStart?: () => number;
  onInteractionEnd?: (token: number, followOutput?: boolean) => void;
  onOpenAgent?: (runId: string, title?: string) => void;
}) {
  if (block.kind === "process") {
    const key = `process:${block.item_id}`;
    return <ProcessActivity
      block={block as ProcessBlock} onOpenFile={onOpenFile}
      imageAssets={imageAssets} onLoadImage={onLoadImage}
      onAuthorizeImage={onAuthorizeImage}
      historyTurnId={historyTurnId}
      historyImageAssets={historyImageAssets}
      onLoadHistoryImage={onLoadHistoryImage}
      onPreviewImage={onPreviewImage}
      onPreviewHistoryImage={onPreviewHistoryImage}
      openOverride={itemOpen?.(key)}
      onOpenChange={(open) => onItemOpenChange?.(key, open)}
      onInteractionStart={onInteractionStart}
      onInteractionEnd={onInteractionEnd} onOpenAgent={onOpenAgent} />;
  }
  const text = block as TextBlock;
  if (text.channel === "thinking") {
    const key = `reasoning:${text.message_id}`;
    return (
      <ProcessDisclosure className="process-reasoning"
        openOverride={itemOpen?.(key)}
        onOpenChange={(open) => onItemOpenChange?.(key, open)}
        onInteractionStart={onInteractionStart}
        onInteractionEnd={onInteractionEnd}
        summary={<><Icon name="spark" size={14} /><span>思考</span>
          <Icon name="chev" size={13} /></>}>
        <div className="process-reasoning-body"><MessageBlock text={text.text}
          done={text.done} onOpenFile={onOpenFile} imageAssets={imageAssets}
          onLoadImage={onLoadImage} onAuthorizeImage={onAuthorizeImage}
          onPreviewImage={onPreviewImage} /></div>
      </ProcessDisclosure>
    );
  }
  return <div className="process-commentary"><MessageBlock text={text.text}
    done={text.done} onOpenFile={onOpenFile} imageAssets={imageAssets}
    onLoadImage={onLoadImage} onAuthorizeImage={onAuthorizeImage}
    onPreviewImage={onPreviewImage} /></div>;
}

type TimelineRow =
  | { kind: "item"; block: TextBlock | ProcessBlock }
  | { kind: "tools"; tools: ToolBlock[] };

function groupTimelineRows(items: Block[]): TimelineRow[] {
  const rows: TimelineRow[] = [];
  for (const block of items) {
    if (block.kind !== "tool") {
      rows.push({ kind: "item", block });
      continue;
    }
    const previous = rows[rows.length - 1];
    if (previous?.kind === "tools") previous.tools.push(block);
    else rows.push({ kind: "tools", tools: [block] });
  }
  return rows;
}

const TERMINAL_PROCESS_STATUSES = new Set([
  "succeeded", "failed", "declined", "cancelled", "interrupted",
]);

function isCommandTool(block: ToolBlock): boolean {
  if (block.category === "command") return true;
  return ["bash", "shell", "commandexecution"].includes(
    block.tool.toLowerCase(),
  );
}

function isGenericCommandTitle(title: string | null | undefined): boolean {
  return !title || title === "运行命令" || title === "Run command";
}

function isPayloadFreeUnfinishedCommandShell(block: Block): boolean {
  if (block.kind === "text") return false;
  if (block.kind === "tool") {
    if (block.done || !isCommandTool(block)) return false;
    const result = block.result;
    const hasPayload = Object.keys(block.input).length > 0
      || !isGenericCommandTitle(block.title)
      || !!block.server
      || !!block.output
      || !!block.diff
      || !!block.progress
      || !!result?.content
      || !!result?.summary
      || !!result?.diff
      || result?.status != null
      || result?.exit_code != null
      || result?.duration_ms != null
      || result?.is_error === true;
    return !hasPayload;
  }
  if (block.processKind !== "command"
      || block.done
      || TERMINAL_PROCESS_STATUSES.has(block.status)) return false;
  const hasPayload = !!block.command
    || !!block.output
    || !!block.diff
    || !!block.summary
    || !!block.detail
    || !!block.progress
    || !!block.explanation
    || !!block.plan?.length
    || !!(block.input && Object.keys(block.input).length > 0)
    || !!block.cwd
    || !!block.server
    || !!block.tool
    || block.exit_code != null
    || block.duration_ms != null;
  return !hasPayload;
}

export function ProcessTimeline({ blocks, done, active, outcome, problem, durationMs, startTs, doneTs, onOpenFile,
  deferredCount = 0, detailLoading = false, detailError, onLoadDetail,
  onRetryDetail,
  canLoadEarlier = false, canLoadNewer = false,
  onLoadEarlier, onLoadNewer,
  imageAssets, onLoadImage, onAuthorizeImage, onPreviewImage, engine = "claude",
  historyTurnId, historyImageAssets, onLoadHistoryImage,
  onPreviewHistoryImage,
  externalPlanItemId,
  openOverride, onOpenChange, itemOpen, onItemOpenChange,
  onInteractionStart, onInteractionEnd, onOpenAgent }: {
  blocks: Block[];
  done: boolean;
  /** Whether this process shell describes the turn's active live phase. */
  active?: boolean;
  /** An enclosing terminal failure is separate from individual tool results. */
  outcome?: "failed" | "interrupted";
  problem?: string;
  durationMs?: number;
  startTs?: number;
  doneTs?: number;
  onOpenFile?: (path: string, line?: number) => void;
  deferredCount?: number;
  detailLoading?: boolean;
  detailError?: string | null;
  onLoadDetail?: () => boolean | void;
  onRetryDetail?: () => boolean | void;
  canLoadEarlier?: boolean;
  canLoadNewer?: boolean;
  onLoadEarlier?: () => void;
  onLoadNewer?: () => void;
  imageAssets?: Record<string, InlineImageAsset>;
  onLoadImage?: (path: string, previewId?: string) => boolean;
  onAuthorizeImage?: (
    authorization: PreviewAuthorizationState,
    decision: "allow" | "deny",
  ) => boolean;
  onPreviewImage?: (src: string, alt: string) => void;
  historyTurnId?: string;
  historyImageAssets?: Record<string, HistoryImageAsset>;
  onLoadHistoryImage?: (
    turnId: string,
    imageId: string,
    variant: HistoryImageVariant,
  ) => boolean;
  onPreviewHistoryImage?: (turnId: string, imageId: string) => void;
  engine?: "claude" | "codex" | "dsh";
  /** The session-level progress strip owns this plan instead of this row. */
  externalPlanItemId?: string | null;
  openOverride?: boolean;
  onOpenChange?: (open: boolean) => void;
  itemOpen?: (key: string) => boolean | undefined;
  onItemOpenChange?: (key: string, open: boolean) => void;
  onInteractionStart?: () => number;
  onInteractionEnd?: (token: number, followOutput?: boolean) => void;
  onOpenAgent?: (runId: string, title?: string) => void;
}) {
  const retainedPlanBlock = useRef<ProcessBlock | null>(null);
  // Codex does not expose its private chain of thought in official clients.
  // Keep actionable commentary, plans, hook failures and tools, but suppress
  // synthetic reasoning and successful hook plumbing so consecutive tool calls
  // collapse into one useful group.
  const projectedItems = presentableProcessBlocks(blocks, engine);
  const needsAuthoritativeDetail = deferredCount > 0;
  // Summary History may include bounded lifecycle/tool shells so the header can
  // report that work exists, but their inputs and outputs are intentionally
  // absent. Hide only those payload-free command shells: a same-revision cache
  // may already contain useful recent rows which must remain visible while one
  // click fetches the rest of the authoritative detail.
  const items = needsAuthoritativeDetail
    ? projectedItems.filter(
        (block) => !isPayloadFreeUnfinishedCommandShell(block),
      )
    : projectedItems;
  const planBlocks = items.filter((block): block is ProcessBlock =>
    block.kind === "process" && block.processKind === "plan");
  // Prefer the newest structured update for the compact progress control. A
  // turn can also contain older free-form plan records with a different item
  // id; keep those in chronology instead of deleting every plan-shaped row.
  const planBlock = [...planBlocks].reverse().find(
    (block) => block.plan != null) ?? planBlocks.at(-1);
  // Authoritative detail replaces the provisional cache page before all older
  // pages arrive. Keep the already-painted plan affordance mounted through that
  // transition; otherwise one click makes its own popover disappear briefly.
  if (planBlock) retainedPlanBlock.current = planBlock;
  const visiblePlanBlock = planBlock ?? retainedPlanBlock.current;
  const inlinePlanBlock = visiblePlanBlock?.item_id === externalPlanItemId
    ? null : visiblePlanBlock;
  const timelineItems = planBlock
    ? items.filter((block) => block !== planBlock)
    : items;
  const processActive = active ?? (!done && (
    hasActiveProcess(projectedItems) || projectedItems.length > 0
  ));
  const foregroundItems = done ? projectedItems.filter((block) => !(
    block.kind === "process" && block.background === true
  )) : projectedItems;
  const terminalComplete = done && !processActive
    && (!!outcome || !hasActiveProcess(foregroundItems));
  const processSettled = !processActive;
  const terminalOutcome = processSettled && done ? outcome : undefined;
  const [uncontrolledOpen, setUncontrolledOpen] = useState(!terminalComplete);
  const open = openOverride ?? uncontrolledOpen;
  const [localDetailError, setLocalDetailError] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now());
  const manuallyToggled = useRef(false);
  const tapGuard = useRef(new PointerTapGuard());
  const interactionTokens = useRef(new Map<number, number>());
  const pendingInteractionTokens = useRef(new Map<number, number>());
  const releaseInteractionFrame = useRef<number | null>(null);

  useEffect(() => {
    if (!manuallyToggled.current) setUncontrolledOpen(!terminalComplete);
  }, [terminalComplete]);
  useEffect(() => {
    if (detailLoading || !needsAuthoritativeDetail) {
      setLocalDetailError(null);
    }
  }, [detailLoading, needsAuthoritativeDetail]);
  useEffect(() => {
    if (!processActive) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [processActive]);
  useEffect(() => () => {
    if (releaseInteractionFrame.current !== null) {
      window.cancelAnimationFrame(releaseInteractionFrame.current);
      releaseInteractionFrame.current = null;
    }
    for (const token of interactionTokens.current.values()) {
      onInteractionEnd?.(token, false);
    }
    for (const token of pendingInteractionTokens.current.values()) {
      onInteractionEnd?.(token, false);
    }
    interactionTokens.current.clear();
    pendingInteractionTokens.current.clear();
  }, [onInteractionEnd]);

  const hasDeferredOnly = timelineItems.length === 0 && needsAuthoritativeDetail;
  const waitingForContent = timelineItems.length === 0
    && !visiblePlanBlock && processActive;
  const visibleDetailError = detailError ?? localDetailError;
  const showOuterDisclosure = timelineItems.length > 0
    || hasDeferredOnly || waitingForContent || !!visibleDetailError
    || canLoadEarlier || canLoadNewer;
  if (!inlinePlanBlock && !showOuterDisclosure
      && !visibleDetailError) return null;
  // A completed timeline is collapsed. Do not allocate/group hundreds of
  // historical rows until the user actually opens it.
  const rows = open ? groupTimelineRows(timelineItems) : [];
  const toolCount = timelineItems.reduce(
    (count, block) => count + (block.kind === "tool" ? 1 : 0), 0);
  const countLabel = visibleDetailError && timelineItems.length === 0
    ? "加载失败"
    : needsAuthoritativeDetail
    ? `${deferredCount} 项`
    : waitingForContent
      ? "等待响应"
    : timelineItems.length === 0 && (canLoadEarlier || canLoadNewer)
      ? "更多过程"
    : engine === "codex" && toolCount === timelineItems.length
      ? `${toolCount} 个工具调用`
      : `${timelineItems.length} 项`;
  const rawElapsed: number | null = terminalComplete
    ? durationMs != null && durationMs > 0
      ? durationMs
      : startTs != null && doneTs != null
        ? engine === "codex" && durationMs === 0
          ? 0
          : Math.max(0, doneTs - startTs)
        : null
    : processActive
      ? startTs == null ? null : Math.max(0, now - startTs)
      : durationMs != null && durationMs > 0 ? durationMs : null;
  // Rounded sub-second intervals render as "0s", which looks like a broken
  // clock and can be resurrected from a pre-fix browser cache. Presence and
  // timing are independent: keep the process label, omit only the unusable
  // duration until at least one displayable second is available.
  const elapsed = rawElapsed != null && rawElapsed >= 500
    ? rawElapsed : null;
  const requestDetail = () => {
    setLocalDetailError(null);
    if (onLoadDetail?.() === false) {
      setLocalDetailError(DETAIL_REQUEST_ERROR);
    }
  };
  const retryDetail = () => {
    setLocalDetailError(null);
    if ((onRetryDetail ?? onLoadDetail)?.() === false) {
      setLocalDetailError(DETAIL_REQUEST_ERROR);
    }
  };
  const toggle = () => {
    manuallyToggled.current = true;
    if (needsAuthoritativeDetail) {
      const next = !open;
      if (next && !detailLoading) requestDetail();
      setUncontrolledOpen(next);
      onOpenChange?.(next);
      return;
    }
    const next = !open;
    setUncontrolledOpen(next);
    onOpenChange?.(next);
  };
  const pointerDown = (event: ReactPointerEvent<HTMLButtonElement>) => {
    tapGuard.current.pointerDown(event.pointerId, event.clientX, event.clientY);
    event.currentTarget.setPointerCapture?.(event.pointerId);
    releaseCancelledInteraction(event.pointerId);
    const token = onInteractionStart?.();
    if (token != null) interactionTokens.current.set(event.pointerId, token);
  };
  const pointerMove = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (tapGuard.current.pointerMove(
      event.pointerId, event.clientX, event.clientY,
    )) {
      releaseCancelledInteraction(event.pointerId);
      releaseDraggedPointer(
        event.currentTarget, event.pointerId, event.pointerType);
    }
  };
  const pointerUp = (event: ReactPointerEvent<HTMLButtonElement>) => {
    tapGuard.current.pointerUp(event.pointerId);
    releaseInteraction(event.pointerId);
  };
  const pointerCancel = (event: ReactPointerEvent<HTMLButtonElement>) => {
    releaseCancelledInteraction(event.pointerId);
    cancelDraggedPointer(
      tapGuard.current,
      event.currentTarget, event.pointerId, event.pointerType);
  };
  const releaseCancelledInteraction = (pointerId: number) => {
    const token = interactionTokens.current.get(pointerId)
      ?? pendingInteractionTokens.current.get(pointerId);
    if (token == null) return;
    interactionTokens.current.delete(pointerId);
    pendingInteractionTokens.current.delete(pointerId);
    onInteractionEnd?.(token, false);
  };
  const releaseInteraction = (pointerId: number) => {
    const token = interactionTokens.current.get(pointerId);
    if (token == null) return;
    interactionTokens.current.delete(pointerId);
    pendingInteractionTokens.current.set(pointerId, token);
    if (releaseInteractionFrame.current !== null) {
      window.cancelAnimationFrame(releaseInteractionFrame.current);
    }
    // Native click is dispatched after pointerup in the same task. Keep the
    // viewport frozen through the following ResizeObserver frame as well, so
    // the clicked disclosure can settle before output following resumes.
    releaseInteractionFrame.current = window.requestAnimationFrame(() => {
      releaseInteractionFrame.current = window.requestAnimationFrame(() => {
        releaseInteractionFrame.current = null;
        for (const token of pendingInteractionTokens.current.values()) {
          onInteractionEnd?.(token);
        }
        pendingInteractionTokens.current.clear();
      });
    });
  };
  return (
    <section data-process-detail-root
      className={`turn-process${open ? " open" : ""}`}>
      <div className="turn-process-controls">
        {showOuterDisclosure && <button type="button" className="turn-process-head"
          aria-expanded={open} aria-busy={detailLoading}
          onPointerDown={pointerDown} onPointerMove={pointerMove}
          onPointerUp={pointerUp} onPointerCancel={pointerCancel}
          onClick={(event) => {
            if (!tapGuard.current.consumeClick(event.detail)) {
              event.preventDefault();
              return;
            }
            toggle();
          }}>
          <span className={`turn-process-state ${terminalOutcome ?? (processSettled ? "done" : "running")}`}>
            {detailLoading && !processActive
              ? <span className="process-spin" />
              : <Icon name={processActive ? "spark" : terminalOutcome === "failed"
                ? "info" : terminalOutcome === "interrupted" ? "stop" : "verify"} size={14} />}
          </span>
          <span>{terminalOutcome ? presentTurnOutcome(terminalOutcome, problem)
            : processSettled ? "已处理" : "正在处理"}
            {elapsed == null ? null : ` ${durationLabel(elapsed)}`}</span>
          <span className="turn-process-count">{countLabel}</span>
          <Icon name="chev" size={15} />
        </button>}
        {inlinePlanBlock && <Suspense fallback={
          <span className="plan-progress-control" role="status"
            aria-label="正在加载计划进度">
            <span className="plan-progress-trigger" aria-hidden="true" />
          </span>
        }>
          <PlanProgressPopover block={inlinePlanBlock}
            openOverride={itemOpen?.(`plan:${inlinePlanBlock.item_id}`)}
            onOpenChange={(next) => onItemOpenChange?.(
              `plan:${inlinePlanBlock.item_id}`, next)}
            detailLoading={detailLoading}
            onNeedDetail={needsAuthoritativeDetail && !detailLoading
              ? requestDetail : undefined} />
        </Suspense>}
      </div>
      {showOuterDisclosure && open && <div className="process-timeline">
        {visibleDetailError && (
          <div className="process-detail-error" role="alert">
            <span>{visibleDetailError}</span>
            <button type="button" disabled={detailLoading}
              onClick={(event) => {
                event.stopPropagation();
                retryDetail();
              }}>
              重试
            </button>
          </div>
        )}
        {(hasDeferredOnly || waitingForContent)
          && !visibleDetailError && (
          <div className="process-detail-loading" role="status">
            {hasDeferredOnly ? "正在加载过程…" : "等待模型响应…"}
          </div>
        )}
        {canLoadEarlier && (
          <button type="button" className="process-page-control earlier"
            disabled={detailLoading} onClick={onLoadEarlier}>
            <Icon name="chev" size={14} />
            加载更早过程
          </button>
        )}
        {rows.map((row) => (
          row.kind === "tools"
            ? <ToolGroup key={`tools-${row.tools[0].tool_use_id}`} tools={row.tools} />
            : <TimelineItem key={row.block.kind === "text"
                ? `text-${row.block.message_id}` : `process-${row.block.item_id}`}
                block={row.block} onOpenFile={onOpenFile}
                imageAssets={imageAssets} onLoadImage={onLoadImage}
                onAuthorizeImage={onAuthorizeImage}
                onPreviewImage={onPreviewImage}
                historyTurnId={historyTurnId}
                historyImageAssets={historyImageAssets}
                onLoadHistoryImage={onLoadHistoryImage}
                onPreviewHistoryImage={onPreviewHistoryImage}
                onOpenAgent={onOpenAgent}
                itemOpen={itemOpen} onItemOpenChange={onItemOpenChange}
                onInteractionStart={onInteractionStart}
                onInteractionEnd={onInteractionEnd} />
        ))}
        {canLoadNewer && (
          <button type="button" className="process-page-control newer"
            disabled={detailLoading} onClick={onLoadNewer}>
            返回较新过程
            <Icon name="chev" size={14} />
          </button>
        )}
      </div>}
    </section>
  );
}
