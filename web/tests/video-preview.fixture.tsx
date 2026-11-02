import { StrictMode, useState } from "react";
import { createRoot } from "react-dom/client";
import { ArtifactPanel } from "../src/components/ArtifactPanel";
import type { Artifact } from "../src/reducer";
import { RightPanelPresentation } from "../src/right-panel-presentation";
import "../src/index.css";
import "../src/App.css";

export default function Fixture() {
  const [artifact, setArtifact] = useState<Artifact>();
  const [expanded, setExpanded] = useState(false);
  async function open() {
    const response = await fetch("/api/test-video", { method: "POST" });
    const video = await response.json();
    if (!response.ok) throw new Error("fixture video unavailable");
    setArtifact({ kind: "video", file: video.path, mediaId: video.media_id,
      mediaType: video.media_type, size: video.size });
  }
  return <div style={{ display: "flex", width: "100vw", height: "100dvh" }}>
    <main style={{ flex: 1, minWidth: 0 }}><button onClick={() => void open()}>sample.mp4</button></main>
    <RightPanelPresentation.Provider value={{ expanded, covered: false, layer: 0, chat: false,
      onExpand: () => setExpanded(true), onOpenChat: () => {}, onDock: () => setExpanded(false),
      onDismiss: () => setExpanded(false) }}>
      {artifact && <ArtifactPanel artifact={artifact} machineId="render-device" active="diff"
        hasBtw={false} onTab={() => {}} onClose={() => setArtifact(undefined)} onRefresh={() => void open()} />}
    </RightPanelPresentation.Provider>
  </div>;
}
createRoot(document.getElementById("root")!).render(<StrictMode><Fixture /></StrictMode>);
