import { PanelFrame } from "./RightPanelPresentation";
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
  return <PanelFrame className="file-browser-shell" label="会话文件">
    <FileBrowserPanel {...browser} />
    {showingPreview && preview.artifact && <>
      <Suspense fallback={<p role="status">加载预览…</p>}>
        <ArtifactPanel key={`${preview.artifact.sid}:${preview.artifact.file}:${preview.artifact.requestId}`}
          {...preview} artifact={preview.artifact} active="diff" hasBtw={false} onTab={() => {}}
          onBack={onBack} backLabel="返回目录" />
      </Suspense>
    </>}
  </PanelFrame>;
}
