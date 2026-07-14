export type OutboundFrame = Record<string, unknown>;

export type EnqueueResult =
  | { ok: true; raw: string; cmdId: string }
  | { ok: false; reason: string };

export type RecoveryReplayStep =
  | { type: "switch"; sid: string }
  | { type: "command"; raw: string };

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
        const frame = JSON.parse(item.raw) as { sid?: unknown };
        return {
          raw: item.raw,
          ...(typeof frame.sid === "string" && frame.sid ? { sid: frame.sid } : {}),
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
