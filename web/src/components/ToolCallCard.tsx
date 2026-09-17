import { useMemo, useRef, useState } from "react";
import type { ToolBlock } from "../domain/conversation";
import { Icon } from "../icons";
import { buildEditDiffPreview } from "../diff";
import {
  cancelDraggedPointer,
  PointerTapGuard,
  releaseDraggedPointer,
} from "../pointer-tap";
import { isToolFailure, presentTool } from "../tool-presentation";
import { ToolInput, ToolOutput } from "./ToolDetails";

function EditDiff({ oldString, newString, serverDiff }: {
  oldString: string; newString: string; serverDiff?: string | null;
}) {
  const preview = useMemo(
    () => buildEditDiffPreview(oldString, newString, serverDiff),
    [oldString, newString, serverDiff],
  );
  if (preview.source === "server") {
    return <pre className="tool-pre tool-diff">{preview.diff}</pre>;
  }
  return (
    <>
      <pre className="diff-pre">
        {preview.lines.map((line, index) => (
          <span key={index} className={"diff-" + line.type}>
            {(line.type === "add" ? "+" : line.type === "del" ? "−" : " ") + " " + line.text + "\n"}
          </span>
        ))}
      </pre>
      {preview.truncated && (
        <div className="tool-diff-note">服务端未返回 diff，已用有界预览省略 {preview.omittedLines} 行。</div>
      )}
    </>
  );
}

export function ToolCallCard({ block }: { block: ToolBlock }) {
  const [open, setOpen] = useState(false);
  const tapGuard = useRef(new PointerTapGuard());
  const status = !block.done ? "run" : isToolFailure(block) ? "err" : "done";
  const presentation = presentTool(block);
  const inp = block.input as { file_path?: string; old_string?: string; new_string?: string; content?: string };
  const isEdit = block.tool === "Edit";
  const isWrite = block.tool === "Write";
  const streamedOutput = block.output?.trim();
  const finalOutput = block.result?.content?.trim();
  const output = finalOutput || streamedOutput;
  const resultSummary = block.result?.summary?.trim();
  const diff = block.result?.diff || block.diff;
  const hasInput = Object.keys(block.input).length > 0;
  const hasBody = !!(
    block.progress || resultSummary || diff || output || hasInput
    || block.result?.exit_code != null
    || block.result?.duration_ms != null
  );
  const header = (
    <>
      <span className="tool-ic"><Icon name={presentation.icon} size={15} /></span>
      <span className="tool-nm">{presentation.title}</span>
      <span className="tool-arg">{presentation.subtitle}</span>
      <span className={`tool-st ${status}`}>
        {status === "done" ? <Icon name="verify" size={16} />
          : status === "err" ? <Icon name="close" size={16} />
          : null}
      </span>
      {hasBody && (
        <span className="tool-chev"><Icon name="chev" size={16} sw={2} /></span>
      )}
    </>
  );
  if (!hasBody) {
    return (
      <div className="tool tool-static">
        <div className="tool-h">{header}</div>
      </div>
    );
  }
  return (
    <details className="tool" open={open}>
      <summary className="tool-h"
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
        }}>{header}</summary>
      {open && <div className="tool-b">
        {block.progress && <div className="tool-progress">{block.progress}</div>}
        {resultSummary && resultSummary !== output && (
          <div className="tool-progress">{resultSummary}</div>
        )}
        {isEdit ? (
          <>
            <div className="tool-lbl">{inp.file_path}</div>
            <EditDiff oldString={inp.old_string || ""} newString={inp.new_string || ""} serverDiff={diff} />
          </>
        ) : isWrite ? (
          <>
            <div className="tool-lbl">{inp.file_path}</div>
            <pre className="tool-pre">{inp.content}</pre>
          </>
        ) : hasInput ? (
          <ToolInput input={block.input} />
        ) : null}
        {diff && !isEdit && (
          <>
            <div className="tool-lbl">Diff</div>
            <pre className="tool-pre tool-diff">{diff}</pre>
          </>
        )}
        {output && (
          <ToolOutput output={output} truncated={block.result?.truncated}
            label={block.result?.is_error ? "输出（失败）"
              : presentation.group === "搜索" ? "搜索结果" : "输出"} />
        )}
        {block.result && (block.result.exit_code != null || block.result.duration_ms != null) && (
          <div className="tool-meta">
            {block.result.exit_code != null && <span>exit {block.result.exit_code}</span>}
            {block.result.duration_ms != null && <span>{Math.max(0, block.result.duration_ms / 1000).toFixed(1)}s</span>}
          </div>
        )}
      </div>}
    </details>
  );
}
