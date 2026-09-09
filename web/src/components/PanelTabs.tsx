import { Icon } from "../icons";
import type { Artifact } from "../reducer";

export type RightPanelView = "diff" | "btw";

/** Segmented tabs shared by the right-side work surfaces. */
export function PanelTabs({ active, artifactKind = "gitdiff", hasArtifact = true,
  hasBtw = true, onTab }: {
  active: RightPanelView;
  artifactKind?: Artifact["kind"];
  hasArtifact?: boolean;
  hasBtw?: boolean;
  onTab: (v: RightPanelView) => void;
}) {
  const markdown = artifactKind === "md";
  const file = ["file", "html", "image", "pdf", "audio"].includes(artifactKind);
  return (
    <div className="panel-tabs" role="tablist">
      {hasArtifact && <button className={"ptab" + (active === "diff" ? " on" : "")} role="tab" aria-selected={active === "diff"}
        onClick={() => onTab("diff")}><Icon name={markdown || file ? "read" : "edit"} size={13} /> {markdown ? "预览" : file ? "文件" : "改动"}</button>}
      {hasBtw && <button className={"ptab" + (active === "btw" ? " on" : "")} role="tab" aria-selected={active === "btw"}
        onClick={() => onTab("btw")}><Icon name="spark" size={13} /> btw</button>}
    </div>
  );
}
