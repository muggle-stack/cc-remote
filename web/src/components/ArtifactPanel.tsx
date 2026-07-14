import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Artifact, PreviewAssetState } from "../reducer";
import { Icon } from "../icons";
import { PanelTabs } from "./PanelTabs";
import { GIT_DIFF_PAGE_LINES, pageGitDiff, type GitDiffSection } from "../diff";
import { classifyPreviewTarget } from "../preview-path";
import { parseLocalFileTarget } from "../file-link";

const EMPTY_GIT_DIFF_SECTIONS: GitDiffSection[] = [];
const MAX_PREVIEW_ASSETS = 12;
const SOURCE_PAGE_LINES = 500;

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

function PreviewImage({ markdownPath, src, alt, title, asset, requestAsset }: {
  markdownPath: string;
  src: string;
  alt?: string;
  title?: string;
  asset?: PreviewAssetState;
  requestAsset: (path: string) => boolean;
}) {
  const target = classifyPreviewTarget(markdownPath, src);
  const [blocked, setBlocked] = useState(false);

  useEffect(() => {
    if (target.kind !== "local" || asset?.data || asset?.error) return;
    setBlocked(!requestAsset(target.value));
  }, [asset?.data, asset?.error, requestAsset, target.kind, target.value]);

  if (target.kind === "external") {
    return <img src={target.value} alt={alt || ""} title={title}
      loading="lazy" referrerPolicy="no-referrer" />;
  }
  if (target.kind !== "local") {
    return <span className="preview-image-error" title={src}>图片路径不可用：{alt || src}</span>;
  }
  if (asset?.data && asset.mediaType) {
    return <img src={`data:${asset.mediaType};base64,${asset.data}`}
      alt={alt || ""} title={title} loading="lazy" />;
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
  onRefresh, onOpenFile, onLoadPreviewAsset }: {
  artifact: Artifact;
  active: "diff" | "btw";
  hasBtw: boolean;
  onTab: (v: "diff" | "btw") => void;
  onClose: () => void;
  onRefresh?: (path: string, line?: number) => void;
  onOpenFile?: (path: string, line?: number) => void;
  onLoadPreviewAsset?: (path: string, previewId: string) => boolean;
}) {
  const artifactKey = `${artifact.sid || ""}:${artifact.file}:${artifact.requestId || ""}`;
  const [pageState, setPageState] = useState({ key: artifactKey, page: 0 });
  const [modeState, setModeState] = useState<{ key: string; mode: "preview" | "source" }>({
    key: artifactKey, mode: "preview",
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
  const sections = artifact.kind === "gitdiff"
    ? (artifact.sections || EMPTY_GIT_DIFF_SECTIONS) : EMPTY_GIT_DIFF_SECTIONS;
  const page = useMemo(() => pageGitDiff(sections, requestedPage), [sections, requestedPage]);
  const showPage = (nextPage: number) => setPageState({ key: artifactKey, page: nextPage });
  const loading = !!artifact.loading;
  const empty = artifact.kind === "gitdiff" && !loading && sections.length === 0;

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
    img: ({ src, alt, title }) => {
      const source = typeof src === "string" ? src : "";
      const target = classifyPreviewTarget(artifact.file, source);
      const asset = target.kind === "local" ? artifact.assets?.[target.value] : undefined;
      return <PreviewImage markdownPath={artifact.file} src={source} alt={alt}
        title={title} asset={asset} requestAsset={requestAsset} />;
    },
    a: ({ href, children, title }) => {
      const target = classifyPreviewTarget(artifact.file, href || "");
      if (target.kind === "external") {
        return <a href={target.value} target="_blank" rel="noopener noreferrer"
          title={title}>{children}</a>;
      }
      if (target.kind === "anchor") return <a href={target.value} title={title}>{children}</a>;
      if (target.kind === "local" && onOpenFile) {
        const source = parseLocalFileTarget(href || "");
        return <a href="#" title={target.value} onClick={(event) => {
          event.preventDefault();
          onOpenFile(target.value, source?.line);
        }}>{children}</a>;
      }
      return <span className="preview-link-disabled" title="该相对链接不会离开当前工作目录">{children}</span>;
    },
  }), [artifact.assets, artifact.file, onOpenFile, requestAsset]);

  const title = artifact.file.split("/").pop()
    || (["md", "file"].includes(artifact.kind) ? "文件预览" : "改动");

  return (
    <div className="artifact-panel">
      <div className="artifact-head">
        {hasBtw ? <PanelTabs active={active} artifactKind={artifact.kind} onTab={onTab} />
          : <span className="artifact-title">{title}</span>}
        <span className="artifact-path" title={artifact.file}>{artifact.file || "所有改动"}</span>
        {artifact.kind === "md" && !loading && !artifact.error && <div className="preview-modes" role="group" aria-label="Markdown 显示模式">
          <button className={mode === "preview" ? "on" : ""}
            onClick={() => setModeState({ key: artifactKey, mode: "preview" })}>预览</button>
          <button className={mode === "source" ? "on" : ""}
            onClick={() => setModeState({ key: artifactKey, mode: "source" })}>源码</button>
        </div>}
        {["md", "file"].includes(artifact.kind) && <button className="iconbtn"
          onClick={() => onRefresh?.(artifact.file, artifact.line)}
          aria-label="刷新文件" title="重新读取文件"><Icon name="refresh" size={17} /></button>}
        <button className="iconbtn" onClick={onClose} aria-label="收起"><Icon name="chevrons-right" /></button>
      </div>
      <div className="artifact-body">
        {loading ? (
          <div className="diff-empty"><span className="thinking"><span/><span/><span/></span> {["md", "file"].includes(artifact.kind) ? "正在读取文件…" : "正在读取 diff…"}</div>
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
        ) : artifact.kind === "file" ? (
          <>
            {artifact.truncated && <div className="preview-truncated">文件共 {artifact.size?.toLocaleString()} 字节，仅预览前 512 KiB。</div>}
            <SourceFile content={artifact.content || ""} targetLine={artifact.line}
              artifactKey={artifactKey} />
          </>
        ) : artifact.kind === "md" ? (
          <>
            {artifact.truncated && <div className="preview-truncated">文件共 {artifact.size?.toLocaleString()} 字节，仅预览前 512 KiB。</div>}
            {mode === "source"
              ? <pre className="markdown-source">{artifact.content || ""}</pre>
              : <div className="prose markdown-preview"><ReactMarkdown
                  remarkPlugins={[remarkGfm]} components={markdownComponents}>{artifact.content || ""}</ReactMarkdown></div>}
          </>
        ) : null}
      </div>
    </div>
  );
}
