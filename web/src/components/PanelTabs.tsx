import { Icon } from "../icons";
import type { Artifact } from "../reducer";

/** Segmented tabs shared by the artifact and /btw views (they reuse the right slot). */
export function PanelTabs({ active, artifactKind = "gitdiff", onTab }: {
  active: "diff" | "btw";
  artifactKind?: Artifact["kind"];
  onTab: (v: "diff" | "btw") => void;
}) {
  const markdown = artifactKind === "md";
  const file = artifactKind === "file";
  return (
    <div className="panel-tabs" role="tablist">
      <button className={"ptab" + (active === "diff" ? " on" : "")} role="tab" aria-selected={active === "diff"}
        onClick={() => onTab("diff")}><Icon name={markdown || file ? "read" : "edit"} size={13} /> {markdown ? "预览" : file ? "文件" : "改动"}</button>
      <button className={"ptab" + (active === "btw" ? " on" : "")} role="tab" aria-selected={active === "btw"}
        onClick={() => onTab("btw")}><Icon name="spark" size={13} /> btw</button>
    </div>
  );
}
