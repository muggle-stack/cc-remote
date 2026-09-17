import type { Block, ProcessBlock, TextBlock, ToolBlock } from "./domain/conversation";
import type { AgentDetail, ServerEvent } from "./protocol";

export interface AgentDetailRun {
  runId: string;
  title: string;
  parentRunId: string | null;
  status: AgentDetail["status"];
  events: ServerEvent[];
  blocks: Block[];
  loading: boolean;
  error: string | null;
  requestId: string | null;
  detailRevision: string | null;
  throughSeq: number;
  hasMore: boolean;
  oldestCursor: string | null;
  hasNewer: boolean;
  newerCursor: string | null;
  viewingOlder?: boolean;
}

export interface AgentDetailPanelState {
  sid: string;
  revision: string;
  stack: string[];
  runs: Record<string, AgentDetailRun>;
}

export function emptyAgentRun(runId: string, title = "协作代理"): AgentDetailRun {
  return {
    runId, title, parentRunId: null, status: "unknown", events: [], blocks: [],
    loading: true, error: null, requestId: null, detailRevision: null,
    throughSeq: 0, hasMore: false, oldestCursor: null,
    hasNewer: false, newerCursor: null,
  };
}

function terminal(status: ProcessBlock["status"]): boolean {
  return ["succeeded", "failed", "declined", "cancelled", "interrupted"]
    .includes(status);
}

/** Materialize an Agent-only event stream without creating a conversation turn. */
export function projectAgentEvents(events: readonly ServerEvent[]): Block[] {
  const blocks: Block[] = [];
  const texts = new Map<string, TextBlock>();
  const tools = new Map<string, ToolBlock>();
  const processes = new Map<string, ProcessBlock>();
  for (const event of events) {
    if (event.type === "assistant_msg_start") {
      let block = texts.get(event.message_id);
      if (!block) {
        block = { kind: "text", message_id: event.message_id, text: "",
          channel: event.channel, done: false };
        texts.set(event.message_id, block);
        blocks.push(block);
      } else if (event.channel) block.channel = event.channel;
      continue;
    }
    if (event.type === "delta") {
      let block = texts.get(event.message_id);
      if (!block) {
        block = { kind: "text", message_id: event.message_id, text: "",
          channel: event.channel, done: false };
        texts.set(event.message_id, block);
        blocks.push(block);
      }
      block.text += event.text;
      if (event.channel) block.channel = event.channel;
      continue;
    }
    if (event.type === "assistant_msg_end") {
      const block = texts.get(event.message_id);
      if (block) {
        block.done = true;
        if (event.channel) block.channel = event.channel;
      }
      continue;
    }
    if (event.type === "tool_use") {
      let block = tools.get(event.tool_use_id);
      if (!block) {
        block = { kind: "tool", message_id: event.message_id,
          tool_use_id: event.tool_use_id, tool: event.tool, input: event.input,
          category: event.category, title: event.title,
          parent_id: event.parent_id, server: event.server, done: false };
        tools.set(event.tool_use_id, block);
        blocks.push(block);
      }
      continue;
    }
    if (event.type === "tool_delta") {
      const block = tools.get(event.tool_use_id);
      if (!block) continue;
      if (event.stream === "progress" || event.stream === "summary") {
        block.progress = (block.progress ?? "") + event.delta;
      } else if (event.stream === "diff") {
        block.diff = (block.diff ?? "") + event.delta;
      } else {
        block.output = (block.output ?? "") + event.delta;
      }
      continue;
    }
    if (event.type === "tool_result") {
      const block = tools.get(event.tool_use_id);
      if (!block) continue;
      block.result = { content: event.content, is_error: event.is_error,
        truncated: event.truncated, status: event.status,
        summary: event.summary, diff: event.diff,
        exit_code: event.exit_code, duration_ms: event.duration_ms };
      block.output = block.output ?? event.content;
      block.diff = block.diff ?? event.diff ?? undefined;
      block.done = true;
      continue;
    }
    if (event.type === "process") {
      let block = processes.get(event.item_id);
      if (!block) {
        block = { kind: "process", item_id: event.item_id,
          processKind: event.kind, phase: event.phase, status: event.status,
          turn_id: event.turn_id, parent_id: event.parent_id,
          title: event.title, done: false };
        processes.set(event.item_id, block);
        blocks.push(block);
      }
      block.processKind = event.kind;
      block.phase = event.phase;
      block.status = event.status;
      block.title = event.title || block.title;
      block.turn_id = event.turn_id ?? block.turn_id;
      block.parent_id = event.parent_id ?? block.parent_id;
      for (const key of ["summary", "detail", "input", "output", "diff",
        "progress", "server", "tool", "command", "cwd", "exit_code",
        "duration_ms", "truncated", "background"] as const) {
        if (event[key] != null) Object.assign(block, { [key]: event[key] });
      }
      if (event.append_to && event.delta) {
        const key = event.append_to === "progress" ? "progress" : event.append_to;
        const current = block[key] as string | null | undefined;
        Object.assign(block, { [key]: (current ?? "") + event.delta });
      }
      block.done = event.phase === "end" || terminal(event.status);
      continue;
    }
    if (event.type === "turn_plan") {
      const block = processes.get(event.item_id);
      if (block) {
        block.explanation = event.explanation;
        block.plan = event.plan;
      }
      continue;
    }
    if (event.type === "turn_diff") {
      const block = processes.get(event.item_id);
      if (block) {
        block.diff = event.diff;
        block.truncated = event.truncated;
      }
    }
  }
  return blocks;
}

export function acceptAgentDetail(
  current: AgentDetailRun,
  message: AgentDetail,
): AgentDetailRun {
  if (message.live) {
    if (message.through_seq <= current.throughSeq) {
      return { ...current, title: message.title, status: message.status,
        detailRevision: message.detail_revision };
    }
    const events = [...current.events, ...message.events].slice(-12_000);
    return { ...current, title: message.title,
      parentRunId: message.parent_run_id ?? current.parentRunId,
      status: message.status, events, blocks: projectAgentEvents(events),
      throughSeq: message.through_seq,
      detailRevision: message.detail_revision };
  }
  const older = message.before != null;
  const events = older
    ? [...message.events, ...current.events].slice(-12_000)
    : message.events.slice(-12_000);
  return { ...current, title: message.title,
    parentRunId: message.parent_run_id ?? null, status: message.status,
    events, blocks: projectAgentEvents(events), loading: false,
    error: message.error ?? null, requestId: null,
    detailRevision: message.detail_revision,
    throughSeq: Math.max(current.throughSeq, message.through_seq),
    hasMore: message.has_more ?? false,
    oldestCursor: message.oldest_cursor ?? null,
    hasNewer: message.has_newer ?? false,
    newerCursor: message.newer_cursor ?? null,
    viewingOlder: older };
}
