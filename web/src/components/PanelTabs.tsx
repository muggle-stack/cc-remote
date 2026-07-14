import { Icon } from "../icons";

/** Segmented tabs shared by the diff and /btw views (they reuse the right slot). */
export function PanelTabs({ active, onTab }: { active: "diff" | "btw"; onTab: (v: "diff" | "btw") => void }) {
  return (
    <div className="panel-tabs" role="tablist">
      <button className={"ptab" + (active === "diff" ? " on" : "")} role="tab" aria-selected={active === "diff"}
        onClick={() => onTab("diff")}><Icon name="edit" size={13} /> 改动</button>
      <button className={"ptab" + (active === "btw" ? " on" : "")} role="tab" aria-selected={active === "btw"}
        onClick={() => onTab("btw")}><Icon name="spark" size={13} /> btw</button>
    </div>
  );
}
