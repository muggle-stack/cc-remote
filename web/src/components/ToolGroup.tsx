import { useState } from "react";
import type { ToolBlock } from "../reducer";
import { Icon } from "../icons";
import { ToolCallCard } from "./ToolCallCard";
import { isToolFailure, presentTool } from "../tool-presentation";

/** Collapsible group for tool calls within a turn (Claude-app style: a gray
 * summary line "N 个工具调用 · Bash ×2 · Edit ×1" that expands to the individual
 * tool cards). ALWAYS collapsed by default — even while running, only the
 * summary + spinner show; the busy stack of Bash/Edit cards is hidden until
 * the user clicks. */
export function ToolGroup({ tools }: { tools: ToolBlock[] }) {
  const [open, setOpen] = useState(false);
  const running = tools.some((t) => !t.done);
  const hasErr = tools.some(isToolFailure);

  const counts: Record<string, number> = {};
  tools.forEach((t) => {
    const label = presentTool(t).group;
    counts[label] = (counts[label] || 0) + 1;
  });
  const sub = Object.entries(counts).map(([n, c]) => (c > 1 ? `${n} ×${c}` : n)).join(" · ");

  return (
    <details className="tool-group" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary className="tool-group-h">
        <span className={"tool-group-ic" + (running ? " running" : "")}>
          {running ? <span className="spin-dot" /> : <Icon name="verify" size={13} />}
        </span>
        <span className="tool-group-nm">
          {running ? `正在调用 ${tools.length} 个工具` : `${tools.length} 个工具调用`}
          {hasErr && !running && <span className="tool-group-err"> · 有错</span>}
        </span>
        <span className="tool-group-sub">{sub}</span>
        <span className="tool-group-chev"><Icon name="chev" size={14} sw={2} /></span>
      </summary>
      {open && <div className="tool-group-b">
        {tools.map((t) => <ToolCallCard key={t.tool_use_id} block={t} />)}
      </div>}
    </details>
  );
}
