import { PanelFrame, PanelExpandButton } from "./RightPanelPresentation";
import { lazy, Suspense, isValidElement, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState,
  type ComponentPropsWithoutRef, type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import type {
  Artifact,
  PreviewAssetState,
  PreviewAuthorizationState,
} from "../reducer";
import { Icon } from "../icons";
import { ArtifactReadingMemory, rememberArtifactReading } from "../artifact-reading-state";
import { LocalFileLink, WebLink } from "./LinkPreview";
import { PanelTabs, type RightPanelView } from "./PanelTabs";
import { GIT_DIFF_PAGE_LINES, pageGitDiff, type GitDiffSection } from "../diff";
import { classifyPreviewTarget } from "../preview-path";
import { parseLocalFileTarget } from "../file-link";
import {
  buildInteractiveSandboxDocument,
  type HtmlPreviewTheme,
} from "../html-preview";
import { isMermaidFenceClass } from "../mermaid";
import { useSanitizedSvgUrl } from "../use-sanitized-svg";
import {
  isMathFenceClass,
  STREAMING_REMARK_PLUGINS,
  useMarkdownMathPlugins,
} from "../markdown-math";
import { MermaidBlock } from "./MermaidBlock";
import { PreviewAuthorizationPrompt } from "./PreviewAuthorizationPrompt";
import { PanelResizer } from "./PanelResizer";
import { PdfArtifactPreview } from "./PdfArtifactPreview";
import { VideoArtifactPreview } from "./VideoArtifactPreview";
import { AudioArtifactPreview } from "./AudioArtifactPreview";
import { ZoomableImage } from "./ZoomableImage";
import { ArtifactHeader } from "./ArtifactHeader";
import ArtifactDownload from "./ArtifactDownload";
import { previewImageDimension, rehypePreviewHtml } from "../markdown-preview-html";

const EMPTY_GIT_DIFF_SECTIONS: GitDiffSection[] = [];
const MAX_PREVIEW_ASSETS = 12;
const SOURCE_PAGE_LINES = 500;
const URL_ATTRIBUTES = new Set(["src", "href", "xlink:href", "poster", "action", "formaction"]);
const UNSAFE_CSS = /(?:url\s*\(|@import|expression\s*\()/i;

function markdownNodeText(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(markdownNodeText).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) {
    return markdownNodeText(node.props.children);
  }
  return "";
}

function markdownFenceClassName(children: ReactNode): string | undefined {
  const items = Array.isArray(children) ? children : [children];
  const code = items.find((child) => isValidElement<{ className?: string }>(child));
  return isValidElement<{ className?: string }>(code)
    ? code.props.className
    : undefined;
}

function MarkdownPreviewPre({ children }: ComponentPropsWithoutRef<"pre">) {
  const className = markdownFenceClassName(children);
  if (isMermaidFenceClass(className)
      || isMathFenceClass(className)) return <>{children}</>;
  return <pre>{children}</pre>;
}

function MarkdownPreviewCode({
  className, children,
}: ComponentPropsWithoutRef<"code">) {
  if (isMathFenceClass(className)) {
    return <code className={className}>{children}</code>;
  }
  if (isMermaidFenceClass(className)) {
    return <MermaidBlock source={markdownNodeText(children).replace(/\n$/, "")} />;
  }
  return <code className={className}>{children}</code>;
}

const SpreadsheetPreview = lazy(() => import("./SpreadsheetPreview"));

function HtmlArtifactPreview({ content, theme }: {
  content: string;
  theme?: HtmlPreviewTheme;
}) {
  const [interactiveDocument, setInteractiveDocument] =
    useState<string | null>(null);
  const [frameRevision, setFrameRevision] = useState(0);
  const interactiveFrameRef = useRef<HTMLIFrameElement>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const prepare = async () => {
      try {
        const runnable = new DOMParser().parseFromString(content, "text/html");
        for (const element of Array.from(runnable.querySelectorAll(
          "iframe,object,embed,form,base,link,meta[http-equiv],script[src]",
        ))) {
          element.remove();
        }
        for (const element of runnable.documentElement.querySelectorAll("*")) {
          for (const attribute of Array.from(element.attributes)) {
            const name = attribute.name.toLowerCase();
            const value = attribute.value.trim();
            if (name === "srcset" || name === "action"
                || name === "formaction") {
              element.removeAttribute(attribute.name);
            } else if (URL_ATTRIBUTES.has(name)) {
              const allowedAnchor = name === "href" && value.startsWith("#");
              const allowedImage = name === "src"
                && /^data:image\/(?:png|jpeg|gif|webp|avif|svg\+xml);base64,/i
                  .test(value);
              if (!allowedAnchor && !allowedImage) {
                element.removeAttribute(attribute.name);
              }
            } else if (name === "style" && UNSAFE_CSS.test(value)) {
              element.removeAttribute(attribute.name);
            }
          }
        }
        for (const style of runnable.documentElement.querySelectorAll("style")) {
          if (UNSAFE_CSS.test(style.textContent || "")) style.remove();
        }
        if (cancelled) return;
        setInteractiveDocument(buildInteractiveSandboxDocument(
          runnable.body.innerHTML,
          runnable.head.innerHTML,
          theme,
        ));
        setFrameRevision(value => value + 1);
        setError(null);
      } catch {
        if (cancelled) return;
        setInteractiveDocument(null);
        setError("HTML 安全处理失败");
      }
    };
    void prepare();
    return () => { cancelled = true; };
  }, [content, theme]);

  const loadInteractiveDocument = useCallback(() => {
    if (!interactiveDocument) return;
    interactiveFrameRef.current?.contentWindow?.postMessage({
        type: "cc-remote-html-preview",
        document: interactiveDocument,
      }, "*");
  }, [interactiveDocument]);

  if (error) return <div className="preview-error"><Icon name="read" size={18} />{error}</div>;
  if (!interactiveDocument) return <div className="diff-empty"><span className="thinking"><span/><span/><span/></span> 正在准备 HTML…</div>;
  return <div className="artifact-html-stage">
    <iframe key={frameRevision} ref={interactiveFrameRef} className="artifact-html-preview"
          title="HTML 交互预览" sandbox="allow-scripts"
          referrerPolicy="no-referrer" src="/html-preview-runner.html"
          onLoad={loadInteractiveDocument} />
  </div>;
}

function ImageArtifactPreview({ data, mediaType, title }: {
  data?: string;
  mediaType?: string;
  title: string;
}) {
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [failedUrl, setFailedUrl] = useState<string | null>(null);
  const svg = useSanitizedSvgUrl(data, mediaType);

  useEffect(() => {
    if (mediaType === "image/svg+xml") {
      setObjectUrl(null);
      setError(null);
      return;
    }
    if (!data || !mediaType) {
      setObjectUrl(null);
      setError("预览数据不完整");
      return;
    }
    try {
      const binary = window.atob(data);
      const bytes = new Uint8Array(binary.length);
      for (let index = 0; index < binary.length; index += 1) {
        bytes[index] = binary.charCodeAt(index);
      }
      const url = URL.createObjectURL(new Blob([bytes], { type: mediaType }));
      setObjectUrl(url);
      setError(null);
      return () => URL.revokeObjectURL(url);
    } catch {
      setObjectUrl(null);
      setError("预览数据损坏");
    }
  }, [data, mediaType]);

  const resolvedUrl = mediaType === "image/svg+xml" ? svg.url : objectUrl;
  const resolvedError = mediaType === "image/svg+xml" ? svg.error : error;
  // A cached decode can fail before passive effects run. Associate the error
  // with its URL so a source-change effect cannot clear that new failure.
  const decodeError = resolvedUrl && failedUrl === resolvedUrl
    ? "图片无法解码或格式不受支持" : null;
  if (resolvedError || decodeError) return <div className="preview-error"><Icon name="read" size={18} />{resolvedError || decodeError}</div>;
  if (!resolvedUrl) return <div className="diff-empty"><span className="thinking"><span/><span/><span/></span> 正在准备预览…</div>;
  return <ZoomableImage src={resolvedUrl} alt={title}
    onLoad={() => setFailedUrl(current => current === resolvedUrl ? null : current)}
    onError={() => setFailedUrl(resolvedUrl)} />;
}

function SourceFile({ content, targetLine, artifactKey }: {
  content: string;
  targetLine?: number;
  artifactKey: string;
}) {
  const lines = useMemo(() => content.split("\n"), [content]);
  const focusLine = targetLine && targetLine <= lines.length ? targetLine : undefined;
  const initialPage = Math.min(
    Math.max(0, Math.floor(((targetLine || 1) - 1) / SOURCE_PAGE_LINES)),
    Math.max(0, Math.ceil(lines.length / SOURCE_PAGE_LINES) - 1),
  );
  const [pageState, setPageState] = useState({ key: artifactKey, page: initialPage });
  const page = pageState.key === artifactKey ? pageState.page : initialPage;
  const pageCount = Math.max(1, Math.ceil(lines.length / SOURCE_PAGE_LINES));
  const start = page * SOURCE_PAGE_LINES;
  const visible = lines.slice(start, start + SOURCE_PAGE_LINES);
  const targetRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!focusLine || Math.floor((focusLine - 1) / SOURCE_PAGE_LINES) !== page) return;
    const frame = window.requestAnimationFrame(() => {
      targetRef.current?.scrollIntoView({ block: "center" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [artifactKey, focusLine, page]);

  return <>
    {pageCount > 1 && <nav className="source-page-nav" aria-label="源文件分页">
      <button type="button" disabled={page === 0}
        onClick={() => setPageState({ key: artifactKey, page: page - 1 })}>上一页</button>
      <span>{start + 1}–{Math.min(lines.length, start + SOURCE_PAGE_LINES)} / {lines.length} 行</span>
      <button type="button" disabled={page + 1 >= pageCount}
        onClick={() => setPageState({ key: artifactKey, page: page + 1 })}>下一页</button>
    </nav>}
    <div className="source-file">
      {visible.map((text, index) => {
        const line = start + index + 1;
        const focused = line === focusLine;
        return <div key={line} ref={focused ? targetRef : undefined}
          className={"source-line" + (focused ? " focused" : "")}>
          <span className="source-line-no">{line}</span>
          <code>{text || " "}</code>
        </div>;
      })}
    </div>
  </>;
}

function PreviewImage({ markdownPath, src, alt, title, width, height, asset, requestAsset,
  onAuthorizePreview }: {
  markdownPath: string;
  src: string;
  alt?: string;
  title?: string;
  width?: string | number;
  height?: string | number;
  asset?: PreviewAssetState;
  requestAsset: (path: string) => boolean;
  onAuthorizePreview?: (
    authorization: PreviewAuthorizationState,
    decision: "allow" | "deny",
  ) => boolean;
}) {
  const target = classifyPreviewTarget(markdownPath, src);
  const [blocked, setBlocked] = useState(false);
  const svg = useSanitizedSvgUrl(asset?.data, asset?.mediaType);

  useEffect(() => {
    if (target.kind !== "local" || asset?.data || asset?.error
        || asset?.authorization) return;
    setBlocked(!requestAsset(target.value));
  }, [
    asset?.authorization,
    asset?.data,
    asset?.error,
    requestAsset,
    target.kind,
    target.value,
  ]);

  if (target.kind === "external") {
    return <img src={target.value} alt={alt || ""} title={title}
      width={width} height={height}
      loading="lazy" referrerPolicy="no-referrer" />;
  }
  if (target.kind !== "local") {
    return <span className="preview-image-error" title={src}>图片路径不可用：{alt || src}</span>;
  }
  if (asset?.authorization) {
    return <PreviewAuthorizationPrompt
      authorization={asset.authorization}
      compact
      onDecision={onAuthorizePreview} />;
  }
  if (asset?.data && asset.mediaType) {
    if (svg.error) {
      return <span className="preview-image-error">{svg.error}</span>;
    }
    if (asset.mediaType === "image/svg+xml" && !svg.url) {
      return <span className="preview-image-loading"><span className="thinking"><span/><span/><span/></span> {alt || "正在处理 SVG"}</span>;
    }
    return <img src={asset.mediaType === "image/svg+xml"
      ? svg.url!
      : `data:${asset.mediaType};base64,${asset.data}`}
      alt={alt || ""} title={title} width={width} height={height} loading="lazy" />;
  }
  if (asset?.error) {
    return <span className="preview-image-error" title={asset.error}>图片不可用：{alt || src}</span>;
  }
  if (blocked) {
    return <span className="preview-image-error">本页本地图片超过 {MAX_PREVIEW_ASSETS} 张，已停止加载</span>;
  }
  return <span className="preview-image-loading"><span className="thinking"><span/><span/><span/></span> {alt || "正在加载图片"}</span>;
}

export function ArtifactPanel({ artifact, active, hasBtw, onTab, onClose,
  onRefresh, onOpenFile, onLoadPreviewAsset, onAuthorizePreview,
  onSaveMarkdown, onDirtyChange, theme, displayName, machineId, onBack, backLabel }: {
  machineId?: string | null;
  displayName?: string;
  onBack?: () => void;
  backLabel?: string;
  artifact: Artifact;
  active: RightPanelView;
  hasBtw: boolean;
  onTab: (v: RightPanelView) => void;
  onClose: () => void;
  onRefresh?: (path: string, line?: number) => void;
  onOpenFile?: (path: string, line?: number) => void;
  onLoadPreviewAsset?: (path: string, previewId: string) => boolean;
  onAuthorizePreview?: (
    authorization: PreviewAuthorizationState,
    decision: "allow" | "deny",
  ) => boolean;
  onSaveMarkdown?: (path: string, content: string, expectedSize: number,
    expectedMtimeNs: string, expectedRevision: string) => string | null;
  onDirtyChange?: (dirty: boolean) => void;
  theme?: HtmlPreviewTheme;
}) {
  const editorRef = useRef<HTMLTextAreaElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const readingMemory = useContext(ArtifactReadingMemory);
  const artifactKey = `${artifact.sid || ""}:${artifact.file}:${artifact.requestId || ""}`;
  const remembered = readingMemory?.get(artifactKey);
  const restoringSource = useRef(remembered?.mode === "source");
  const [pageState, setPageState] = useState({ key: artifactKey, page: remembered?.page ?? 0 });
  const [modeState, setModeState] = useState<{ key: string; mode: "preview" | "source" }>({
    key: artifactKey, mode: remembered?.mode ?? "preview",
  });
  const [editorState, setEditorState] = useState({
    key: artifactKey,
    draft: artifact.content || "",
    baseline: artifact.content || "",
  });
  const requestedAssets = useRef<{
    key: string;
    paths: Set<string>;
    queued: string[];
    active?: string;
  }>({
    key: artifactKey, paths: new Set(), queued: [],
  });
  if (requestedAssets.current.key !== artifactKey) {
    requestedAssets.current = {
      key: artifactKey, paths: new Set(), queued: [],
    };
  }

  const requestedPage = pageState.key === artifactKey ? pageState.page : 0;
  const mode = modeState.key === artifactKey ? modeState.mode : "preview";
  const editor = editorState.key === artifactKey ? editorState : {
    key: artifactKey,
    draft: artifact.content || "",
    baseline: artifact.content || "",
  };
  const math = useMarkdownMathPlugins(
    editor.draft,
    artifact.kind === "md" && mode === "preview",
  );
  const markdownRehypePlugins = useMemo(() => [
    rehypePreviewHtml, ...(math.plugins?.rehypePlugins ?? []),
  ], [math.plugins?.rehypePlugins]);
  const dirty = artifact.kind === "md" && editor.draft !== editor.baseline;
  const sections = artifact.kind === "gitdiff"
    ? (artifact.sections || EMPTY_GIT_DIFF_SECTIONS) : EMPTY_GIT_DIFF_SECTIONS;
  const page = useMemo(() => pageGitDiff(sections, requestedPage), [sections, requestedPage]);
  const showPage = (nextPage: number) => {
    rememberArtifactReading(readingMemory, artifactKey, { page: nextPage, top: 0 });
    setPageState({ key: artifactKey, page: nextPage });
  };
  const showMode = (nextMode: "preview" | "source") => {
    rememberArtifactReading(readingMemory, artifactKey, { mode: nextMode });
    setModeState({ key: artifactKey, mode: nextMode });
  };
  const loading = !!artifact.loading;
  const empty = artifact.kind === "gitdiff" && !loading && sections.length === 0;

  useLayoutEffect(() => {
    if (loading) return;
    const saved = readingMemory?.get(artifactKey);
    if (!saved) return;
    if (bodyRef.current) {
      bodyRef.current.scrollTop = saved.top;
      bodyRef.current.scrollLeft = saved.left;
    }
    if (editorRef.current) editorRef.current.scrollTop = saved.sourceTop;
  }, [artifactKey, loading, mode, readingMemory]);

  useLayoutEffect(() => {
    const body = bodyRef.current;
    const source = editorRef.current;
    // Scroll events can still be queued when a keyboard switch unmounts us.
    // Capture synchronously before React removes the old reading surface.
    return () => {
      if (!body) return;
      rememberArtifactReading(readingMemory, artifactKey, {
        top: body.scrollTop, left: body.scrollLeft,
        ...(source ? { sourceTop: source.scrollTop } : {}),
      });
    };
  }, [artifactKey, mode, readingMemory]);

  useEffect(() => {
    const incoming = artifact.content || "";
    setEditorState((current) => {
      if (current.key !== artifactKey) {
        return { key: artifactKey, draft: incoming, baseline: incoming };
      }
      if (artifact.saveStatus === "saved" || current.draft === current.baseline) {
        if (current.draft === incoming && current.baseline === incoming) return current;
        return { key: artifactKey, draft: incoming, baseline: incoming };
      }
      return current;
    });
  }, [artifact.content, artifact.saveStatus, artifactKey]);

  useEffect(() => {
    onDirtyChange?.(dirty);
    return () => onDirtyChange?.(false);
  }, [dirty, onDirtyChange]);

  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  useEffect(() => {
    if (artifact.kind !== "md" || mode !== "source" || loading) return;
    if (restoringSource.current) { restoringSource.current = false; return; }
    const frame = window.requestAnimationFrame(() => editorRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [artifact.kind, loading, mode]);

  const canSave = artifact.kind === "md" && !loading && !artifact.error
    && artifact.writable !== false
    && !artifact.truncated && typeof artifact.size === "number"
    && typeof artifact.mtimeNs === "string" && !!artifact.revision
    && !!onSaveMarkdown;
  const saveDraft = useCallback(() => {
    if (!canSave || !dirty || artifact.saving || !artifact.revision) return;
    onSaveMarkdown?.(
      artifact.file,
      editor.draft,
      artifact.size!,
      artifact.mtimeNs!,
      artifact.revision,
    );
  }, [artifact.file, artifact.mtimeNs, artifact.revision, artifact.size,
    artifact.saving, canSave, dirty, editor.draft, onSaveMarkdown]);

  const confirmDiscard = useCallback(() => (
    !dirty || window.confirm("Markdown 有未保存的修改，确定放弃吗？")
  ), [dirty]);

  const leavePanel = useCallback(() => {
    if (!confirmDiscard()) return;
    onDirtyChange?.(false);
    onClose();
  }, [confirmDiscard, onClose, onDirtyChange]);

  const switchPanelTab = useCallback((next: RightPanelView) => {
    if (next !== active && !confirmDiscard()) return;
    if (next !== active) onDirtyChange?.(false);
    onTab(next);
  }, [active, confirmDiscard, onDirtyChange, onTab]);

  const handlePanelKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (artifact.kind !== "md" || !(event.metaKey || event.ctrlKey)
        || event.key.toLowerCase() !== "s") return;
    event.preventDefault();
    saveDraft();
  };

  const sendNextAsset = useCallback(() => {
    const current = requestedAssets.current;
    if (current.key !== artifactKey || current.active
        || !artifact.requestId || !onLoadPreviewAsset) return;
    while (current.queued.length) {
      const path = current.queued.shift()!;
      if (onLoadPreviewAsset(path, artifact.requestId)) {
        current.active = path;
        return;
      }
      current.paths.delete(path);
    }
  }, [artifact.requestId, artifactKey, onLoadPreviewAsset]);

  useEffect(() => {
    const current = requestedAssets.current;
    if (current.key !== artifactKey) return;
    if (current.active && artifact.assets?.[current.active]) {
      current.active = undefined;
    }
    sendNextAsset();
  }, [artifact.assets, artifactKey, sendNextAsset]);

  const requestAsset = useCallback((path: string): boolean => {
    const current = requestedAssets.current;
    if (current.key !== artifactKey || !artifact.requestId
        || !onLoadPreviewAsset) return false;
    if (current.paths.has(path)) return true;
    if (current.paths.size >= MAX_PREVIEW_ASSETS) return false;
    current.paths.add(path);
    current.queued.push(path);
    sendNextAsset();
    return current.paths.has(path);
  }, [artifact.requestId, artifactKey, onLoadPreviewAsset, sendNextAsset]);

  const markdownComponents = useMemo<Components>(() => ({
    pre: MarkdownPreviewPre,
    code: MarkdownPreviewCode,
    img: ({ src, alt, title, width, height }) => {
      const source = typeof src === "string" ? src : "";
      const target = classifyPreviewTarget(artifact.file, source);
      const asset = target.kind === "local" ? artifact.assets?.[target.value] : undefined;
      return <PreviewImage markdownPath={artifact.file} src={source} alt={alt}
        title={title} width={previewImageDimension(width)} height={previewImageDimension(height)}
        asset={asset} requestAsset={requestAsset}
        onAuthorizePreview={onAuthorizePreview} />;
    },
    a: ({ href, children, title, id }) => {
      const target = classifyPreviewTarget(artifact.file, href || "");
      if (target.kind === "external") {
        return <WebLink href={target.value} title={title} id={id}>{children}</WebLink>;
      }
      if (target.kind === "anchor") return <a href={target.value} title={title} id={id}>{children}</a>;
      if (target.kind === "local" && onOpenFile) {
        const source = parseLocalFileTarget(href || "");
        return <LocalFileLink id={id} location={source?.line ? `${target.value}:${source.line}` : target.value}
          onOpen={() => onOpenFile(target.value, source?.line)}>{children}</LocalFileLink>;
      }
      return <span id={id} className="preview-link-disabled" title="该相对链接不会离开当前工作目录">{children}</span>;
    },
  }), [
    artifact.assets,
    artifact.file,
    onAuthorizePreview,
    onOpenFile,
    requestAsset,
  ]);

  const title = displayName || artifact.file.split("/").pop()
    || (["md", "file", "html", "image", "pdf", "audio", "video", "spreadsheet"].includes(artifact.kind) ? "文件预览" : "改动");
  const mermaidFile = artifact.kind === "file" && /\.(?:mmd|mermaid)$/i.test(artifact.file);
  const renderedArtifact = ["image", "pdf", "audio", "video"].includes(artifact.kind)
    || (artifact.kind === "html" && mode === "preview");
  const tabs = hasBtw ? <PanelTabs active={active}
    artifactKind={artifact.kind} hasArtifact hasBtw={hasBtw} onTab={switchPanelTab} /> : null;
  const controlsUnavailable = loading || !!artifact.error || !!artifact.authorization;
  // The optimistic file request only distinguishes Markdown from generic files.
  // Reserve HTML controls before the device confirms the preview format.
  const htmlFile = artifact.kind === "html" || (controlsUnavailable
    && artifact.kind === "file" && /\.html?$/i.test(artifact.file));
  const modes = (artifact.kind === "md" || htmlFile || mermaidFile)
    ? <div className="preview-modes" role="group"
        aria-label={`${mermaidFile ? "Mermaid" : htmlFile ? "HTML" : "Markdown"} 显示模式`}>
        <button className={mode === "preview" ? "on" : ""} aria-pressed={mode === "preview"}
          disabled={controlsUnavailable} onClick={() => showMode("preview")}>预览</button>
        <button className={mode === "source" ? "on" : ""} aria-pressed={mode === "source"}
          disabled={controlsUnavailable} onClick={() => showMode("source")}>源码</button>
      </div> : null;
  const actions = <>
    {artifact.kind === "md" && <button
      type="button" className="markdown-save"
      disabled={!dirty || artifact.saving || !canSave} onClick={saveDraft}
      title={artifact.writable === false ? "此文件仅获准查看"
        : artifact.truncated ? "截断的文件不可编辑" : "保存 Markdown（Ctrl/⌘+S）"}>
      <Icon name={artifact.saving ? "refresh" : "check"} size={15} />
      {artifact.saving ? "保存中" : "保存"}
    </button>}
    {artifact.kind === "md" && artifact.saveStatus === "saved" && !dirty
      && <span className="markdown-save-state ok">已保存</span>}
    {["md", "file", "html", "image", "pdf"].includes(artifact.kind)
      && <ArtifactDownload id={artifactKey} file={displayName || artifact.file} data={artifact.data}
        content={artifact.content} converted={artifact.convertedFrom}
        unavailable={loading ? "正在读取文件…" : artifact.authorization ? "授权后可下载"
          : artifact.error ? "文件读取失败" : artifact.truncated ? "截断的文件不可下载" : undefined} />}
    <PanelExpandButton label="文件预览" />
  </>;

  return (
    <PanelFrame className="artifact-panel" label={artifact.kind === "gitdiff" ? "改动" : "文件预览"}
      onKeyDown={handlePanelKeyDown}>
      <PanelResizer ariaLabel="调整文件面板宽度" />
      {artifact.kind === "gitdiff" ? <div className="artifact-head">
        {tabs || <span className="artifact-title">{title}</span>}
        <span className="artifact-path" title={artifact.file}>{artifact.file || "所有改动"}</span>
        <PanelExpandButton label="改动" />
        <button className="iconbtn" onClick={leavePanel} aria-label="收起"><Icon name="chevrons-right" /></button>
      </div> : <ArtifactHeader key={artifactKey} title={title} path={artifact.file}
        onBack={onBack} backLabel={backLabel} onClose={leavePanel}
        onReload={onRefresh ? () => onRefresh(artifact.file, artifact.line) : undefined}
        reloadDisabled={loading || artifact.saving} convertedFrom={artifact.convertedFrom}
        tabs={tabs} modes={modes} actions={actions} />}
      <div ref={bodyRef} onScrollCapture={event => {
        const target = event.target;
        if (!(target instanceof HTMLElement)) return;
        if (target === bodyRef.current) rememberArtifactReading(readingMemory, artifactKey,
          { top: target.scrollTop, left: target.scrollLeft });
        else if (target === editorRef.current) rememberArtifactReading(readingMemory, artifactKey,
          { sourceTop: target.scrollTop });
      }} className={`artifact-body${renderedArtifact ? " rendered-artifact-body" : ""}${
        artifact.kind === "md" && mode === "source"
          ? " source-artifact-body" : ""
      }`}>
        {artifact.authorization ? (
          <PreviewAuthorizationPrompt
            authorization={artifact.authorization}
            onDecision={onAuthorizePreview} />
        ) : loading ? (
          <div className="diff-empty"><span className="thinking"><span/><span/><span/></span> {["md", "file", "html", "image", "pdf", "audio", "video", "spreadsheet"].includes(artifact.kind) ? "正在读取文件…" : "正在读取 diff…"}</div>
        ) : artifact.error ? (
          <div className="preview-error"><Icon name="read" size={18} />{artifact.error}</div>
        ) : artifact.kind === "gitdiff" ? (
          empty ? (
            <div className="diff-empty">没有未提交的改动。</div>
          ) : (
            <>
              {page.totalLines > GIT_DIFF_PAGE_LINES && (
                <nav className="diff-page-nav" aria-label="Diff 分页">
                  <button type="button" disabled={page.page === 0}
                    onClick={() => showPage(page.page - 1)}>上一页</button>
                  <span>{page.startLine + 1}–{page.endLine} / {page.totalLines} 行</span>
                  <button type="button" disabled={page.page + 1 >= page.pageCount}
                    onClick={() => showPage(page.page + 1)}>下一页</button>
                </nav>
              )}
              <div className="diff-table">
                {page.sections.map((s, si) => (
                  <div className="diff-file" key={si}>
                    <div className="diff-file-h" title={s.file}>
                      <Icon name="edit" size={13} />
                      <span className="diff-file-nm">{s.file}</span>
                    </div>
                    {s.hunks.map((h, hi) => (
                      <div className="diff-hunk" key={hi}>
                        <div className="diff-hunk-h">{h.header}</div>
                        {h.lines.map((l, li) => (
                          <div className={"drow " + l.type} key={li}>
                            <span className="dno">{l.oldNo ?? ""}</span>
                            <span className="dno">{l.newNo ?? ""}</span>
                            <span className="dline">{l.text || " "}</span>
                          </div>
                        ))}
                      </div>
                    ))}
                  </div>
                ))}
              </div>
            </>
          )
        ) : artifact.kind === "diff" ? (
          <pre className="diff-pre">
            {artifact.diff?.map((l, i) => (
              <span key={i} className={"diff-" + l.type}>{(l.type === "add" ? "+" : l.type === "del" ? "−" : " ") + " " + l.text + "\n"}</span>
            ))}
          </pre>
        ) : artifact.kind === "html" ? (
          mode === "source"
            ? <SourceFile content={artifact.content || ""} artifactKey={artifactKey} />
            : <HtmlArtifactPreview content={artifact.content || ""} theme={theme} />
        ) : artifact.kind === "spreadsheet" ? (
          <Suspense fallback={<div className="diff-empty">读取表格…</div>}><SpreadsheetPreview key={artifactKey} content={artifact.content || ""} data={artifact.data} title={title} /></Suspense>
        ) : artifact.kind === "image" ? (
          <ImageArtifactPreview data={artifact.data} mediaType={artifact.mediaType}
            title={title} />
        ) : artifact.kind === "pdf" ? (
          <PdfArtifactPreview data={artifact.data} title={title} />
        ) : artifact.kind === "audio" ? (
          <AudioArtifactPreview key={artifactKey} data={artifact.data}
            mediaType={artifact.mediaType} title={title} size={artifact.size} />
        ) : artifact.kind === "video" ? (
          <VideoArtifactPreview machineId={machineId} mediaId={artifact.mediaId}
            title={title} size={artifact.size} />
        ) : artifact.kind === "file" ? (
          <>
            {artifact.truncated && <div className="preview-truncated">文件共 {artifact.size?.toLocaleString()} 字节，仅预览前 512 KiB。</div>}
            {mermaidFile && mode === "preview" && !artifact.truncated
              ? <MermaidBlock source={artifact.content || ""} />
              : <SourceFile content={artifact.content || ""} targetLine={artifact.line}
                  artifactKey={artifactKey} />}
          </>
        ) : artifact.kind === "md" ? (
          <>
            {artifact.truncated && <div className="preview-truncated">文件共 {artifact.size?.toLocaleString()} 字节，仅预览前 512 KiB。</div>}
            {artifact.saveError && <div className={"markdown-save-error " + (artifact.saveStatus || "error")}>
              {artifact.saveError}
            </div>}
            {mode === "source"
              ? <textarea ref={editorRef} className="markdown-editor"
                  aria-label="Markdown 源码编辑器" value={editor.draft}
                  readOnly={!canSave}
                  spellCheck={false}
                  onChange={(event) => setEditorState({
                    key: artifactKey,
                    draft: event.currentTarget.value,
                    baseline: editor.baseline,
                  })} />
              : <div className="prose markdown-preview"><ReactMarkdown
                  remarkPlugins={
                    math.plugins?.remarkPlugins ?? STREAMING_REMARK_PLUGINS}
                  rehypePlugins={markdownRehypePlugins}
                  components={markdownComponents}>
                  {math.normalizedSource}
                </ReactMarkdown></div>}
          </>
        ) : null}
      </div>
    </PanelFrame>
  );
}
