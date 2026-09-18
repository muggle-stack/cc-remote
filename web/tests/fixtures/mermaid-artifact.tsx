import { useState } from "react";
import { ArtifactPanel } from "../../src/components/ArtifactPanel";
import { WorkArtifactsSheet } from "../../src/components/WorkArtifactsSheet";

export function MermaidArtifactFixture() {
  const [file, setFile] = useState<string | null>(null);
  const extension = new URLSearchParams(location.search).get("ext") ?? "mmd";
  const path = `camera_navigation_pipeline.${extension}`;
  const content = "flowchart TD\n  camera[双目相机] --> decoder[硬件解码]\n  decoder --> planner[路径规划]\n";
  return <main>
    <WorkArtifactsSheet open={!file} artifacts={[{ path, kind: "document",
      previewable: true, size: content.length, modified_at: 1 }]}
      onOpen={setFile} onClose={() => {}} />
    {file && <ArtifactPanel artifact={{ kind: "file", file, content, size: content.length }}
      active="diff" hasBtw={false} onTab={() => {}} onClose={() => setFile(null)} />}
  </main>;
}
