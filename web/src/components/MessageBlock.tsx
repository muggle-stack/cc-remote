import { createContext, isValidElement, memo, useContext, useEffect, useId,
  useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore,
  type ComponentPropsWithoutRef,
  type ReactNode } from "react";
import { createPortal } from "react-dom";
import ReactMarkdown, { defaultUrlTransform, type Components, type UrlTransform } from "react-markdown";
import { parseLocalFileTarget } from "../file-link";
import { RemoteViewerContext } from "../remote-viewer-context";
import { ViewerPagesContext } from "../viewer-pages-context";
import { isLocalViewerUrl } from "../remote-viewer";
import { Icon } from "../icons";
import {
  classifyMessageImageTarget,
  inlineImageAssetCacheSnapshot,
  INLINE_IMAGE_REQUEST_TIMEOUT_MS,
  subscribeInlineImageAssetCacheChanges,
  type InlineImageAsset,
} from "../inline-image-assets";
import type { PreviewAuthorizationState } from "../reducer";
import { isMermaidFenceClass } from "../mermaid";
import {
  isMathFenceClass,
  STREAMING_REMARK_PLUGINS,
  useMarkdownMathPlugins,
} from "../markdown-math";
import { useSanitizedSvgUrl } from "../use-sanitized-svg";
import { MermaidBlock } from "./MermaidBlock";
import { PreviewAuthorizationPrompt } from "./PreviewAuthorizationPrompt";
import { useMarkdownExtras } from "../use-markdown-extras";

// Keep the parser behind its own memo boundary. MessageBlock still receives
// fresh callbacks/assets through context, but an unchanged displayed string
// must not run the Markdown pipeline on each raw delta, scroll or drawer update.
// Context consumers inside the cached tree continue to update independently.
const MessageMarkdown = memo(ReactMarkdown);

const CODEX_DIRECTIVE_LABELS: Record<string, string> = {
  "git-stage": "Git 变更已暂存",
  "git-commit": "Git 提交已创建",
  "git-create-branch": "Git 分支已创建",
  "git-push": "Git 分支已推送",
  "git-create-pr": "Pull Request 已创建",
  "created-thread": "Codex 任务已创建",
};

type MessagePart =
  | { kind: "markdown"; text: string }
  | { kind: "directive"; name: string; label: string }
  | { kind: "visualization"; path: string; title: string };

const CODEX_VISUALIZATION_PREFIX = "visualize";
const CODEX_VISUALIZATION_SUFFIX = "";
const MAX_VISUALIZATION_PATH_CHARS = 4096;
const MAX_VISUALIZATION_TITLE_CHARS = 240;

function parseCodexVisualization(line: string): MessagePart | null {
  const value = line.trim();
  if (!value.startsWith(CODEX_VISUALIZATION_PREFIX)
      || !value.endsWith(CODEX_VISUALIZATION_SUFFIX)) return null;
  const encoded = value.slice(
    CODEX_VISUALIZATION_PREFIX.length,
    -CODEX_VISUALIZATION_SUFFIX.length,
  );
  let payload: unknown;
  try {
    payload = JSON.parse(encoded);
  } catch {
    return null;
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  const record = payload as Record<string, unknown>;
  if (typeof record.path !== "string"
      || record.path.length > MAX_VISUALIZATION_PATH_CHARS) return null;
  const target = parseLocalFileTarget(record.path);
  if (!target || !/\.html?$/i.test(target.path)) return null;
  const filename = target.path.replace(/\\/g, "/").split("/").pop()
    ?.replace(/\.html?$/i, "") || "可视化";
  const suppliedTitle = typeof record.title === "string"
    ? record.title.trim().slice(0, MAX_VISUALIZATION_TITLE_CHARS)
    : "";
  return {
    kind: "visualization",
    path: target.path,
    title: suppliedTitle || filename,
  };
}

function splitCodexRichContent(text: string): MessagePart[] {
  const parts: MessagePart[] = [];
  let markdown = "";
  let fence: "`" | "~" | null = null;
  const lines = text.split("\n");
  const flushMarkdown = () => {
    if (!markdown) return;
    parts.push({ kind: "markdown", text: markdown });
    markdown = "";
  };

  lines.forEach((line, index) => {
    const suffix = index < lines.length - 1 ? "\n" : "";
    const fenceMatch = line.match(/^\s*(`{3,}|~{3,})/);
    if (!fence) {
      const visualization = parseCodexVisualization(line);
      if (visualization) {
        flushMarkdown();
        parts.push(visualization);
        return;
      }
      const directive = line.match(
        /^::(git-stage|git-commit|git-create-branch|git-push|git-create-pr|created-thread)\{[^\n]*\}\s*$/,
      );
      if (directive) {
        flushMarkdown();
        const name = directive[1];
        parts.push({
          kind: "directive",
          name,
          label: CODEX_DIRECTIVE_LABELS[name],
        });
        return;
      }
    }
    markdown += line + suffix;
    if (!fence && fenceMatch) {
      fence = fenceMatch[1][0] as "`" | "~";
    } else if (fence && fenceMatch?.[1][0] === fence) {
      fence = null;
    }
  });
  flushMarkdown();
  return parts;
}

function CodexVisualizationCard({ path, title, onOpenFile }: {
  path: string;
  title: string;
  onOpenFile?: (path: string, line?: number) => void;
}) {
  const available = !!onOpenFile;
  return <button type="button" className="codex-visualization-card"
    disabled={!available} data-visualization="html"
    title={available ? "在 Remote 中打开可视化" : "当前无法读取可视化文件"}
    onClick={() => onOpenFile?.(path)}>
    <span className="codex-visualization-icon"><Icon name="read" size={17} /></span>
    <span className="codex-visualization-copy">
      <strong>{title}</strong>
      <span>HTML 可视化</span>
    </span>
    <span className="codex-visualization-open">
      {available ? "打开" : "不可用"}
      <Icon name="chevron-right" size={15} />
    </span>
  </button>;
}

function nodeText(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(nodeText).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) return nodeText(node.props.children);
  return "";
}

function CopyableCodeBlock({ children }: { children?: ReactNode }) {
  const [copied, setCopied] = useState(false);
  const code = nodeText(children).replace(/\n$/, "");
  const copy = () => {
    void navigator.clipboard?.writeText(code).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    }).catch(() => {});
  };
  return (
    <div className="message-code-block">
      <button type="button" className={"message-code-copy" + (copied ? " copied" : "")}
        onClick={copy} aria-label="复制代码" title={copied ? "已复制" : "复制代码"}>
        <Icon name={copied ? "check" : "copy"} size={13} />
        <span>{copied ? "已复制" : "复制"}</span>
      </button>
      <pre>{children}</pre>
    </div>
  );
}

interface MessageMarkdownContextValue {
  done?: boolean;
  imageAssets?: Record<string, InlineImageAsset>;
  onLoadImage?: (path: string) => boolean;
  onAuthorizeImage?: (
    authorization: PreviewAuthorizationState,
    decision: "allow" | "deny",
  ) => boolean;
  onOpenFile?: (path: string, line?: number) => void;
  onPreviewImage?: (src: string, alt: string) => void;
}

const MessageMarkdownContext = createContext<MessageMarkdownContextValue>({});
const externalImageDimensions = new Map<string, readonly [number, number]>();
const MAX_EXTERNAL_IMAGE_DIMENSIONS = 128;

function rememberExternalImageDimensions(
  src: string,
  dimensions: readonly [number, number],
): void {
  if (!/^https?:\/\//i.test(src)) return;
  externalImageDimensions.delete(src);
  externalImageDimensions.set(src, dimensions);
  while (externalImageDimensions.size > MAX_EXTERNAL_IMAGE_DIMENSIONS) {
    const oldest = externalImageDimensions.keys().next().value;
    if (typeof oldest !== "string") break;
    externalImageDimensions.delete(oldest);
  }
}

function MessageImage({ src, alt, title, asset, onLoadImage,
  onAuthorizeImage, onPreviewImage }: {
  src: string;
  alt?: string;
  title?: string;
  asset?: InlineImageAsset;
  onLoadImage?: (path: string) => boolean;
  onAuthorizeImage?: (
    authorization: PreviewAuthorizationState,
    decision: "allow" | "deny",
  ) => boolean;
  onPreviewImage?: (src: string, alt: string) => void;
}) {
  const target = useMemo(() => classifyMessageImageTarget(src), [src]);
  const cacheSnapshot = useSyncExternalStore(
    subscribeInlineImageAssetCacheChanges,
    inlineImageAssetCacheSnapshot,
    inlineImageAssetCacheSnapshot,
  );
  const loadAttemptRef = useRef<{
    path: string;
    loader: NonNullable<typeof onLoadImage>;
    snapshot: number;
    waitingForCapacity: boolean;
  } | null>(null);
  const assetObservationRef = useRef<{
    path: string;
    seen: boolean;
  }>({ path: "", seen: false });
  const [blocked, setBlocked] = useState(false);
  const [manualRetryPending, setManualRetryPending] = useState(false);
  const [stalled, setStalled] = useState(false);
  const [naturalSize, setNaturalSize] = useState<{
    src: string;
    width: number;
    height: number;
  } | null>(null);
  const svg = useSanitizedSvgUrl(
    asset?.status === "ready" ? asset.data : undefined,
    asset?.status === "ready" ? asset.mediaType : undefined,
  );

  useEffect(() => {
    if (target.kind !== "local") {
      assetObservationRef.current = { path: "", seen: false };
      loadAttemptRef.current = null;
      setBlocked(false);
      setManualRetryPending(false);
      return;
    }
    if (assetObservationRef.current.path !== target.value) {
      assetObservationRef.current = { path: target.value, seen: false };
      loadAttemptRef.current = null;
      setBlocked(false);
      setManualRetryPending(false);
    }
    if (asset) {
      assetObservationRef.current.seen = true;
      loadAttemptRef.current = null;
      setBlocked(false);
      setManualRetryPending(false);
      return;
    }
    if (!onLoadImage) {
      loadAttemptRef.current = null;
      setBlocked(false);
      setManualRetryPending(false);
      return;
    }
    // Once this mounted image has observed a cache entry, its disappearance is
    // an eviction rather than initial capacity becoming available. Do not
    // automatically reclaim the slot: two visible images in a limit-1 cache
    // would otherwise evict and reload each other forever.
    if (assetObservationRef.current.seen) {
      loadAttemptRef.current = null;
      if (!manualRetryPending) setBlocked(true);
      return;
    }
    const previous = loadAttemptRef.current;
    if (previous?.path === target.value
        && previous.loader === onLoadImage
        && (!previous.waitingForCapacity
          || previous.snapshot === cacheSnapshot)) return;
    const accepted = onLoadImage(target.value);
    loadAttemptRef.current = {
      path: target.value,
      loader: onLoadImage,
      // Consume synchronous begin/cancel publications from this attempt. A
      // rejected begin() does not publish, so only a later real cache mutation
      // can wake this mounted image for another capacity attempt.
      snapshot: inlineImageAssetCacheSnapshot(),
      waitingForCapacity: !accepted,
    };
    setBlocked(!accepted);
  }, [
    asset,
    cacheSnapshot,
    manualRetryPending,
    onLoadImage,
    target,
  ]);
  useEffect(() => {
    setStalled(false);
    if (asset?.status !== "loading") return;
    const elapsed = asset.startedAt == null
      ? 0
      : Math.max(0, Date.now() - asset.startedAt);
    const remaining = Math.max(
      0,
      INLINE_IMAGE_REQUEST_TIMEOUT_MS - elapsed,
    );
    if (remaining === 0) {
      setStalled(true);
      return;
    }
    const timer = window.setTimeout(
      () => setStalled(true),
      remaining,
    );
    return () => window.clearTimeout(timer);
  }, [
    asset?.requestGeneration,
    asset?.startedAt,
    asset?.status,
    target,
  ]);

  const retryLocalImage = () => {
    if (target.kind !== "local" || !onLoadImage) return;
    const accepted = onLoadImage(target.value);
    if (accepted) {
      setManualRetryPending(true);
      setBlocked(false);
      setStalled(false);
    } else {
      setManualRetryPending(false);
      setBlocked(true);
    }
  };

  if (target.kind === "blocked") {
    return <span className="message-image-error">图片不可用</span>;
  }
  if (target.kind === "local" && asset?.authorization) {
    return <PreviewAuthorizationPrompt
      authorization={asset.authorization}
      compact
      onDecision={onAuthorizeImage} />;
  }
  if (target.kind === "local" && asset?.status === "error") {
    return onLoadImage
      ? <button type="button" className="message-image-error"
          title={asset.error} onClick={retryLocalImage}>
          图片加载失败，点击重试
        </button>
      : <span className="message-image-error">图片加载失败</span>;
  }
  if (target.kind === "local" && asset?.status === "loading" && stalled) {
    return onLoadImage
      ? <button type="button" className="message-image-error"
          onClick={retryLocalImage}>
          图片加载超时，点击重试
        </button>
      : <span className="message-image-error">图片加载超时</span>;
  }
  const evicted = target.kind === "local" && !asset
    && assetObservationRef.current.path === target.value
    && assetObservationRef.current.seen;
  if (evicted && !manualRetryPending) {
    return onLoadImage
      ? <button type="button" className="message-image-error"
          onClick={retryLocalImage}>
          图片暂时无法加载，点击重试
        </button>
      : <span className="message-image-error">图片暂时无法加载</span>;
  }
  if (target.kind === "local" && (blocked || (!onLoadImage && !asset))) {
    return <span className="message-image-error">图片暂时无法加载</span>;
  }
  if (svg.error) {
    return <span className="message-image-error">{svg.error}</span>;
  }

  const resolved = target.kind === "external"
    ? target.value
    : asset?.status === "ready" && asset.data && asset.mediaType
      ? asset.mediaType === "image/svg+xml"
        ? svg.url
        : `data:${asset.mediaType};base64,${asset.data}`
      : null;
  if (!resolved) {
    return <span className="message-image-loading" role="status">
      <span className="thinking"><span/><span/><span/></span>
      {alt || "正在加载图片"}
    </span>;
  }

  const cachedSize = externalImageDimensions.get(resolved);
  const dimensions = asset?.width && asset.height
    ? [asset.width, asset.height] as const
    : naturalSize?.src === resolved
      ? [naturalSize.width, naturalSize.height] as const
      : cachedSize;
  const image = <img className="message-inline-image" src={resolved}
    alt={alt || ""} title={title} loading="lazy" decoding="async"
    width={dimensions?.[0]} height={dimensions?.[1]}
    referrerPolicy="no-referrer"
    onLoad={(event) => {
      const width = event.currentTarget.naturalWidth;
      const height = event.currentTarget.naturalHeight;
      if (width <= 0 || height <= 0) return;
      rememberExternalImageDimensions(resolved, [width, height]);
      if (dimensions?.[0] === width && dimensions[1] === height) return;
      setNaturalSize({ src: resolved, width, height });
    }} />;
  if (!onPreviewImage) return image;
  return <button type="button" className="message-image-trigger"
    aria-label={`预览图片${alt ? `：${alt}` : ""}`}
    onClick={() => onPreviewImage(resolved, alt || "图片预览")}>{image}</button>;
}

function MarkdownImage({ src, alt, title }: ComponentPropsWithoutRef<"img">) {
  const {
    imageAssets, onLoadImage, onAuthorizeImage, onPreviewImage,
  } = useContext(MessageMarkdownContext);
  const source = typeof src === "string" ? src : "";
  const target = classifyMessageImageTarget(source);
  const asset = target.kind === "local" ? imageAssets?.[target.value] : undefined;
  return <MessageImage src={source} alt={alt} title={title} asset={asset}
    onLoadImage={onLoadImage} onAuthorizeImage={onAuthorizeImage}
    onPreviewImage={onPreviewImage} />;
}

// Keep local file anchors until MarkdownLink turns them into preview buttons.
// Images and all other schemes retain react-markdown's default URL policy.
const messageUrlTransform: UrlTransform = (url, key, node) => {
  if (key === "href" && node.tagName === "a"
      && /^file:\/\//i.test(url) && parseLocalFileTarget(url)) return url;
  return defaultUrlTransform(url);
};

function MarkdownLink({
  href = "", children, title,
}: ComponentPropsWithoutRef<"a">) {
  const { onOpenFile } = useContext(MessageMarkdownContext);
  const openViewer = useContext(RemoteViewerContext);
  const openPage = useContext(ViewerPagesContext)?.openLink;
  const file = parseLocalFileTarget(href);
  if (file && onOpenFile) {
    const location = file.line ? `${file.path}:${file.line}` : file.path;
    return <LocalFileLink location={location}
      onOpen={() => onOpenFile(file.path, file.line)}>{children}</LocalFileLink>;
  }
  if (/^https?:\/\//i.test(href) || /^mailto:/i.test(href)) {
    return <a href={href} target="_blank" rel="noopener noreferrer"
      onClick={isLocalViewerUrl(href) ? (event) => {
        if (event.defaultPrevented || event.button !== 0
            || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        if (openPage?.(href) || openViewer?.(href)) event.preventDefault();
      } : undefined}
      title={title}>{children}</a>;
  }
  if (href.startsWith("#")) return <a href={href} title={title}>{children}</a>;
  return <span className="message-link-disabled"
    title="该链接无法在当前会话中打开">{children}</span>;
}

function LocalFileLink({ location, children, onOpen }: {
  location: string;
  children: ReactNode;
  onOpen: () => void;
}) {
  const tooltipId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const tooltipRef = useRef<HTMLSpanElement>(null);
  const pathRef = useRef<HTMLSpanElement>(null);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [shown, setShown] = useState(false);
  const [position, setPosition] = useState({ left: 0, top: 0 });

  const cancelClose = () => {
    if (!closeTimer.current) return;
    clearTimeout(closeTimer.current);
    closeTimer.current = null;
  };
  const open = () => {
    cancelClose();
    setShown(true);
  };
  const scheduleClose = () => {
    cancelClose();
    closeTimer.current = setTimeout(() => {
      setShown(false);
      closeTimer.current = null;
    }, 220);
  };

  useEffect(() => () => {
    if (closeTimer.current) clearTimeout(closeTimer.current);
  }, []);

  useLayoutEffect(() => {
    if (!shown) return;
    const update = () => {
      const trigger = triggerRef.current;
      const tooltip = tooltipRef.current;
      if (!trigger || !tooltip) return;
      const anchor = trigger.getBoundingClientRect();
      const box = tooltip.getBoundingClientRect();
      const margin = 10;
      const gap = 8;
      const idealLeft = anchor.left + anchor.width / 2 - box.width / 2;
      const left = Math.min(
        Math.max(margin, idealLeft),
        Math.max(margin, window.innerWidth - box.width - margin),
      );
      const above = anchor.top - box.height - gap;
      const top = above >= margin
        ? above
        : Math.min(window.innerHeight - box.height - margin, anchor.bottom + gap);
      setPosition((current) => current.left === left && current.top === top
        ? current : { left, top });
    };
    update();
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
    };
  }, [shown, location]);

  const selectPath = () => {
    const path = pathRef.current;
    const selection = window.getSelection();
    if (!path || !selection) return;
    const range = document.createRange();
    range.selectNodeContents(path);
    selection.removeAllRanges();
    selection.addRange(range);
  };

  return <>
    <button ref={triggerRef} type="button" className="message-file-link"
      aria-label={`在 Remote 中打开 ${location}`}
      aria-describedby={shown ? tooltipId : undefined}
      onPointerEnter={open} onPointerLeave={scheduleClose}
      onFocus={open} onBlur={scheduleClose}
      onClick={onOpen}>{children}</button>
    {shown && createPortal(
      <span ref={tooltipRef} id={tooltipId} role="tooltip"
        className="message-file-tooltip"
        style={{ left: position.left, top: position.top }}
        onPointerEnter={open} onPointerLeave={scheduleClose}
        onFocus={open} onBlur={scheduleClose}>
        <span ref={pathRef} className="message-file-tooltip-path"
          onDoubleClick={(event) => {
            event.preventDefault();
            selectPath();
          }}>{location}</span>
      </span>,
      document.body,
    )}
  </>;
}

function fenceClassName(children: ReactNode): string | undefined {
  const items = Array.isArray(children) ? children : [children];
  const code = items.find((child) => isValidElement<{ className?: string }>(child));
  return isValidElement<{ className?: string }>(code)
    ? code.props.className
    : undefined;
}

function MarkdownPre({ children }: ComponentPropsWithoutRef<"pre">) {
  const { done } = useContext(MessageMarkdownContext);
  const className = fenceClassName(children);
  if (done && (isMermaidFenceClass(className)
      || isMathFenceClass(className))) return <>{children}</>;
  return <CopyableCodeBlock>{children}</CopyableCodeBlock>;
}

function MarkdownCode({
  className, children,
}: ComponentPropsWithoutRef<"code">) {
  const { done } = useContext(MessageMarkdownContext);
  if (done && isMathFenceClass(className)) {
    return <code className={className}>{children}</code>;
  }
  if (done && isMermaidFenceClass(className)) {
    return <MermaidBlock source={nodeText(children).replace(/\n$/, "")} />;
  }
  return <code className={className}>{children}</code>;
}

/** Module-stable renderer identities keep React from remounting decoded images
 * whenever a streaming block receives a new asset snapshot. */
const MESSAGE_MARKDOWN_COMPONENTS: Components = Object.freeze({
  pre: MarkdownPre,
  code: MarkdownCode,
  img: MarkdownImage,
  a: MarkdownLink,
});
// Streams markdown with a ~50ms throttle: re-parsing react-markdown on every
// token delta is wasteful, so we hold a "shown" buffer that catches up on a
// timer while streaming, and snaps to the full text when the block is done.
export function MessageBlock({ text, done, onOpenFile, imageAssets,
  onLoadImage, onAuthorizeImage, onPreviewImage }: {
  text: string;
  done: boolean;
  onOpenFile?: (path: string, line?: number) => void;
  imageAssets?: Record<string, InlineImageAsset>;
  onLoadImage?: (path: string) => boolean;
  onAuthorizeImage?: (
    authorization: PreviewAuthorizationState,
    decision: "allow" | "deny",
  ) => boolean;
  onPreviewImage?: (src: string, alt: string) => void;
}) {
  const [shown, setShown] = useState(text);
  const latest = useRef(text);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    latest.current = text;
    if (done) {
      if (timer.current) { clearTimeout(timer.current); timer.current = null; }
      setShown(text);
      return;
    }
    if (timer.current) return; // a catch-up is already scheduled
    timer.current = setTimeout(() => {
      setShown(latest.current);
      timer.current = null;
    }, 50);
  }, [text, done]);

  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
    // StrictMode reconnects effects on mount; a canceled timer must not keep
    // subsequent text updates waiting for a callback that will never run.
    timer.current = null;
  }, []);

  const markdownContext = useMemo<MessageMarkdownContextValue>(() => ({
    done, imageAssets, onLoadImage, onAuthorizeImage,
    onOpenFile, onPreviewImage,
  }), [
    done,
    imageAssets,
    onAuthorizeImage,
    onLoadImage,
    onOpenFile,
    onPreviewImage,
  ]);
  const math = useMarkdownMathPlugins(shown, true);
  const extras = useMarkdownExtras(shown);
  const remarkPlugins = useMemo(() => [
    ...(math.plugins?.remarkPlugins ?? STREAMING_REMARK_PLUGINS),
    ...(extras.citation ? [extras.citation] : []),
  ], [extras.citation, math.plugins?.remarkPlugins]);
  const rehypePlugins = useMemo(() => [
    ...(extras.details ? [extras.details] : []),
    ...(math.plugins?.rehypePlugins ?? []),
  ], [extras.details, math.plugins?.rehypePlugins]);
  const parts = useMemo(
    () => splitCodexRichContent(math.normalizedSource),
    [math.normalizedSource],
  );

  if (!shown) return null;
  return (
    <MessageMarkdownContext.Provider value={markdownContext}>
      <div className="prose">
        {parts.map((part, index) => {
          if (part.kind === "markdown") return <MessageMarkdown key={`markdown-${index}`}
              remarkPlugins={remarkPlugins}
              rehypePlugins={rehypePlugins}
              urlTransform={messageUrlTransform}
              components={MESSAGE_MARKDOWN_COMPONENTS}>{part.text}</MessageMarkdown>;
          if (part.kind === "visualization") return <CodexVisualizationCard
            key={`visualization-${index}`} path={part.path} title={part.title}
            onOpenFile={onOpenFile} />;
          return <div key={`directive-${index}`} className="codex-directive-status"
              data-directive={part.name}>
              <Icon name="verify" size={14} />
              <span>{part.label}</span>
            </div>;
        })}
        {!done && <span className="cursor" />}
      </div>
    </MessageMarkdownContext.Provider>
  );
}
