import { useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Artifact } from "../reducer";
import { Icon } from "../icons";
import { PanelTabs } from "./PanelTabs";
import { GIT_DIFF_PAGE_LINES, pageGitDiff, type GitDiffSection } from "../diff";

const EMPTY_GIT_DIFF_SECTIONS: GitDiffSection[] = [];

export function ArtifactPanel({ artifact, active, hasBtw, onTab, onClose }: {
  artifact: Artifact; active: "diff" | "btw"; hasBtw: boolean;
  onTab: (v: "diff" | "btw") => void; onClose: () => void;
}) {
  const artifactKey = `${artifact.sid || ""}:${artifact.file}`;
  const [pageState, setPageState] = useState({ key: artifactKey, page: 0 });
  const requestedPage = pageState.key === artifactKey ? pageState.page : 0;
  const sections = artifact.kind === "gitdiff"
    ? (artifact.sections || EMPTY_GIT_DIFF_SECTIONS) : EMPTY_GIT_DIFF_SECTIONS;
  const page = useMemo(() => pageGitDiff(sections, requestedPage), [sections, requestedPage]);
  const showPage = (nextPage: number) => setPageState({ key: artifactKey, page: nextPage });
  const loading = !!artifact.loading && sections.length === 0;
  const empty = artifact.kind === "gitdiff" && !loading && sections.length === 0;

  return (
    <div className="artifact-panel">
      <div className="artifact-head">
        {hasBtw ? <PanelTabs active={active} onTab={onTab} />
          : <span className="artifact-title">{artifact.file.split("/").pop() || "改动"}</span>}
        <span className="artifact-path" title={artifact.file}>{artifact.file || "所有改动"}</span>
        <button className="iconbtn" onClick={onClose} aria-label="收起"><Icon name="chevrons-right" /></button>
      </div>
      <div className="artifact-body">
        {loading ? (
          <div className="diff-empty"><span className="thinking"><span/><span/><span/></span> 正在读取 diff…</div>
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
        ) : (
          <div className="prose"><ReactMarkdown remarkPlugins={[remarkGfm]}>{artifact.content || ""}</ReactMarkdown></div>
        )}
      </div>
    </div>
  );
}
