import { Icon } from "../icons";
import type { WorkArtifactInfo } from "../protocol";

interface Props {
  open: boolean;
  artifacts: WorkArtifactInfo[];
  onOpen: (path: string) => void;
  onClose: () => void;
}

const LABELS: Record<WorkArtifactInfo["kind"], string> = {
  document: "文档",
  spreadsheet: "表格",
  presentation: "演示",
  image: "图片",
  pdf: "PDF",
  file: "文件",
};

function fileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function WorkArtifactsSheet({ open, artifacts, onOpen, onClose }: Props) {
  if (!open) return null;
  return <>
    <div className="scrim show work-artifacts-scrim" onClick={onClose} />
    <section className="work-artifacts-sheet" role="dialog" aria-modal="true"
      aria-label="Artifacts">
      <header>
        <div>
          <span>Artifacts</span>
          <small>当前工作产生的 {artifacts.length} 个文件</small>
        </div>
        <button className="iconbtn" onClick={onClose} aria-label="关闭">
          <Icon name="close" />
        </button>
      </header>
      <div className="work-artifacts-list">
        {artifacts.map((artifact) => {
          const name = artifact.path.split("/").pop() || artifact.path;
          return <button key={artifact.path} type="button"
            className={artifact.previewable ? "previewable" : ""}
            disabled={!artifact.previewable}
            title={artifact.previewable ? `查看 ${artifact.path}` : "该格式暂不支持在线预览"}
            onClick={() => onOpen(artifact.path)}>
            <span className="work-artifact-icon"><Icon name="read" size={18} /></span>
            <span className="work-artifact-main">
              <b>{name}</b>
              <small title={artifact.path}>{artifact.path}</small>
            </span>
            <span className="work-artifact-meta">
              <b>{/\.(?:mmd|mermaid)$/i.test(artifact.path) ? "Mermaid 图表" : LABELS[artifact.kind]}</b>
              <small>{fileSize(artifact.size)}</small>
            </span>
            {artifact.previewable
              ? <Icon name="chevrons-right" size={15} />
              : <span className="work-artifact-unavailable">暂不可预览</span>}
          </button>;
        })}
      </div>
    </section>
  </>;
}
