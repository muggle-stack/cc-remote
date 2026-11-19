import { useState } from "react";
import { ArtifactPanel } from "../../src/components/ArtifactPanel";
import { RightPanelPresentation } from "../../src/right-panel-presentation";
import { initialState, reduce } from "../../src/reducer";
import { PROTOCOL_VERSION } from "../../src/protocol";

export function PresentationPreviewFixture() {
  const [open, setOpen] = useState(true);
  const [expanded, setExpanded] = useState(false);
  const [revision, setRevision] = useState(0);
  const data = (window as unknown as { presentationFixture: string }).presentationFixture;
  const requestId = `presentation-${revision}`;
  const artifact = reduce({ ...initialState, focusedSid: "slides",
    artifact: { kind: "file", file: "slides.pptx", sid: "slides", requestId, loading: true },
  }, { type: "event", event: {
    type: "file_preview", v: PROTOCOL_VERSION, ts: 1, sid: "slides",
    path: "slides.pptx", request_id: requestId, format: "presentation", content: "",
    data, size: data.length, truncated: false, mtime_ns: "1",
  } }).artifact!;
  return <main style={{ height: "100dvh" }}>
    <RightPanelPresentation.Provider value={{ expanded, covered: false, layer: 0, chat: false,
      onExpand: () => setExpanded(true), onDock: () => setExpanded(false),
      onDismiss: () => setOpen(false), onOpenChat: () => {} }}>
      {open ? <ArtifactPanel artifact={artifact} active="diff" hasBtw={false}
        onClose={() => setOpen(false)} onTab={() => {}}
        onRefresh={() => setRevision(value => value + 1)} />
        : <p>预览已关闭</p>}
    </RightPanelPresentation.Provider>
  </main>;
}
