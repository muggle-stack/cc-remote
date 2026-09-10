import { Suspense, lazy, type ComponentProps } from "react";
import { FileBrowserPanel } from "./FileBrowserPanel";
import type { ArtifactPanel as ArtifactPanelType } from "./ArtifactPanel";

const ArtifactPanel = lazy(() => import("./ArtifactPanel").then(({ ArtifactPanel }) => ({ default: ArtifactPanel })));
type PreviewProps = Omit<ComponentProps<typeof ArtifactPanelType>, "artifact" | "active" | "hasBtw" | "onTab">;

export default function SessionFilesPanel({ browser, preview, showingPreview, onBack }: {
  browser: ComponentProps<typeof FileBrowserPanel>;
  preview: PreviewProps & { artifact: ComponentProps<typeof ArtifactPanelType>["artifact"] | null };
  showingPreview: boolean;
  onBack: () => void;
}) {
  return <div className="file-browser-shell">
    <FileBrowserPanel {...browser} />
    {showingPreview && preview.artifact && <>
      <button className="workspace-back" onClick={onBack}>← 返回目录</button>
      <Suspense fallback={<p role="status">加载预览…</p>}>
        <ArtifactPanel {...preview} artifact={preview.artifact} active="diff" hasBtw={false} onTab={() => {}} />
      </Suspense>
    </>}
  </div>;
}
