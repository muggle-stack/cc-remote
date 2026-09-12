import type {
  ConversationTurn,
  History,
  QueryFile,
  QueryImg,
} from "./protocol.ts";

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
  private readonly bySession = new Map<string, PendingQueryAcceptance>();

  begin(
    sid: string,
    messageId: string,
    descriptor = queryAcceptanceDescriptor(messageId, ""),
    baseline: QueryAcceptanceHistoryHead | null = null,
  ): boolean {
    if (!sid || !messageId || this.bySession.has(sid)) return false;
    this.bySession.set(sid, {
      descriptor: {
        ...descriptor,
        messageId,
        imageMediaTypes: [...descriptor.imageMediaTypes],
        fileNames: [...descriptor.fileNames],
      },
      baseline: baseline ? { ...baseline } : null,
    });
    return true;
  }

  pendingMessageId(sid: string): string | null {
    return this.bySession.get(sid)?.descriptor.messageId ?? null;
  }

  /** The same receipt survives reconnect and temporary-to-native re-keying.
   * Unknown outcomes surface as errors; the runtime's steer_unknown barrier
   * continues to block resubmission until native activity is reconciled. */
  receiptFor(sid: string, messageId: string): Promise<QueryAcceptanceResult> | null {
    const pending = this.bySession.get(sid);
    if (!pending || pending.descriptor.messageId !== messageId) return null;
    return pending.receipt ??= new Promise((resolve) => { pending.finish = resolve; });
  }

  pendingSessionIds(): string[] {
    return [...this.bySession.keys()];
  }

  accept(event: QueryAcceptanceEvent): boolean {
    const messageId = event.client_msg_id ?? event.msg_id;
    if (!event.sid || !messageId) return false;
    if (event.type === "error" && event.code === "wrapper_offline") return false;
    if (this.pendingMessageId(event.sid) !== messageId) return false;
    return this.completeSession(event.sid, event.type === "error"
      ? { accepted: false, error: { code: event.code ?? "", message: event.message ?? "" } }
      : { accepted: true });
  }

  acceptHistory(history: History): boolean {
    const pending = this.bySession.get(history.session_id);
    if (!pending
        || !matchQueryAcceptanceHistory(
          pending.descriptor, pending.baseline, history)) return false;
    return this.completeSession(history.session_id);
  }

  completeSession(sid: string, result: QueryAcceptanceResult = { accepted: true }): boolean {
    const pending = this.bySession.get(sid);
    if (!pending) return false;
    this.bySession.delete(sid);
    pending.finish?.(result);
    return true;
  }

  rekeySession(oldKey: string, sessionId: string): void {
    if (!oldKey || oldKey === sessionId) return;
    const pending = this.bySession.get(oldKey);
    if (!pending) return;
    this.bySession.set(sessionId, pending);
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
    this.pending.set(cmdId, { clientId, raw, bytes });
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

  /** Insertion-ordered frames plus their explicit runtime target. Recovery uses
   * this to make one target resident immediately before replaying that command,
   * instead of pre-warming every target and evicting early ones at pool capacity. */
  pendingFramesWithSessionIds(): Array<{ raw: string; sid?: string }> {
    return [...this.pending.values()].map((item) => {
      try {
        const frame = JSON.parse(item.raw) as {
          type?: unknown; sid?: unknown; turn_id?: unknown; revision?: unknown;
        };
        // Archived files are private disk reads, not engine operations. A
        // reconnect must replay the read without resuming its background sid.
        const archiveRead = frame.type === "get_turn_file_changes"
          || (frame.type === "get_diff" && !!frame.turn_id && !!frame.revision);
        return {
          raw: item.raw,
          ...(!archiveRead && typeof frame.sid === "string" && frame.sid ? { sid: frame.sid } : {}),
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
