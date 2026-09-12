import { useEffect, useRef, useState } from "react";
import type { Turn } from "../domain/conversation";
import type { TurnFileChange } from "../protocol";
import type { LoadTurnFilePage } from "../turn-file-pages";
import { collectTurnFileChanges } from "../file-changes";
import { isMarkdownPath } from "../preview-path";
import { Icon } from "../icons";
import { parseGitDiff } from "../diff";
import { presentChangedPaths } from "../file-presentation";

interface TurnChangesProps {
  turn: Turn; open: boolean; onToggle: () => void;
  onLoadFilePage?: LoadTurnFilePage;
  onBeforeLoad?: () => void;
  onOpenDiff?: (turnId: string, revision: string, path: string) => void;
  onOpenLegacyDiff?: (files: string[], diff: string) => void;
  onPreviewMarkdown?: (path: string) => void;
  work: boolean; onOpenFile?: (path: string) => void; onOpenArtifacts?: () => void;
}

export function TurnChangesPanel(props: TurnChangesProps) {
  return <TurnChangesRevision key={`${props.turn.fileChangesTurnId ?? props.turn.historyTurnId ?? props.turn.id}:${props.turn.fileChanges?.revision ?? "legacy"}`} {...props} />;
}

function TurnChangesRevision({ turn, open, onToggle, onOpenDiff, onOpenLegacyDiff,
  onPreviewMarkdown, work, onOpenFile, onOpenArtifacts, onLoadFilePage, onBeforeLoad }: TurnChangesProps) {
  const summary = turn.fileChanges;
  const [extraFiles, setExtraFiles] = useState<TurnFileChange[]>([]);
  const [nextOffset, setNextOffset] = useState(summary?.next_offset ?? null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => { pending.current?.abort(); }, []);
  const legacy = summary ? { paths: [], diff: "" } : collectTurnFileChanges([
    ...turn.blocks, ...(turn.liveSpillBlocks ?? []), ...(turn.detailProjection?.blocks ?? []),
  ]);
  const files: TurnFileChange[] = summary ? [...summary.files, ...extraFiles]
    : legacy.paths.map((path) => ({ path, state: "unavailable" as const }));
  if (!files.length) return null;
  const presentations = presentChangedPaths(files.map((file) => file.path));
  const legacyPaths = new Set(legacy.diff ? parseGitDiff(legacy.diff).map((section) => section.file) : []);
  const available = files.filter((file) => file.state === "available");
  const added = summary?.total_additions ?? available.reduce((n, file) => n + (file.additions ?? 0), 0);
  const removed = summary?.total_deletions ?? available.reduce((n, file) => n + (file.deletions ?? 0), 0);
  const total = summary?.total_files ?? files.length;
  const loadMore = async () => {
    if (!summary || nextOffset === null || !onLoadFilePage || pending.current) return;
    const controller = new AbortController();
    pending.current = controller;
    onBeforeLoad?.();
    setLoading(true);
    setError(null);
    try {
      const page = await onLoadFilePage(turn.fileChangesTurnId ?? turn.historyTurnId ?? turn.id,
        summary.revision, nextOffset, controller.signal);
      if (controller.signal.aborted) return;
      if (page.total_files !== total || page.offset !== files.length
          || page.files.some((file) => files.some((existing) => existing.path === file.path))) {
        throw new Error("文件清单已更新，请刷新本轮后重试。");
      }
      setExtraFiles((previous) => [...previous, ...page.files]);
      setNextOffset(page.next_offset ?? null);
    } catch (cause) {
      if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "加载失败，请重试。");
    } finally {
      if (!controller.signal.aborted) {
        pending.current = null;
        setLoading(false);
      }
    }
  };
  return <div className="turn-changes">
    <button type="button" className="turn-changes-toggle" onClick={onToggle} aria-expanded={open}>
      <Icon name="chev" size={14} /><span>{work ? "Artifacts" : "改动"} {total}{summary?.truncated ? "+" : ""} 个文件</span>
      {summary?.truncated && <small>部分记录</small>}
      {!work && (available.length > 0 || added > 0 || removed > 0) && <span className="turn-changes-counts">
        <span>+{added}</span><span>−{removed}</span>
      </span>}
    </button>
    {open && <div className="turn-changes-files">
      {files.map((file, index) => {
        const presentation = presentations[index];
        const canOpen = work ? !!onOpenFile : summary
          ? file.state === "available" && !!onOpenDiff : legacyPaths.has(file.path) && !!onOpenLegacyDiff;
        const reason = "reason" in file ? file.reason : undefined;
        return <div className="turn-change-file" key={file.path}>
          <button type="button" className="turn-change-path" disabled={!canOpen}
            aria-label={file.path} title={reason ? `${file.path}\n${reason}` : file.path}
            onClick={() => {
              if (work) onOpenFile?.(file.path);
              else if (summary) onOpenDiff?.(turn.fileChangesTurnId ?? turn.historyTurnId ?? turn.id, summary.revision, file.path);
              else onOpenLegacyDiff?.([file.path], legacy.diff);
            }}>
            <span className="turn-change-type" data-tone={presentation.type.tone}
              role="img" aria-label={presentation.type.label} title={presentation.type.label}>
              {presentation.type.badge || <Icon name="read" size={16} />}
            </span>
            <span className="turn-change-label"><span className="turn-change-name">{presentation.name}</span>
              {presentation.directory && <span className="turn-change-directory">{presentation.directory}</span>}
            </span>
          </button>
          {!work && file.state === "available" && "additions" in file && (file.additions || file.deletions) ?
            <span className="turn-changes-counts" aria-label={`新增 ${file.additions ?? 0} 行，删除 ${file.deletions ?? 0} 行`}>
              <span>+{file.additions ?? 0}</span><span>−{file.deletions ?? 0}</span>
            </span> : null}
          {!work && file.state === "pending" && <small>修改中</small>}
          {!work && file.state === "unavailable" && <small title={reason || "历史原生差异未完整保存"}>{!summary && canOpen ? "原始修改记录" : "差异未保存"}</small>}
          {!work && file.state === "available" && "additions" in file && file.additions === 0 && file.deletions === 0 && <small>无净改动</small>}
          {!work && onPreviewMarkdown && isMarkdownPath(file.path) && <button type="button"
            className="turn-change-preview" onClick={() => onPreviewMarkdown(file.path)}>预览当前文件</button>}
        </div>;
      })}
      {nextOffset !== null && <div className="turn-changes-more">
        <span>已显示 {files.length} / {total} 个文件</span>
        <button type="button" className="turn-change-preview" disabled={loading || !onLoadFilePage}
          onClick={() => { void loadMore(); }}>{loading ? "加载中…" : error ? "重试加载" : "加载更多"}</button>
        {error && <small role="alert">{error}</small>}
      </div>}
      {work && onOpenArtifacts && <button type="button" className="turn-change-preview"
        onClick={onOpenArtifacts}>查看 Artifacts</button>}
    </div>}
  </div>;
}
