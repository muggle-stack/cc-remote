import { useEffect, useRef, useState } from "react";
import type { Block, ProcessBlock, TextBlock, ToolBlock } from "../reducer";
import { Icon } from "../icons";
import { MessageBlock } from "./MessageBlock";
import { ToolGroup } from "./ToolGroup";
import { hasActiveProcess, processBlocks } from "../process-blocks";

function durationLabel(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  if (minutes < 60) return `${minutes}m ${rest}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

function statusIcon(status: ProcessBlock["status"], done: boolean) {
  if (!done && (status === "running" || status === "pending" || status === "unknown")) {
    return <span className="process-spin" />;
  }
  if (status === "failed" || status === "declined" || status === "cancelled"
      || status === "interrupted") {
    return <Icon name="close" size={14} />;
  }
  return <Icon name="verify" size={14} />;
}

const PROCESS_IC: Record<ProcessBlock["processKind"], string> = {
  reasoning: "spark",
  plan: "plan",
  command: "bash",
  file_change: "edit",
  mcp: "term",
  agent: "spark",
  hook: "shield",
  server_tool: "term",
  web_search: "research",
  task: "plan",
  terminal: "bash",
  model: "cpu",
  safety: "shield",
  diff: "edit",
  compaction: "simplify",
};

function ProcessActivity({ block }: { block: ProcessBlock }) {
  const hasBody = !!(block.summary || block.detail || block.output || block.diff
    || block.progress || block.command || block.cwd || block.plan?.length
    || (block.input && Object.keys(block.input).length));
  const body = (
    <>
      {block.progress && <div className="process-progress">{block.progress}</div>}
      {block.explanation && <div className="process-copy">{block.explanation}</div>}
      {block.plan && block.plan.length > 0 && (
        <ol className="process-plan">
          {block.plan.map((entry, index) => (
            <li key={`${index}-${entry.step}`} className={`plan-${entry.status}`}>
              <span>{entry.status === "completed" ? "✓" : entry.status === "inProgress" ? "•" : "○"}</span>
              <span>{entry.step}</span>
            </li>
          ))}
        </ol>
      )}
      {block.command && <pre className="tool-pre process-command">$ {block.command}</pre>}
      {block.cwd && <div className="process-meta">{block.cwd}</div>}
      {block.summary && <div className="process-copy">{block.summary}</div>}
      {block.detail && <pre className="tool-pre">{block.detail}</pre>}
      {block.input && Object.keys(block.input).length > 0 && (
        <pre className="tool-pre">{JSON.stringify(block.input, null, 2)}</pre>
      )}
      {block.output && <pre className="tool-pre">{block.output}{block.truncated ? "\n…(truncated)" : ""}</pre>}
      {block.diff && <pre className="tool-pre tool-diff">{block.diff}</pre>}
      {(block.exit_code != null || block.duration_ms != null) && (
        <div className="tool-meta">
          {block.exit_code != null && <span>exit {block.exit_code}</span>}
          {block.duration_ms != null && <span>{durationLabel(block.duration_ms)}</span>}
        </div>
      )}
    </>
  );

  if (!hasBody) {
    return (
      <div className={`process-activity process-${block.status}`}>
        <span className="process-item-ic"><Icon name={PROCESS_IC[block.processKind]} size={15} /></span>
        <span className="process-item-title">{block.title}</span>
        <span className="process-item-status">{statusIcon(block.status, block.done)}</span>
      </div>
    );
  }
  return (
    <details className={`process-activity process-${block.status}`}>
      <summary>
        <span className="process-item-ic"><Icon name={PROCESS_IC[block.processKind]} size={15} /></span>
        <span className="process-item-title">{block.title}</span>
        <span className="process-item-status">{statusIcon(block.status, block.done)}</span>
        <span className="process-item-chev"><Icon name="chev" size={14} /></span>
      </summary>
      <div className="process-item-body">{body}</div>
    </details>
  );
}

function TimelineItem({ block }: { block: Block }) {
  if (block.kind === "process") return <ProcessActivity block={block as ProcessBlock} />;
  const text = block as TextBlock;
  if (text.channel === "thinking") {
    return (
      <details className="process-reasoning">
        <summary><Icon name="spark" size={14} /><span>思考</span><Icon name="chev" size={13} /></summary>
        <div className="process-reasoning-body"><MessageBlock text={text.text} done={text.done} /></div>
      </details>
    );
  }
  return <div className="process-commentary"><MessageBlock text={text.text} done={text.done} /></div>;
}

type TimelineRow =
  | { kind: "item"; block: TextBlock | ProcessBlock }
  | { kind: "tools"; tools: ToolBlock[] };

function groupTimelineRows(items: Block[]): TimelineRow[] {
  const rows: TimelineRow[] = [];
  for (const block of items) {
    if (block.kind !== "tool") {
      rows.push({ kind: "item", block });
      continue;
    }
    const previous = rows[rows.length - 1];
    if (previous?.kind === "tools") previous.tools.push(block);
    else rows.push({ kind: "tools", tools: [block] });
  }
  return rows;
}

export function ProcessTimeline({ blocks, done, durationMs, startTs }: {
  blocks: Block[];
  done: boolean;
  durationMs?: number;
  startTs?: number;
}) {
  const items = processBlocks(blocks);
  const complete = done && !hasActiveProcess(items);
  const [open, setOpen] = useState(!complete);
  const [now, setNow] = useState(Date.now());
  const manuallyToggled = useRef(false);

  useEffect(() => {
    if (!manuallyToggled.current) setOpen(!complete);
  }, [complete]);
  useEffect(() => {
    if (complete) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [complete]);

  if (!items.length) return null;
  const rows = groupTimelineRows(items);
  const elapsed = complete ? (durationMs ?? 0) : Math.max(0, now - (startTs ?? now));
  return (
    <section className={`turn-process${open ? " open" : ""}`}>
      <button type="button" className="turn-process-head" aria-expanded={open}
        onClick={() => { manuallyToggled.current = true; setOpen((value) => !value); }}>
        <span className={`turn-process-state${complete ? " done" : " running"}`}>
          {complete ? <Icon name="verify" size={14} /> : <span className="process-spin" />}
        </span>
        <span>{complete ? "已处理" : "正在处理"} {durationLabel(elapsed)}</span>
        <span className="turn-process-count">{items.length} 项</span>
        <Icon name="chev" size={15} />
      </button>
      {open && <div className="process-timeline">{rows.map((row) => (
        row.kind === "tools"
          ? <ToolGroup key={`tools-${row.tools[0].tool_use_id}`} tools={row.tools} />
          : <TimelineItem key={row.block.kind === "text"
              ? `text-${row.block.message_id}` : `process-${row.block.item_id}`}
              block={row.block} />
      ))}</div>}
    </section>
  );
}
