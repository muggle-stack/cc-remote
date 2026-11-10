import type {
  ConversationTurn,
  History,
  QueryFile,
  QueryImg,
} from "./protocol.ts";

import { MAX_PENDING_STEERS } from "./pending-steers.ts";

export type OutboundFrame = Record<string, unknown>;

export type EnqueueResult =
  | { ok: true; raw: string; cmdId: string }
  | { ok: false; reason: string };

export type RecoveryReplayStep =
  | { type: "switch"; sid: string }
  | { type: "command"; raw: string };

export interface QueryAcceptanceEvent {
  type: "user_msg" | "turn_steered" | "turn_binding" | "error";
  sid?: string | null;
  msg_id?: string | null;
  client_msg_id?: string | null;
  code?: string;
  message?: string;
}

export type QueryAcceptanceResult =
  | { accepted: true }
  | { accepted: false; error: { code: string; message: string } };

export interface QueryAcceptanceDescriptor {
  messageId: string;
  prompt: string;
  imageMediaTypes: string[];
  fileNames: string[];
}

export interface QueryAcceptanceHistoryHead {
  revision: string;
  generation: string | null;
  buildSeq: number;
  liveSeq: number;
  newestId: string | null;
}

function attachmentIdentity(
  prompt: string,
  images?: ReadonlyArray<Pick<QueryImg, "media_type">> | null,
  imageRefs?: ReadonlyArray<{ media_type: string }> | null,
  files?: ReadonlyArray<Pick<QueryFile, "filename">> | null,
): Omit<QueryAcceptanceDescriptor, "messageId"> {
  return {
    prompt,
    imageMediaTypes: [...(images ?? imageRefs ?? [])].map(
      (image) => image.media_type),
    fileNames: [...(files ?? [])].map((file) => file.filename),
  };
}

export function queryAcceptanceDescriptor(
  messageId: string,
  prompt: string,
  images?: ReadonlyArray<Pick<QueryImg, "media_type">> | null,
  files?: ReadonlyArray<Pick<QueryFile, "filename">> | null,
): QueryAcceptanceDescriptor {
  return {
    messageId,
    ...attachmentIdentity(prompt, images, undefined, files),
  };
}

/** Capture only a canonical, authoritative newest-page head.
 *
 * `build_seq` is required for direction within one wrapper generation. Without
 * it, a delayed old page with a repeated prompt could look like a new accepted
 * query merely because its native id differs from the browser's optimistic id.
 */
export function queryAcceptanceHistoryHead(
  history: History,
): QueryAcceptanceHistoryHead | null {
  if (history.authoritative === false || history.before
      || !Number.isInteger(history.build_seq)
      || !Number.isInteger(history.live_seq)
      || !Object.prototype.hasOwnProperty.call(history, "newest_id")) {
    return null;
  }
  return {
    revision: history.revision,
    generation: history.generation ?? null,
    buildSeq: history.build_seq!,
    liveSeq: history.live_seq!,
    newestId: history.newest_id ?? null,
  };
}

function historyNewestTurn(
  history: History,
  newestId: string,
): Pick<ConversationTurn, "id" | "prompt" | "images" | "imageRefs" | "files"> | null {
  const materialized = history.turns?.find((turn) => turn.id === newestId);
  if (materialized) return materialized;
  const user = history.events.find((event) => (
    event.type === "user_msg" && event.msg_id === newestId
  ));
  if (!user || user.type !== "user_msg") return null;
  return {
    id: user.msg_id,
    prompt: user.prompt,
    images: user.images ?? undefined,
    imageRefs: undefined,
    files: user.files?.map((file) => ({
      filename: file.filename,
      // History UserMsg carries metadata only; matching never reads file bytes.
      data: "",
    })),
  };
}

function sameStrings(first: readonly string[], second: readonly string[]): boolean {
  return first.length === second.length
    && first.every((value, index) => value === second[index]);
}

/** Return the native newest turn id only when a History page proves that the
 * pending query was appended after the exact head frozen at send time.
 *
 * Prompt/attachment equality identifies the query; head movement and monotonic
 * build ordering prevent an unrelated old repeated prompt from satisfying it.
 */
export function matchQueryAcceptanceHistory(
  pending: QueryAcceptanceDescriptor,
  baseline: QueryAcceptanceHistoryHead | null,
  history: History,
): string | null {
  if (!baseline) return null;
  const head = queryAcceptanceHistoryHead(history);
  if (!head || !head.newestId || head.newestId === baseline.newestId) return null;

  const sameGeneration = head.generation === baseline.generation;
  if (sameGeneration) {
    if (head.revision !== baseline.revision
        || head.buildSeq <= baseline.buildSeq
        || head.liveSeq <= baseline.liveSeq) return null;
  } else if (head.generation == null || baseline.generation == null) {
    // A legacy/partial frame cannot prove an epoch transition. Keep the latch
    // until an exact echo/binding/error arrives.
    return null;
  }

  const newest = historyNewestTurn(history, head.newestId);
  if (!newest) return null;
  const candidate = attachmentIdentity(
    newest.prompt, newest.images, newest.imageRefs, newest.files);
  if (candidate.prompt !== pending.prompt
      || !sameStrings(candidate.imageMediaTypes, pending.imageMediaTypes)
      || !sameStrings(candidate.fileNames, pending.fileNames)) return null;
  return newest.id;
}

interface PendingQueryAcceptance {
  kind: "query" | "steer" | "steer_unknown";
  exactOnly: boolean;
  bytes: number;
  descriptor: QueryAcceptanceDescriptor;
  baseline: QueryAcceptanceHistoryHead | null;
  receipt?: Promise<QueryAcceptanceResult>;
  finish?: (result: QueryAcceptanceResult) => void;
}

/** Per-session acceptance barrier for direct query commands.
 *
 * Transport ACK only proves that the reliable command handler returned. It can
 * arrive before the browser receives the replayable user echo / turn binding,
 * so ACK must never release this barrier. The same object survives RelayWs
 * reconnects and is released only by correlated authoritative narrative proof.
 */
export class QueryAcceptanceLatch {
  private readonly bySession = new Map<string, Map<string, PendingQueryAcceptance>>();

  canBeginSteer(sid: string, concurrent: boolean, bytes = 0): boolean {
    if (concurrent && bytes + [...this.bySession.values()].reduce((total, entries) =>
      total + [...entries.values()].reduce((size, entry) => size + entry.bytes, 0), 0) > 64 * 1024 * 1024) return false;
    const entries = this.bySession.get(sid);
    return !!sid && (!entries?.size || (concurrent
      && entries.size < MAX_PENDING_STEERS
      && [...entries.values()].every(entry => entry.kind === "steer")));
  }

  begin(
    sid: string,
    messageId: string,
    descriptor = queryAcceptanceDescriptor(messageId, ""),
    baseline: QueryAcceptanceHistoryHead | null = null,
    concurrentSteer = false,
    bytes = 0,
  ): boolean {
    if (!sid || !messageId || (concurrentSteer
      ? !this.canBeginSteer(sid, true, bytes) : this.bySession.has(sid))) return false;
    const entries = this.bySession.get(sid) ?? new Map<string, PendingQueryAcceptance>();
    if (entries.has(messageId)) return false;
    entries.set(messageId, {
      kind: concurrentSteer ? "steer" : "query",
      exactOnly: concurrentSteer,
      bytes,
      descriptor: {
        ...descriptor, messageId,
        imageMediaTypes: [...descriptor.imageMediaTypes],
        fileNames: [...descriptor.fileNames],
      },
      baseline: baseline ? { ...baseline } : null,
    });
    this.bySession.set(sid, entries);
    return true;
  }

  pendingMessageIds(sid: string): string[] {
    return [...(this.bySession.get(sid)?.keys() ?? [])];
  }

  pendingMessageId(sid: string): string | null {
    return this.pendingMessageIds(sid)[0] ?? null;
  }

  /** Receipts survive reconnect and re-key; every message resolves separately. */
  receiptFor(sid: string, messageId: string): Promise<QueryAcceptanceResult> | null {
    const pending = this.bySession.get(sid)?.get(messageId);
    if (!pending) return null;
    return pending.receipt ??= new Promise((resolve) => { pending.finish = resolve; });
  }

  pendingSessionIds(): string[] {
    return [...this.bySession.keys()];
  }

  accept(event: QueryAcceptanceEvent): boolean {
    const messageId = event.client_msg_id ?? event.msg_id;
    if (!event.sid || !messageId) return false;
    if (event.type === "error" && event.code === "wrapper_offline") return false;
    const pending = this.bySession.get(event.sid)?.get(messageId);
    if (!pending) return false;
    if (event.type === "error" && event.code === "steer_outcome_unknown"
        && pending.exactOnly) {
      pending.kind = "steer_unknown";
      pending.finish?.({ accepted: false,
        error: { code: event.code, message: event.message ?? "" } });
      return true;
    }
    return this.completeMessage(event.sid, messageId, event.type === "error"
      ? { accepted: false, error: { code: event.code ?? "", message: event.message ?? "" } }
      : { accepted: true });
  }

  acceptHistory(history: History): boolean {
    let changed = false;
    for (const [id, pending] of this.bySession.get(history.session_id) ?? []) {
      // Repeated steer text cannot prove which input Claude consumed. Its
      // durable client/native UUID alias is required, even with one left.
      if (pending.exactOnly || !matchQueryAcceptanceHistory(
        pending.descriptor, pending.baseline, history)) continue;
      changed = this.completeMessage(history.session_id, id) || changed;
    }
    return changed;
  }

  completeMessage(sid: string, id: string,
    result: QueryAcceptanceResult = { accepted: true }): boolean {
    const entries = this.bySession.get(sid);
    const pending = entries?.get(id);
    if (!pending) return false;
    entries!.delete(id);
    if (!entries!.size) this.bySession.delete(sid);
    pending.finish?.(result);
    return true;
  }

  completeSession(sid: string, result: QueryAcceptanceResult = { accepted: true }): boolean {
    const id = this.pendingMessageId(sid);
    return id ? this.completeMessage(sid, id, result) : false;
  }

  settleUnknownSteers(sid: string): void {
    for (const [id, pending] of this.bySession.get(sid) ?? []) {
      if (pending.kind === "steer_unknown") this.completeMessage(sid, id, {
        accepted: false, error: { code: "steer_outcome_unknown",
          message: "任务已结束，但本次引导是否生效未得到确认。" },
      });
    }
  }

  rekeySession(oldKey: string, sessionId: string): void {
    if (!oldKey || oldKey === sessionId) return;
    const pending = this.bySession.get(oldKey);
    if (!pending) return;
    const target = this.bySession.get(sessionId);
    this.bySession.set(sessionId, new Map([...pending, ...(target ?? [])]));
    this.bySession.delete(oldKey);
  }
}

/** Interleave target preparation with the exact command it protects. */
export function planRecoveryReplay(
  pending: ReadonlyArray<{ raw: string; sid?: string }>,
  focusedSid: string | null,
): RecoveryReplayStep[] {
  const steps: RecoveryReplayStep[] = [];
  for (const item of pending) {
    if (item.sid && !item.sid.startsWith("btw-")) {
      steps.push({ type: "switch", sid: item.sid });
    }
    steps.push({ type: "command", raw: item.raw });
  }
  if (focusedSid && !focusedSid.startsWith("btw-")) {
    steps.push({ type: "switch", sid: focusedSid });
  }
  return steps;
}

interface PendingCommand {
  clientId: string;
  type: string | null;
  raw: string;
  bytes: number;
}

/** Bounded insertion-ordered store for commands awaiting wrapper ACK. */
export class CommandOutbox {
  private readonly pending = new Map<string, PendingCommand>();
  private pendingBytes = 0;
  private readonly maxCommands: number;
  private readonly maxBytes: number;
  private readonly maxFrameBytes: number;

  constructor(maxCommands: number, maxBytes: number, maxFrameBytes = maxBytes) {
    this.maxCommands = maxCommands;
    this.maxBytes = maxBytes;
    this.maxFrameBytes = maxFrameBytes;
  }

  get size(): number {
    return this.pending.size;
  }

  get byteSize(): number {
    return this.pendingBytes;
  }

  hasPendingExcept(types: ReadonlySet<string>): boolean {
    for (const item of this.pending.values()) {
      if (item.type === null || !types.has(item.type)) return true;
    }
    return false;
  }

  enqueue(frame: OutboundFrame, clientId: string, cmdId: string): EnqueueResult {
    if (this.pending.has(cmdId)) {
      return { ok: false, reason: `duplicate command id: ${cmdId}` };
    }
    const raw = JSON.stringify({ ...frame, cmd_id: cmdId, client_id: clientId });
    const bytes = new TextEncoder().encode(raw).byteLength;
    if (bytes > this.maxFrameBytes) {
      return {
        ok: false,
        reason: `command too large (${bytes} bytes; max ${this.maxFrameBytes} bytes)`,
      };
    }
    if (this.pending.size >= this.maxCommands) {
      return { ok: false, reason: `command outbox full (${this.maxCommands} commands)` };
    }
    if (bytes > this.maxBytes || this.pendingBytes + bytes > this.maxBytes) {
      return { ok: false, reason: `command outbox full (${this.maxBytes} bytes)` };
    }
    this.pending.set(cmdId, {
      clientId, type: typeof frame.type === "string" ? frame.type : null, raw, bytes,
    });
    this.pendingBytes += bytes;
    return { ok: true, raw, cmdId };
  }

  ack(clientId: string, cmdId: string): boolean {
    const item = this.pending.get(cmdId);
    if (!item || item.clientId !== clientId) return false;
    this.pending.delete(cmdId);
    this.pendingBytes -= item.bytes;
    return true;
  }

  pendingFrames(): string[] {
    return [...this.pending.values()].map((item) => item.raw);
  }

  /** Insertion-ordered frames plus targets needing preparation for mutations.
   * Read-only requests keep their own target in raw without a recovery switch. */
  pendingFramesWithSessionIds(): Array<{ raw: string; sid?: string }> {
    return [...this.pending.values()].map((item) => {
      try {
        const frame = JSON.parse(item.raw) as {
          type?: unknown; sid?: unknown;
        };
        // Reads own their lookup/fallback behavior. Replaying a background read
        // must never focus or resume that session (its engine/space catalog may
        // not even have been loaded on this page).
        const read = typeof frame.type === "string"
          && /^(get_|list_|read_|browse_)/.test(frame.type);
        return {
          raw: item.raw,
          ...(!read && typeof frame.sid === "string" && frame.sid ? { sid: frame.sid } : {}),
        };
      } catch {
        return { raw: item.raw }; // enqueue always generated valid JSON
      }
    });
  }

  pendingSessionIds(): string[] {
    const out: string[] = [];
    const seen = new Set<string>();
    for (const item of this.pending.values()) {
      try {
        const frame = JSON.parse(item.raw) as { sid?: unknown; session_id?: unknown };
        for (const candidate of [frame.sid, frame.session_id]) {
          if (typeof candidate === "string" && candidate && !seen.has(candidate)) {
            seen.add(candidate);
            out.push(candidate);
          }
        }
      } catch { /* enqueue always generated valid JSON */ }
    }
    return out;
  }

  rekeySession(oldKey: string, sessionId: string): void {
    if (!oldKey || oldKey === sessionId) return;
    for (const item of this.pending.values()) {
      try {
        const frame = JSON.parse(item.raw) as Record<string, unknown>;
        let changed = false;
        if (frame.sid === oldKey) { frame.sid = sessionId; changed = true; }
        if (frame.session_id === oldKey) { frame.session_id = sessionId; changed = true; }
        if (!changed) continue;
        const raw = JSON.stringify(frame);
        const bytes = new TextEncoder().encode(raw).byteLength;
        if (bytes > this.maxFrameBytes
            || this.pendingBytes - item.bytes + bytes > this.maxBytes) continue;
        this.pendingBytes += bytes - item.bytes;
        item.raw = raw;
        item.bytes = bytes;
      } catch { /* enqueue always generated valid JSON */ }
    }
  }
}
