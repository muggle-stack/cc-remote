import { lazy, Suspense, useRef, useState } from "react";
import type { ToolBlock } from "../domain/conversation";
import { Icon } from "../icons";
import {
  cancelDraggedPointer,
  PointerTapGuard,
  releaseDraggedPointer,
} from "../pointer-tap";
import { isToolFailure, presentTool } from "../tool-presentation";

const ToolCallCard = lazy(() => import("./ToolCallCard").then((module) => ({ default: module.ToolCallCard })));

/** Collapsible group for tool calls within a turn (Claude-app style: a gray
 * summary line "N 个工具调用 · Bash ×2 · Edit ×1" that expands to the individual
 * tool cards). ALWAYS collapsed by default — even while running, only the
 * summary shows live activity; the busy stack of Bash/Edit cards is hidden until
 * the user clicks. */
export function ToolGroup({ tools, active = true }: {
  tools: ToolBlock[]; active?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const tapGuard = useRef(new PointerTapGuard());
  const running = active && tools.some((t) => !t.done);
  const hasErr = tools.some(isToolFailure);

  const counts: Record<string, number> = {};
  tools.forEach((t) => {
    const label = presentTool(t).group;
    counts[label] = (counts[label] || 0) + 1;
  });
  const sub = Object.entries(counts).map(([n, c]) => (c > 1 ? `${n} ×${c}` : n)).join(" · ");

  return (
    <details className="tool-group" open={open}>
      <summary className="tool-group-h"
        onPointerDown={(event) => {
          tapGuard.current.pointerDown(
            event.pointerId, event.clientX, event.clientY);
          event.currentTarget.setPointerCapture?.(event.pointerId);
        }}
        onPointerMove={(event) => {
          if (tapGuard.current.pointerMove(
            event.pointerId, event.clientX, event.clientY,
          )) {
            releaseDraggedPointer(
              event.currentTarget, event.pointerId, event.pointerType);
          }
        }}
        onPointerUp={(event) => tapGuard.current.pointerUp(event.pointerId)}
        onPointerCancel={(event) => {
          cancelDraggedPointer(
            tapGuard.current,
            event.currentTarget, event.pointerId, event.pointerType);
        }}
        onClick={(event) => {
          event.preventDefault();
          if (tapGuard.current.consumeClick(event.detail)) setOpen(!open);
        }}>
        <span className={`tool-group-label status-shimmer${running ? " is-active" : ""}`}>
          <span className="tool-group-nm">
            {running ? `正在调用 ${tools.length} 个工具` : `${tools.length} 个工具调用`}
            {hasErr && !running && <span className="tool-group-err"> · 有错</span>}
          </span>
          <span className="tool-group-sub">{sub}</span>
        </span>
        <span className="tool-group-chev"><Icon name="chev" size={14} sw={2} /></span>
      </summary>
      {open && <div className="tool-group-b">
        <Suspense fallback={<span className="tool-lbl">读取工具详情…</span>}>
        {tools.map((t) => <ToolCallCard key={t.tool_use_id} block={t} />)}
        </Suspense>
      </div>}
    </details>
  );
}
