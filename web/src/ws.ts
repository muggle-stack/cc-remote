// WebSocket client to the relay. Browser authentication is carried by the
// relay's Secure HttpOnly cookie, so no credential appears in JS or the URL.
// Auto-reconnects with backoff.
//
// Multi-session: every inbound frame is demuxed by `msg.sid` (the wrapper
// stamps it). lastSeq is tracked PER session (lastSeqBySession) so catch-up
// cursors are per-session. session_focus just sets focusedSid + dispatches —
// no cursor reset, no re-hello (background turns keep streaming). All outbound
// commands that target a session stamp `sid: focusedSid`.
import type {
  AutoCompactMode, DiffTheme, Engine, GoalStatus, QueryFile,
  QueryImg, ServerEvent, SessionControl, Space,
} from "./protocol.ts";
import {
  compareSessionControl,
  makeForkSessionCommand,
  makeForkSessionWorktreeCommand,
  makeMigrateSessionCommand,
  makeOpenBtwCommand,
  PROTOCOL_VERSION,
  sessionControlTargetsSid,
} from "./protocol.ts";
import { validAutoCompactThreshold } from "./auto-compact.ts";
import {
  CommandOutbox,
  QueryAcceptanceLatch,
  planRecoveryReplay,
  queryAcceptanceDescriptor,
  queryAcceptanceHistoryHead,
  type QueryAcceptanceHistoryHead,
} from "./outbox.ts";
import { probeSession, shouldReconnectAfterSessionProbe } from "./session-auth.ts";
import { uuid } from "./util.ts";

export type ConnState = "connecting" | "connected" | "reconnecting" | "disconnected";

export interface EventOwnership {
  scopeKey: string;
  machineId: string;
  engine: "claude" | "codex";
  space: Space;
  claudeProfileId?: string | null;
  codexProfileId?: string | null;
  surfaceEpoch: number;
  connectionGeneration: number;
}

interface ListRequestOwnership {
  ownership: EventOwnership;
  order: number;
}

interface InvalidatedListRefresh {
  requestId: string;
  engine: "claude" | "codex";
  space: Space;
  dirty: boolean;
}

export function sessionScopeKey(
  machineId: string, engine: "claude" | "codex", space: Space,
): string {
  return `${machineId}:${space}:${engine}`;
}

export interface WsCallbacks {
  onEvent: (msg: ServerEvent, ownership?: EventOwnership) => void;
  onConnState: (s: ConnState, detail?: string) => void;
  onAuthFail?: () => void;
  onCommandError?: (detail: string) => void;
  onCommandCompleted?: (commandId: string) => void;
  onWrapperGenerationChanged?: () => void;
  onOutboxChanged?: (protectedSessionIds: string[]) => void;
}

const OUTBOX_MAX_COMMANDS = 256;
const OUTBOX_MAX_BYTES = 64 * 1024 * 1024;
// Keep one browser frame below the relay's 16 MiB WebSocket receive ceiling.
// The margin covers envelope growth and avoids a reconnect loop for commands
// that can enter the 64 MiB aggregate outbox but can never cross the relay.
const OUTBOX_MAX_FRAME_BYTES = 14 * 1024 * 1024;
const MAX_REPLAY_SESSIONS = 128;
const PROTOCOL_RELOAD_KEY = "cc-remote:protocol-reload";
const PROTOCOL_RECOVERY_POLL_MS = 1000;
const PROTOCOL_RECOVERY_TIMEOUT_MS = 120000;
let pageClientId: string | null = null;

/** A page-lifetime connection identity. Durable BTW ownership is derived from
 * authenticated relay claims, so duplicated tabs must never reuse this id and
 * replace one another's WebSocket generation.
 */
export function stableTabClientId(): string {
  pageClientId ??= uuid();
  return pageClientId;
}

function readProtocolReloadMarker(): string | null | undefined {
  try {
    return globalThis.sessionStorage.getItem(PROTOCOL_RELOAD_KEY);
  } catch {
    return undefined;
  }
}

function writeProtocolReloadMarker(protocol: number): boolean {
  try {
    globalThis.sessionStorage.setItem(
      PROTOCOL_RELOAD_KEY, String(protocol));
    return true;
  } catch {
    return false;
  }
}

function clearProtocolReloadMarker(): void {
  try {
    globalThis.sessionStorage.removeItem(PROTOCOL_RELOAD_KEY);
  } catch {
    // Storage can be unavailable in privacy-restricted or sandboxed contexts.
  }
}

type ProtocolRecoveryAction =
  | { kind: "wait" }
  | { kind: "reconnect" }
  | { kind: "reload"; protocol: number };

export function protocolRecoveryAction(
  relayProtocol: number | null,
  webProtocol: number | null,
): ProtocolRecoveryAction {
  if (relayProtocol === null || webProtocol === null
      || relayProtocol !== webProtocol) {
    return { kind: "wait" };
  }
  if (relayProtocol === PROTOCOL_VERSION) return { kind: "reconnect" };
  return { kind: "reload", protocol: relayProtocol };
}

async function fetchProtocol(url: string): Promise<number | null> {
  try {
    // The query keeps probes fresh even while an older service worker is still
    // controlling the page and cache-first rules from that worker remain live.
    const separator = url.includes("?") ? "&" : "?";
    const response = await fetch(
      `${url}${separator}protocol_probe=${Date.now()}`,
      { cache: "no-store", credentials: "same-origin" },
    );
    if (!response.ok) return null;
    const body = await response.json() as { protocol?: unknown };
    return typeof body.protocol === "number" && Number.isInteger(body.protocol)
      ? body.protocol
      : null;
  } catch {
    return null;
  }
}

function nowTs(): number {
  return Date.now() / 1000;
}

export class RelayWs {
  private ws: WebSocket | null = null;
  private lastSeqBySession: Record<string, number> = {};
  private generationBySession: Record<string, string> = {};
  // Transport-level control watermark survives runtime pruning/reconnect. The
  // reducer repeats this guard because cached control may hydrate independently.
  private controlBySession: Record<string, SessionControl> = {};
  private rebuildingSessions = new Set<string>();
  private replayOrder: string[] = [];
  private engineBySession: Record<string, "claude" | "codex"> = {};
  private spaceBySession: Record<string, Space> = {};
  private claudeProfileBySession: Record<string, string> = {};
  private codexProfileBySession: Record<string, string> = {};
  private focusedSid: string | null = null;
  private activeEngine: "claude" | "codex" = "claude";
  private activeSpace: Space = "code";
  // Correlates a create response without using SessionFocus as a trigger for the
  // first query. A later explicit switch clears it, so late create focus cannot
  // override the user's newer navigation intent.
  private newSessionFocusRequestId: string | null = null;
  private newSessionEngine: "claude" | "codex" = "claude";
  private newSessionSpace: Space = "code";
  private connectionGeneration = 0;
  private surfaceEpoch = 1;
  private readonly surfaceEpochByScope: Record<string, number> = {};
  private readonly ownershipBySession: Record<string, EventOwnership> = {};
  private readonly pendingOwnershipByRequest: Record<string, EventOwnership> = {};
  private readonly pendingListOwnershipByRequest =
    new Map<string, ListRequestOwnership>();
  private readonly latestAcceptedListOrderByScope = new Map<string, number>();
  private readonly invalidatedListRefreshByScope =
    new Map<string, InvalidatedListRefresh>();
  private readonly invalidatedListScopeByRequest = new Map<string, string>();
  private listRequestOrder = 0;
  private readonly pendingGoalOwnershipByRequest =
    new Map<string, EventOwnership>();
  private readonly pendingSwitchOwnership: Record<string, EventOwnership[]> = {};
  private readonly outbox = new CommandOutbox(
    OUTBOX_MAX_COMMANDS, OUTBOX_MAX_BYTES, OUTBOX_MAX_FRAME_BYTES);
  private readonly queryAcceptance = new QueryAcceptanceLatch();
  private readonly historyHeadBySession: Record<
    string, QueryAcceptanceHistoryHead
  > = {};
  private readonly clientId: string;
  private readonly url: string;
  private backoff = 1;
  private stopped = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private protocolRecoveryTimer: ReturnType<typeof setTimeout> | null = null;
  private protocolRecoveryStartedAt = 0;
  private protocolRecoveryGeneration = 0;
  private readonly cb: WsCallbacks;
  private readonly machineId: string;
  // Heartbeat: detect a HALF-OPEN link (dead TCP with no close event) and recover.
  private lastRecvAt = 0;
  private hbTimer: ReturnType<typeof setInterval> | null = null;
  private pingSeq = 0;
  private wrapperGeneration: string | null = null;
  private lastGenerationChangeNotice: string | null = null;
  private readonly knownBtwSids = new Set<string>();
  private readonly previewReadAttempts = new Map<string, { authorizationId: string; commandId: string }>();

  constructor(cb: WsCallbacks, machineId = "default") {
    this.cb = cb;
    this.machineId = machineId;
    this.clientId = stableTabClientId();
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = new URL(`${proto}//${window.location.host}/ws`);
    if (machineId !== "default") url.searchParams.set("machine", machineId);
    this.url = url.toString();
    this.surfaceEpochByScope[
      sessionScopeKey(machineId, this.activeEngine, this.activeSpace)
    ] = this.surfaceEpoch;
  }

  start(): void {
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.cancelProtocolRecovery();
    this.stopHeartbeat();
    this.invalidatedListRefreshByScope.clear();
    this.invalidatedListScopeByRequest.clear();
    const ws = this.ws;
    // Retire the callback generation before close(). Browsers may still deliver
    // queued open/message/close callbacks synchronously while an App effect is
    // replacing this RelayWs instance.
    this.connectionGeneration += 1;
    this.ws = null;
    ws?.close();
  }

  // App-level heartbeat. The browser's WS onclose does NOT fire for a HALF-OPEN
  // link (dead TCP with no FIN — common behind mobile NAT / TUN proxies), so the
  // client would sit "connected" receiving nothing until a manual refresh (this
  // is exactly the "只能强行刷新才能看到" symptom). Ping every 20s; if NO frame at
  // all arrives for 45s (i.e. pongs stopped), force-close → onclose → reconnect.
  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.lastRecvAt = Date.now();
    this.hbTimer = setInterval(() => {
      const ws = this.ws;
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      if (Date.now() - this.lastRecvAt > 45000) { ws.close(); return; }
      this.sendUntracked({ v: PROTOCOL_VERSION, type: "ping", n: ++this.pingSeq, ts: nowTs() });
    }, 20000);
  }

  private stopHeartbeat(): void {
    if (this.hbTimer) { clearInterval(this.hbTimer); this.hbTimer = null; }
  }

  /** Highest seq across all known sessions (used by the App's IDB persist). */
  get lastSeqValue(): number {
    const vals = Object.values(this.lastSeqBySession);
    return vals.length ? Math.max(...vals) : 0;
  }

  /** Highest sequence seen for one session (never borrow another session's cursor). */
  lastSeqFor(sid: string): number {
    return this.lastSeqBySession[sid] ?? 0;
  }

  generationFor(sid: string): string | undefined {
    return this.generationBySession[sid];
  }

  /** Runtime ids that must survive client-side reclamation while commands await ACK. */
  pendingSessionIds(): string[] {
    return [...new Set([
      ...this.outbox.pendingSessionIds(),
      ...this.queryAcceptance.pendingSessionIds(),
    ])];
  }

  pendingQueryFor(sid: string): string | null {
    return this.queryAcceptance.pendingMessageId(sid);
  }

  private touchReplay(sid: string): void {
    this.replayOrder = this.replayOrder.filter((known) => known !== sid);
    this.replayOrder.push(sid);
    while (this.replayOrder.length > MAX_REPLAY_SESSIONS) {
      const expired = this.replayOrder.shift();
      if (!expired) break;
      delete this.lastSeqBySession[expired];
      delete this.generationBySession[expired];
      delete this.controlBySession[expired];
      delete this.engineBySession[expired];
      delete this.spaceBySession[expired];
      delete this.historyHeadBySession[expired];
      this.rebuildingSessions.delete(expired);
    }
  }

  private dropBtwReplayState(): void {
    this.knownBtwSids.clear();
    for (const knownSid of Object.keys(this.generationBySession)) {
      if (!knownSid.startsWith("btw-")) continue;
      delete this.generationBySession[knownSid];
      delete this.controlBySession[knownSid];
      delete this.lastSeqBySession[knownSid];
      delete this.engineBySession[knownSid];
      delete this.spaceBySession[knownSid];
      delete this.historyHeadBySession[knownSid];
      this.rebuildingSessions.delete(knownSid);
      this.replayOrder = this.replayOrder.filter((item) => item !== knownSid);
    }
  }

  private noteWrapperGeneration(generation: string): void {
    const previous = this.wrapperGeneration;
    this.wrapperGeneration = generation;
    if (previous === null || previous === generation
        || this.lastGenerationChangeNotice === generation) return;
    this.lastGenerationChangeNotice = generation;
    // BtwOpened precedes the fork's generation-bearing Snapshot. If that second
    // frame is lost, checking only per-btw generation state leaves a dead fork
    // visible after restart. Every confirmed wrapper generation change is the
    // authoritative invalidation boundary for ephemeral forks.
    this.cb.onWrapperGenerationChanged?.();
    this.dropBtwReplayState();
  }

  private noteGeneration(sid: string, generation: string): void {
    this.noteWrapperGeneration(generation);
    const previous = this.generationBySession[sid];
    if (previous !== undefined && previous !== generation) {
      // seq is monotonic only within a wrapper generation. Snapshot can be the
      // first per-session proof of a restart, so reset here as well as on an
      // explicit rebuild envelope.
      this.lastSeqBySession[sid] = 0;
      delete this.controlBySession[sid];
    }
    this.generationBySession[sid] = generation;
    this.touchReplay(sid);
  }

  private acceptControl(sid: string, incoming: SessionControl): boolean {
    const incomingGeneration = incoming.generation ?? null;
    const activeGeneration = this.generationBySession[sid] ?? null;
    if (activeGeneration !== null && incomingGeneration !== activeGeneration) {
      return false;
    }
    if (activeGeneration === null && incomingGeneration !== null) {
      this.noteGeneration(sid, incomingGeneration);
    }
    const disposition = compareSessionControl(
      this.controlBySession[sid], incoming);
    if (disposition === "newer") this.controlBySession[sid] = incoming;
    return disposition === "newer" || disposition === "same";
  }

  /** Keep narrative/snapshot envelopes while stripping only an obsolete nested
   * control value. A direct obsolete SessionControl frame can be dropped whole. */
  private filterControl(msg: ServerEvent): ServerEvent | null {
    if (msg.type === "session_control") {
      // A live control frame without a routing key must never fall through to
      // the reducer's focused-session fallback. Embedded controls are routed by
      // their trusted Snapshot/History envelope instead.
      if (!msg.sid) return null;
      return this.acceptControl(msg.sid, msg) ? msg : null;
    }
    if (msg.type !== "snapshot" && msg.type !== "history") return msg;
    const control = msg.control;
    const sid = msg.type === "history"
      ? msg.session_id : (msg.sid ?? msg.cc_session_id);
    // The outer snapshot/history generation is the trusted epoch switch. Move
    // the transport watermark before comparing its embedded control revision.
    if (sid && msg.generation) this.noteGeneration(sid, msg.generation);
    if (!control) return msg;
    if (sid && !sessionControlTargetsSid(control, sid)) {
      return { ...msg, control: undefined } as ServerEvent;
    }
    if (!sid || this.acceptControl(sid, control)) return msg;
    return { ...msg, control: undefined } as ServerEvent;
  }

  private boundedReplayState(): {
    cursors: Record<string, number>; generations: Record<string, string>;
  } {
    const cursors: Record<string, number> = {};
    const generations: Record<string, string> = {};
    for (const sid of this.replayOrder.slice(-MAX_REPLAY_SESSIONS)) {
      const seq = this.lastSeqBySession[sid];
      if (typeof seq !== "number") continue;
      cursors[sid] = seq;
      const generation = this.generationBySession[sid];
      if (generation) generations[sid] = generation;
    }
    return { cursors, generations };
  }

  /** Track the highest seq seen for a session (so reconnect replays from there). */
  noteSeq(sid: string | null | undefined, seq: number | null | undefined): void {
    if (sid && typeof seq === "number") {
      this.lastSeqBySession[sid] = Math.max(this.lastSeqBySession[sid] ?? 0, seq);
      this.touchReplay(sid);
    }
  }

  /** Seed a session's cursor (e.g. from the IndexedDB cache on load). */
  setLastSeq(sid: string, seq: number): void {
    this.lastSeqBySession[sid] = seq;
    this.touchReplay(sid);
  }

  /** Bulk-seed cursors from the IndexedDB cache BEFORE the first hello, so the
   *  wrapper replays only the delta (seq > lastSeq) instead of the whole history
   *  of every resident session — that flood is what wedged reconnect into a loop. */
  seedReplayState(
    cursors: Record<string, number>, generations: Record<string, string>,
    controls: Record<string, SessionControl> = {},
  ): void {
    for (const [sid, seq] of Object.entries(cursors)) {
      if (typeof seq === "number" && seq > (this.lastSeqBySession[sid] ?? 0)) {
        this.lastSeqBySession[sid] = seq;
        this.touchReplay(sid);
      }
    }
    for (const [sid, generation] of Object.entries(generations)) {
      if (generation && (this.lastSeqBySession[sid] != null
          || controls[sid] != null)) {
        this.noteGeneration(sid, generation);
      }
    }
    for (const [sid, control] of Object.entries(controls)) {
      if (this.acceptControl(sid, control)) this.touchReplay(sid);
    }
  }

  setFocusedSid(sid: string | null, engine?: "claude" | "codex", space?: Space): void {
    this.focusedSid = sid;
    if (sid) {
      if (engine) this.engineBySession[sid] = engine;
      if (space) this.spaceBySession[sid] = space;
      this.touchReplay(sid);
    }
    if (this.newSessionFocusRequestId) {
      delete this.pendingOwnershipByRequest[this.newSessionFocusRequestId];
    }
    this.newSessionFocusRequestId = null;
  }

  /** Set the visible product surface and drop a focus owned by another one. */
  setSurface(engine: "claude" | "codex", space: Space): void {
    const changed = engine !== this.activeEngine || space !== this.activeSpace;
    this.activeEngine = engine;
    this.activeSpace = space;
    if (changed) {
      this.surfaceEpoch += 1;
      this.surfaceEpochByScope[
        sessionScopeKey(this.machineId, engine, space)
      ] = this.surfaceEpoch;
    }
    if (this.focusedSid && !this.sessionMatchesSurface(this.focusedSid, engine, space)) {
      this.focusedSid = null;
    }
    if (changed && this.newSessionFocusRequestId
        && (this.newSessionEngine !== engine || this.newSessionSpace !== space)) {
      delete this.pendingOwnershipByRequest[this.newSessionFocusRequestId];
      this.newSessionFocusRequestId = null;
    }
  }

  private sessionMatchesSurface(
    sid: string, engine = this.activeEngine, space = this.activeSpace,
  ): boolean {
    return this.engineBySession[sid] === engine && this.spaceBySession[sid] === space;
  }

  setSessionEngines(sessions: Array<{
    session_id: string;
    engine?: string | null;
    space?: Space | null;
    claude_profile_id?: string | null;
    codex_profile_id?: string | null;
  }>): void {
    for (const session of sessions) {
      if (session.engine === "codex" || session.engine === "claude") {
        this.engineBySession[session.session_id] = session.engine;
      }
      if (session.space === "work" || session.space === "code") {
        this.spaceBySession[session.session_id] = session.space;
      }
      if (session.codex_profile_id) {
        this.codexProfileBySession[session.session_id] =
          session.codex_profile_id;
      }
      if (session.claude_profile_id) {
        this.claudeProfileBySession[session.session_id] =
          session.claude_profile_id;
      }
    }
  }

  private ownershipSnapshot(
    engine = this.activeEngine, space = this.activeSpace,
    codexProfileId?: string | null,
    claudeProfileId?: string | null,
  ): EventOwnership {
    const scopeKey = sessionScopeKey(this.machineId, engine, space);
    return {
      scopeKey,
      machineId: this.machineId,
      engine,
      space,
      claudeProfileId: engine === "claude" ? (claudeProfileId ?? null) : null,
      codexProfileId: engine === "codex" ? (codexProfileId ?? null) : null,
      surfaceEpoch: this.surfaceEpochByScope[scopeKey] ?? 0,
      connectionGeneration: this.connectionGeneration,
    };
  }

  private acceptsOwnership(
    ownership: EventOwnership | undefined, socketGeneration: number,
  ): ownership is EventOwnership {
    return !!ownership
      && ownership.connectionGeneration === socketGeneration
      && ownership.connectionGeneration === this.connectionGeneration
      && this.surfaceEpochByScope[ownership.scopeKey] === ownership.surfaceEpoch;
  }

  private queueOwnership(
    target: Record<string, EventOwnership[]>, key: string,
    ownership: EventOwnership,
  ): void {
    const queue = target[key] ?? [];
    queue.push(ownership);
    target[key] = queue.slice(-32);
  }

  private shiftOwnership(
    target: Record<string, EventOwnership[]>, key: string,
  ): EventOwnership | undefined {
    const queue = target[key];
    const ownership = queue?.shift();
    if (queue?.length === 0) delete target[key];
    return ownership;
  }

  private sidObj(targetSid?: string | null): Record<string, unknown> {
    const sid = targetSid ?? this.focusedSid;
    return sid ? { sid } : {};
  }

  sendQuery(prompt: string, msg_id: string, images?: QueryImg[], files?: QueryFile[]): boolean {
    const sid = this.focusedSid;
    if (!sid || this.queryAcceptance.pendingMessageId(sid)) return false;
    const sentAt = nowTs();
    const obj: Record<string, unknown> = {
      v: PROTOCOL_VERSION, type: "query", prompt, msg_id, sid, ts: sentAt,
    };
    if (images && images.length) obj.images = images;
    if (files && files.length) obj.files = files;
    if (!this.send(obj)) return false;
    const historyHead = this.historyHeadBySession[sid];
    const baseline = historyHead ? {
      ...historyHead,
      liveSeq: Math.max(
        historyHead.liveSeq, this.lastSeqBySession[sid] ?? 0),
    } : null;
    return this.queryAcceptance.begin(
      sid, msg_id,
      queryAcceptanceDescriptor(msg_id, prompt, images, files),
      baseline,
    );
  }

  // ---- /btw ephemeral side-fork ----
  sendOpenBtw(parentSid: string, requestId = uuid()): string | null {
    const queued = this.send({
      ...makeOpenBtwCommand(parentSid, requestId, nowTs()),
    });
    return queued ? requestId : null;
  }
  sendCloseBtw(btwSid: string): boolean {
    return this.send({
      v: PROTOCOL_VERSION, type: "close_btw", sid: btwSid, ts: nowTs(),
    });
  }

  sendSyncBtw(btwSid: string): boolean {
    const command: Record<string, unknown> = {
      v: PROTOCOL_VERSION,
      type: "sync_btw",
      sid: btwSid,
      cursor: this.lastSeqFor(btwSid),
      ts: nowTs(),
    };
    const generation = this.generationFor(btwSid);
    if (generation) command.generation = generation;
    return this.send(command);
  }

  sendForkSessionWorktree(parentSessionId: string, name: string,
                          requestId = uuid(), lastTurnId?: string): string | null {
    const queued = this.sendListRefreshingCommand({
      ...makeForkSessionWorktreeCommand(
        parentSessionId, name, requestId, nowTs(), lastTurnId),
    }, "codex", "code") !== null;
    return queued ? requestId : null;
  }

  sendForkSession(parentSessionId: string, forkPointId: string,
                  requestId = uuid()): string | null {
    const engine = this.engineBySession[parentSessionId] ?? this.activeEngine;
    const queued = this.sendListRefreshingCommand({
      ...makeForkSessionCommand(parentSessionId, forkPointId, requestId, nowTs()),
    }, engine, "code") !== null;
    return queued ? requestId : null;
  }

  sendMigrateSession(sessionId: string, cwd: string,
                     requestId = uuid()): string | null {
    const queued = this.sendListRefreshingCommand({
      ...makeMigrateSessionCommand(sessionId, cwd, requestId, nowTs()),
    }, "codex", "code") !== null;
    return queued ? requestId : null;
  }

  // a turn targeted at an explicit sid (the btw fork), NOT the focused session.
  sendQueryTo(sid: string, prompt: string, msg_id: string,
              images?: QueryImg[], files?: QueryFile[]): boolean {
    if (this.queryAcceptance.pendingMessageId(sid)) return false;
    const sentAt = nowTs();
    const obj: Record<string, unknown> = {
      v: PROTOCOL_VERSION, type: "query", prompt, msg_id, sid, ts: sentAt,
    };
    if (images && images.length) obj.images = images;
    if (files && files.length) obj.files = files;
    if (!this.send(obj)) return false;
    const historyHead = this.historyHeadBySession[sid];
    const baseline = historyHead ? {
      ...historyHead,
      liveSeq: Math.max(
        historyHead.liveSeq, this.lastSeqBySession[sid] ?? 0),
    } : null;
    return this.queryAcceptance.begin(
      sid, msg_id,
      queryAcceptanceDescriptor(msg_id, prompt, images, files),
      baseline,
    );
  }

  /** Transfer follow-up ownership to the wrapper immediately. Unlike an
   * immediate Query, a deferred query must not open the browser's narrative
   * acceptance latch: it may remain queued long after this tab is suspended. */
  sendDeferredQueryTo(
    sid: string,
    prompt: string,
    msg_id: string,
    delivery: "queue" | "replace",
    images?: QueryImg[],
    files?: QueryFile[],
  ): boolean {
    const obj: Record<string, unknown> = {
      v: PROTOCOL_VERSION,
      type: "query",
      prompt,
      msg_id,
      sid,
      delivery,
      ts: nowTs(),
    };
    if (images && images.length) obj.images = images;
    if (files && files.length) obj.files = files;
    return this.send(obj);
  }

  sendCancelQueuedQueryTo(sid: string, msg_id: string): boolean {
    return this.send({
      v: PROTOCOL_VERSION,
      type: "cancel_queued_query",
      sid,
      msg_id,
      ts: nowTs(),
    });
  }

  sendGetQueuedQueryTo(sid: string, msg_id: string): string | null {
    return this.sendTracked({
      v: PROTOCOL_VERSION,
      type: "get_queued_query",
      sid,
      msg_id,
      ts: nowTs(),
    });
  }

  sendUpdateQueuedQueryTo(
    sid: string, msg_id: string, prompt: string,
  ): string | null {
    return this.sendTracked({
      v: PROTOCOL_VERSION,
      type: "update_queued_query",
      sid,
      msg_id,
      prompt,
      ts: nowTs(),
    });
  }

  /** Append input to the active Codex turn. The reliable command and the
   *  narrative acceptance latch are separate: an ACK alone must not clear the
   *  draft/runtime protection before the wrapper echoes the steered user row. */
  sendSteerTo(sid: string, prompt: string, msg_id: string,
              images?: QueryImg[], files?: QueryFile[]): boolean {
    if (!sid) return false;
    if (this.queryAcceptance.pendingMessageId(sid)) return false;
    const sentAt = nowTs();
    const obj: Record<string, unknown> = {
      v: PROTOCOL_VERSION, type: "steer", prompt, msg_id, sid, ts: sentAt,
    };
    if (images && images.length) obj.images = images;
    if (files && files.length) obj.files = files;
    if (!this.send(obj)) return false;
    const historyHead = this.historyHeadBySession[sid];
    const baseline = historyHead ? {
      ...historyHead,
      liveSeq: Math.max(
        historyHead.liveSeq, this.lastSeqBySession[sid] ?? 0),
    } : null;
    return this.queryAcceptance.begin(
      sid, msg_id,
      queryAcceptanceDescriptor(msg_id, prompt, images, files),
      baseline,
    );
  }

  sendInterrupt(): void {
    this.send({ v: PROTOCOL_VERSION, type: "interrupt", ts: nowTs(), ...this.sidObj() });
  }

  sendInterruptTo(sid: string): void {
    this.send({ v: PROTOCOL_VERSION, type: "interrupt", sid, ts: nowTs() });
  }

  sendTakeover(sid: string): boolean {
    return this.send({ v: PROTOCOL_VERSION, type: "takeover", sid, ts: nowTs() });
  }

  sendSetModel(model: string): void {
    this.send({ v: PROTOCOL_VERSION, type: "set_model", model, ts: nowTs(), ...this.sidObj() });
  }

  sendSetModelTo(sid: string, model: string): void {
    this.send({ v: PROTOCOL_VERSION, type: "set_model", sid, model, ts: nowTs() });
  }

  sendSetEffort(effort: string): void {
    this.send({ v: PROTOCOL_VERSION, type: "set_effort", effort, ts: nowTs(), ...this.sidObj() });
  }

  sendSetEffortTo(sid: string, effort: string): void {
    this.send({ v: PROTOCOL_VERSION, type: "set_effort", sid, effort, ts: nowTs() });
  }

  sendSetAutoCompact(
    mode: AutoCompactMode, thresholdTokens?: number | null,
  ): boolean {
    if (mode === "custom" && !validAutoCompactThreshold(thresholdTokens)) {
      return false;
    }
    return this.send({
      v: PROTOCOL_VERSION,
      type: "set_auto_compact",
      mode,
      ...(mode === "custom"
        ? { threshold_tokens: thresholdTokens }
        : {}),
      ts: nowTs(),
      ...this.sidObj(),
    });
  }

  sendSetAutoCompactTo(
    sid: string, mode: AutoCompactMode, thresholdTokens?: number | null,
  ): boolean {
    if (mode === "custom" && !validAutoCompactThreshold(thresholdTokens)) {
      return false;
    }
    return this.send({
      v: PROTOCOL_VERSION,
      type: "set_auto_compact",
      sid,
      mode,
      ...(mode === "custom"
        ? { threshold_tokens: thresholdTokens }
        : {}),
      ts: nowTs(),
    });
  }

  sendSetServiceTier(service_tier: string): void {
    this.send({ v: PROTOCOL_VERSION, type: "set_service_tier", service_tier, ts: nowTs(), ...this.sidObj() });
  }

  sendSetCollaborationMode(mode: "default" | "plan"): void {
    this.send({ v: PROTOCOL_VERSION, type: "set_collaboration_mode", mode, ts: nowTs(), ...this.sidObj() });
  }

  sendSetPerm(mode: string): void {
    this.send({ v: PROTOCOL_VERSION, type: "set_perm", mode, ts: nowTs(), ...this.sidObj() });
  }

  sendGetPermissionProfiles(
    cwd?: string,
    codexProfileId?: string | null,
  ): string | null {
    return this.sendTracked({
      v: PROTOCOL_VERSION,
      type: "get_permission_profiles",
      ts: nowTs(),
      ...(codexProfileId ? { codex_profile_id: codexProfileId } : {}),
      ...(cwd ? { cwd } : this.sidObj()),
    });
  }

  sendSetPermissionProfile(profile: string): string | null {
    return this.sendTracked({
      v: PROTOCOL_VERSION,
      type: "set_permission_profile",
      profile,
      ts: nowTs(),
      ...this.sidObj(),
    });
  }

  sendSetWebSearch(mode: "cached" | "live"): void {
    this.send({
      v: PROTOCOL_VERSION,
      type: "set_web_search",
      mode,
      ts: nowTs(),
      ...this.sidObj(),
    });
  }

  sendGetContext(refresh = false): string | null {
    return this.sendTracked({
      v: PROTOCOL_VERSION, type: "get_context", refresh,
      ts: nowTs(), ...this.sidObj(),
    });
  }

  sendGetContextTo(sid: string, refresh = false): string | null {
    return this.sendTracked({
      v: PROTOCOL_VERSION, type: "get_context", sid, refresh, ts: nowTs(),
    });
  }

  sendGetDiff(file: string, theme: DiffTheme, turnId?: string, revision?: string, engine?: Engine): string | null {
    return this.sendTracked({
      v: PROTOCOL_VERSION, type: "get_diff", file, theme,
      ...(turnId ? { turn_id: turnId, revision, engine } : {}),
      ts: nowTs(), ...this.sidObj(),
    });
  }

  sendGetTurnFileChanges(sid: string, engine: Engine, turnId: string, revision: string, offset: number): string | null {
    return this.sendTracked({
      v: PROTOCOL_VERSION, type: "get_turn_file_changes", sid, engine,
      turn_id: turnId, revision, offset, limit: 64, ts: nowTs(),
    });
  }

  sendGetFilePreview(
    path: string,
    requestId = uuid(),
    targetSid?: string | null,
  ): string | null {
    const queued = this.send({
      v: PROTOCOL_VERSION, type: "get_file_preview", path, request_id: requestId,
      ts: nowTs(), ...this.sidObj(targetSid),
    });
    return queued ? requestId : null;
  }

  sendBrowseFiles(sid: string, path: string, requestId: string,
                  hidden = false, offset = 0, revision?: string | null): boolean {
    return this.send({ v: PROTOCOL_VERSION, type: "browse_files", sid, path,
      request_id: requestId, hidden, offset, limit: 100, revision, ts: nowTs() });
  }

  sendCodexContext(sid: string, threshold: number | null): boolean {
    if (threshold !== null && (!Number.isSafeInteger(threshold) || threshold < 1 || threshold > 100_000_000)) return false;
    return !!this.sendTracked({ v: PROTOCOL_VERSION, type: "set_codex_context",
      sid, threshold_tokens: threshold, ts: nowTs() });
  }

  sendSaveMarkdown(path: string, content: string, expectedSize: number,
                   expectedMtimeNs: string, expectedRevision: string,
                   requestId = uuid(),
                   targetSid?: string | null): string | null {
    const queued = this.send({
      v: PROTOCOL_VERSION,
      type: "save_markdown",
      path,
      content,
      expected_size: expectedSize,
      expected_mtime_ns: expectedMtimeNs,
      expected_revision: expectedRevision,
      request_id: requestId,
      ts: nowTs(),
      ...this.sidObj(targetSid),
    });
    return queued ? requestId : null;
  }

  sendGetPreviewAsset(path: string, previewId: string,
                      requestId = uuid(),
                      targetSid?: string | null): string | null {
    const queued = this.send({
      v: PROTOCOL_VERSION, type: "get_preview_asset", path,
      preview_id: previewId, request_id: requestId,
      ts: nowTs(), ...this.sidObj(targetSid),
    });
    return queued ? requestId : null;
  }

  sendAuthorizePreview(
    authorizationId: string,
    requestId: string,
    decision: "allow" | "deny",
    targetSid?: string | null,
  ): string | null {
    const key = JSON.stringify([this.sidObj(targetSid).sid ?? null, requestId]);
    const previous = this.previewReadAttempts.get(key);
    // Automatic read handshakes must not loop if a producer keeps atomically
    // replacing the file. Same challenge is idempotent; a changed challenge
    // requires a fresh read request (e.g. the existing Refresh action).
    if (decision === "allow" && previous) {
      return previous.authorizationId === authorizationId ? previous.commandId : null;
    }
    const commandId = this.sendTracked({
      v: PROTOCOL_VERSION,
      type: "authorize_preview",
      authorization_id: authorizationId,
      request_id: requestId,
      decision,
      ts: nowTs(),
      ...this.sidObj(targetSid),
    });
    if (commandId && decision === "allow") {
      this.previewReadAttempts.set(key, { authorizationId, commandId });
      if (this.previewReadAttempts.size > 256) {
        this.previewReadAttempts.delete(this.previewReadAttempts.keys().next().value!);
      }
    }
    return commandId;
  }

  /** Fetch a small canonical conversation page. Heavy per-turn detail remains
   *  in the wrapper materialized index until the user expands it. */
  sendGetHistory(
    sessionId: string,
    before?: string | null,
    limit?: number | null,
    cwd?: string | null,
  ): boolean {
    const obj: Record<string, unknown> = {
      v: PROTOCOL_VERSION, type: "get_history", session_id: sessionId,
      client_id: this.clientId, detail: "summary", ts: nowTs(),
    };
    if (cwd) obj.cwd = cwd;
    if (before) obj.before = before;
    if (limit) obj.limit = limit;
    return this.send(obj);
  }

  sendGetTurnDetail(
    sessionId: string, turnId: string, revision?: string | null,
    before?: string | null, limit = 192,
  ): boolean {
    const frame: Record<string, unknown> = {
      v: PROTOCOL_VERSION, type: "get_turn_detail",
      session_id: sessionId, turn_id: turnId,
      client_id: this.clientId, limit, ts: nowTs(),
    };
    if (revision) frame.revision = revision;
    if (before) frame.before = before;
    return this.send(frame);
  }

  sendGetAgentDetail(
    sessionId: string, runId: string, revision?: string | null,
    detailRevision?: string | null, before?: string | null, limit = 192,
    requestId = uuid(),
  ): string | null {
    const frame: Record<string, unknown> = {
      v: PROTOCOL_VERSION, type: "get_agent_detail",
      session_id: sessionId, run_id: runId, request_id: requestId,
      client_id: this.clientId, limit, ts: nowTs(),
    };
    if (revision) frame.revision = revision;
    if (detailRevision) frame.detail_revision = detailRevision;
    if (before) frame.before = before;
    return this.send(frame) ? requestId : null;
  }

  sendGetHistoryImage(
    sessionId: string,
    turnId: string,
    imageId: string,
    variant: "thumbnail" | "full",
    requestId: string,
    revision?: string | null,
  ): boolean {
    const frame: Record<string, unknown> = {
      v: PROTOCOL_VERSION, type: "get_history_image",
      session_id: sessionId, turn_id: turnId, image_id: imageId,
      variant, request_id: requestId, client_id: this.clientId, ts: nowTs(),
    };
    if (revision) frame.revision = revision;
    return this.send(frame);
  }

  /** Ask the engine for its catalog and explicit new-session defaults. Claude
   *  needs cwd because project/local settings can change the selected model. */
  sendGetModels(
    engine: "cc" | "claude" | "codex",
    cwd?: string | null,
    codexProfileId?: string | null,
    claudeProfileId?: string | null,
  ): void {
    const frame: Record<string, unknown> = {
      v: PROTOCOL_VERSION, type: "get_models", engine,
      client_id: this.clientId, ts: nowTs(),
    };
    if (cwd) frame.cwd = cwd;
    if (engine === "codex" && codexProfileId) {
      frame.codex_profile_id = codexProfileId;
    }
    if (engine === "claude" && claudeProfileId) {
      frame.claude_profile_id = claudeProfileId;
    }
    this.send(frame);
  }

  sendGetEngineCapabilities(engine: "claude" | "codex", space: Space,
                            cwd?: string | null,
                            skillsOnly = false,
                            codexProfileId?: string | null,
                            claudeProfileId?: string | null): string | null {
    const frame: Record<string, unknown> = {
      v: PROTOCOL_VERSION, type: "get_engine_capabilities", engine, space,
      client_id: this.clientId, skills_only: skillsOnly, ts: nowTs(),
    };
    if (cwd) frame.cwd = cwd;
    if (engine === "codex" && codexProfileId) {
      frame.codex_profile_id = codexProfileId;
    }
    if (engine === "claude" && claudeProfileId) {
      frame.claude_profile_id = claudeProfileId;
    }
    return this.sendTracked(frame);
  }

  sendManageEnginePlugin(engine: "claude" | "codex", space: Space,
                         action: "install" | "uninstall", pluginId: string,
                         cwd?: string | null,
                         codexProfileId?: string | null,
                         claudeProfileId?: string | null): string | null {
    const frame: Record<string, unknown> = {
      v: PROTOCOL_VERSION, type: "manage_engine_plugin", engine, space,
      action, plugin_id: pluginId, client_id: this.clientId, ts: nowTs(),
    };
    if (cwd) frame.cwd = cwd;
    if (engine === "codex" && codexProfileId) {
      frame.codex_profile_id = codexProfileId;
    }
    if (engine === "claude" && claudeProfileId) {
      frame.claude_profile_id = claudeProfileId;
    }
    return this.sendTracked(frame);
  }

  sendManageEngineSkill(
    engine: "claude" | "codex", space: Space,
    action: "create" | "remove" | "enable" | "disable",
    options: {
      skillId?: string; name?: string; description?: string;
      instructions?: string; scope?: "user" | "project";
    },
    cwd?: string | null,
    codexProfileId?: string | null,
    claudeProfileId?: string | null,
  ): string | null {
    const frame: Record<string, unknown> = {
      v: PROTOCOL_VERSION, type: "manage_engine_skill", engine, space, action,
      client_id: this.clientId, ts: nowTs(),
    };
    if (options.skillId) frame.skill_id = options.skillId;
    if (options.name) frame.name = options.name;
    if (options.description !== undefined) frame.description = options.description;
    if (options.instructions !== undefined) frame.instructions = options.instructions;
    if (options.scope) frame.scope = options.scope;
    if (cwd) frame.cwd = cwd;
    if (engine === "codex" && codexProfileId) {
      frame.codex_profile_id = codexProfileId;
    }
    if (engine === "claude" && claudeProfileId) {
      frame.claude_profile_id = claudeProfileId;
    }
    return this.sendTracked(frame);
  }

  sendManageEngineHook(
    engine: "claude" | "codex", space: Space,
    action: "create" | "remove",
    options: {
      hookId?: string; event?: string; matcher?: string; command?: string;
      timeout?: number; scope?: "user" | "project";
    },
    cwd?: string | null,
    codexProfileId?: string | null,
    claudeProfileId?: string | null,
  ): string | null {
    const frame: Record<string, unknown> = {
      v: PROTOCOL_VERSION, type: "manage_engine_hook", engine, space, action,
      client_id: this.clientId, ts: nowTs(),
    };
    if (options.hookId) frame.hook_id = options.hookId;
    if (options.event) frame.event = options.event;
    if (options.matcher !== undefined) frame.matcher = options.matcher;
    if (options.command !== undefined) frame.command = options.command;
    if (options.timeout !== undefined) frame.timeout = options.timeout;
    if (options.scope) frame.scope = options.scope;
    if (cwd) frame.cwd = cwd;
    if (engine === "codex" && codexProfileId) {
      frame.codex_profile_id = codexProfileId;
    }
    if (engine === "claude" && claudeProfileId) {
      frame.claude_profile_id = claudeProfileId;
    }
    return this.sendTracked(frame);
  }

  sendAnswerQuestion(
    sid: string, askId: string, answer: string | string[],
  ): boolean {
    return this.send({
      v: PROTOCOL_VERSION, type: "answer_question",
      ask_id: askId, answer, sid, ts: nowTs(),
    });
  }

  sendGetGoal(): string | null {
    const sid = this.focusedSid;
    return sid ? this.sendGetGoalTo(sid) : null;
  }

  sendGetGoalTo(sid: string): string | null {
    const engine = this.engineBySession[sid]
      ?? (sid === this.focusedSid ? this.activeEngine : undefined);
    const space = this.spaceBySession[sid]
      ?? (sid === this.focusedSid ? this.activeSpace : undefined);
    const ownership = engine && space
      ? this.ownershipSnapshot(engine, space) : undefined;
    const commandId = uuid();
    if (ownership) {
      this.pendingGoalOwnershipByRequest.set(commandId, ownership);
      while (this.pendingGoalOwnershipByRequest.size > OUTBOX_MAX_COMMANDS) {
        const oldest = this.pendingGoalOwnershipByRequest.keys().next().value;
        if (typeof oldest !== "string") break;
        this.pendingGoalOwnershipByRequest.delete(oldest);
      }
    }
    const requestId = this.sendTracked({
      v: PROTOCOL_VERSION, type: "get_goal", sid,
      client_id: this.clientId, ts: nowTs(),
    }, commandId);
    if (!requestId) this.pendingGoalOwnershipByRequest.delete(commandId);
    return requestId;
  }

  sendGetStatus(): string | null {
    return this.sendTracked({
      v: PROTOCOL_VERSION, type: "get_status", client_id: this.clientId,
      ts: nowTs(), ...this.sidObj(),
    });
  }

  sendGetStatusTo(sid: string): string | null {
    return this.sendTracked({
      v: PROTOCOL_VERSION, type: "get_status", sid,
      client_id: this.clientId, ts: nowTs(),
    });
  }

  sendConsumeRateLimitResetCredit(
    sid: string,
    creditId?: string | null,
  ): string | null {
    return this.sendTracked({
      v: PROTOCOL_VERSION,
      type: "consume_rate_limit_reset_credit",
      sid,
      client_id: this.clientId,
      ...(creditId ? { credit_id: creditId } : {}),
      ts: nowTs(),
    });
  }

  sendSetGoal(objective: string | null, status: GoalStatus | null, tokenBudget: number | null): void {
    const obj: Record<string, unknown> = { v: PROTOCOL_VERSION, type: "set_goal", ts: nowTs(), ...this.sidObj() };
    if (objective !== null) obj.objective = objective;
    if (status !== null) obj.status = status;
    if (tokenBudget !== null) obj.token_budget = tokenBudget;
    this.send(obj);
  }

  sendClearGoal(): void {
    this.send({ v: PROTOCOL_VERSION, type: "clear_goal", ts: nowTs(), ...this.sidObj() });
  }

  sendDismissGoalTo(sid: string, goalId: string): string | null {
    return this.sendTracked({
      v: PROTOCOL_VERSION,
      type: "dismiss_goal",
      sid,
      goal_id: goalId,
      client_id: this.clientId,
      ts: nowTs(),
    });
  }

  sendAcknowledgeCompletionTo(
    sid: string,
    completionId: string,
  ): string | null {
    return this.sendTracked({
      v: PROTOCOL_VERSION,
      type: "acknowledge_completion",
      sid,
      completion_id: completionId,
      client_id: this.clientId,
      ts: nowTs(),
    });
  }

  sendListSessions(engine?: "claude" | "codex", space: Space = "code"): boolean {
    const targetEngine = engine ?? "claude";
    const obj: Record<string, unknown> = { v: PROTOCOL_VERSION, type: "list_sessions", ts: nowTs() };
    if (engine && engine !== "claude") obj.engine = engine;
    if (space !== "code") obj.space = space;
    return this.sendListRefreshingCommand(obj, targetEngine, space) !== null;
  }

  /** Session mutations return a SessionList correlated to the mutation's own
   * cmd_id. Freeze the same request-time ownership used by an explicit list so
   * those authoritative refreshes cannot borrow the surface active at receive
   * time or be dropped as unowned. */
  private sendListRefreshingCommand(
    obj: Record<string, unknown>, engine: "claude" | "codex", space: Space,
    commandId = uuid(),
  ): string | null {
    const ownership = this.ownershipSnapshot(engine, space);
    this.pendingListOwnershipByRequest.set(commandId, {
      ownership,
      order: ++this.listRequestOrder,
    });
    while (this.pendingListOwnershipByRequest.size > OUTBOX_MAX_COMMANDS) {
      const oldest = this.pendingListOwnershipByRequest.keys().next().value;
      if (typeof oldest !== "string") break;
      this.pendingListOwnershipByRequest.delete(oldest);
    }
    const queued = this.sendTracked(obj, commandId) !== null;
    if (!queued) this.pendingListOwnershipByRequest.delete(commandId);
    return queued ? commandId : null;
  }

  private refreshInvalidatedSessionList(
    engine: "claude" | "codex",
    space: Space,
    socketGeneration: number,
  ): void {
    if (socketGeneration !== this.connectionGeneration
        || engine !== this.activeEngine || space !== this.activeSpace) return;
    const scopeKey = sessionScopeKey(this.machineId, engine, space);
    const inFlight = this.invalidatedListRefreshByScope.get(scopeKey);
    if (inFlight) {
      inFlight.dirty = true;
      return;
    }
    const obj: Record<string, unknown> = {
      v: PROTOCOL_VERSION,
      type: "list_sessions",
      ts: nowTs(),
    };
    if (engine !== "claude") obj.engine = engine;
    if (space !== "code") obj.space = space;
    const requestId = this.sendListRefreshingCommand(obj, engine, space);
    if (!requestId) return;
    this.invalidatedListRefreshByScope.set(scopeKey, {
      requestId, engine, space, dirty: false,
    });
    this.invalidatedListScopeByRequest.set(requestId, scopeKey);
  }

  private completeInvalidatedSessionListRefresh(
    requestId: string,
    socketGeneration: number,
  ): void {
    const scopeKey = this.invalidatedListScopeByRequest.get(requestId);
    if (!scopeKey) return;
    this.invalidatedListScopeByRequest.delete(requestId);
    const refresh = this.invalidatedListRefreshByScope.get(scopeKey);
    if (!refresh || refresh.requestId !== requestId) return;
    this.invalidatedListRefreshByScope.delete(scopeKey);
    if (refresh.dirty) {
      this.refreshInvalidatedSessionList(
        refresh.engine, refresh.space, socketGeneration);
    }
  }

  sendSwitchSession(sessionId: string, engine?: "claude" | "codex", space: Space = "code"): void {
    const targetEngine = engine ?? this.activeEngine;
    if (engine) this.engineBySession[sessionId] = engine;
    this.spaceBySession[sessionId] = space;
    const ownership = this.ownershipSnapshot(
      targetEngine,
      space,
      targetEngine === "codex"
        ? this.codexProfileBySession[sessionId]
          ?? this.ownershipBySession[sessionId]?.codexProfileId
        : null,
      targetEngine === "claude"
        ? this.claudeProfileBySession[sessionId]
          ?? this.ownershipBySession[sessionId]?.claudeProfileId
        : null,
    );
    const obj: Record<string, unknown> = { v: PROTOCOL_VERSION, type: "switch_session", session_id: sessionId, ts: nowTs() };
    if (engine && engine !== "claude") obj.engine = engine;
    if (space !== "code") obj.space = space;
    if (this.send(obj)) {
      this.queueOwnership(this.pendingSwitchOwnership, sessionId, ownership);
    }
  }

  sendNewSession(cwd?: string | null, engine?: "claude" | "codex",
                 model?: string | null, effort?: string | null,
                 initial?: { prompt: string; msg_id: string; images?: QueryImg[]; files?: QueryFile[] },
                 collaborationMode?: "default" | "plan",
                 permissionMode?: "never" | "on-request" | "untrusted",
                 permissionProfile?: string,
                 webSearch?: "cached" | "live",
                 serviceTier?: "default" | "fast",
                 space: Space = "code", projectId?: string | null,
                 codexProfileId?: string | null,
                 autoCompact?: {
                   mode: AutoCompactMode;
                   thresholdTokens?: number | null;
                 },
                 claudeProfileId?: string | null): boolean {
    const targetEngine = engine ?? "claude";
    if (targetEngine === "claude" && autoCompact?.mode === "custom"
        && !validAutoCompactThreshold(autoCompact.thresholdTokens)) {
      return false;
    }
    const requestId = initial?.msg_id ?? uuid();
    this.newSessionFocusRequestId = requestId;
    this.newSessionEngine = targetEngine;
    this.newSessionSpace = space;
    const ownership = this.ownershipSnapshot(
      this.newSessionEngine,
      this.newSessionSpace,
      this.newSessionEngine === "codex" ? codexProfileId : null,
      this.newSessionEngine === "claude" ? claudeProfileId : null,
    );
    this.pendingOwnershipByRequest[requestId] = ownership;
    const obj: Record<string, unknown> = {
      v: PROTOCOL_VERSION, type: "new_session", request_id: requestId, ts: nowTs(),
    };
    if (cwd) obj.cwd = cwd;
    if (engine && engine !== "claude") obj.engine = engine;
    if (engine === "codex" && codexProfileId) {
      obj.codex_profile_id = codexProfileId;
    }
    if (targetEngine === "claude" && claudeProfileId) {
      obj.claude_profile_id = claudeProfileId;
    }
    if (space !== "code") obj.space = space;
    if (space === "work" && projectId) obj.project_id = projectId;
    if (model) obj.model = model;
    if (effort) obj.effort = effort;
    if (targetEngine === "claude" && autoCompact) {
      obj.auto_compact_mode = autoCompact.mode;
      if (autoCompact.mode === "custom") {
        obj.auto_compact_threshold_tokens = autoCompact.thresholdTokens;
      }
    }
    if (engine === "codex" && collaborationMode) {
      obj.collaboration_mode = collaborationMode;
    }
    if (engine === "codex" && permissionMode) {
      obj.permission_mode = permissionMode;
    }
    if (engine === "codex" && permissionProfile) {
      obj.permission_profile = permissionProfile;
    }
    if (engine === "codex" && webSearch) {
      obj.web_search = webSearch;
    }
    if (engine === "codex" && serviceTier) {
      obj.service_tier = serviceTier;
    }
    if (initial) {
      obj.prompt = initial.prompt;
      obj.msg_id = initial.msg_id;
      if (initial.images?.length) obj.images = initial.images;
      if (initial.files?.length) obj.files = initial.files;
    }
    const queued = this.send(obj);
    if (!queued) {
      this.newSessionFocusRequestId = null;
      delete this.pendingOwnershipByRequest[requestId];
    }
    return queued;
  }

  sendRenameSession(sessionId: string, title: string,
                    engine?: "claude" | "codex", space: Space = "code"): void {
    const targetEngine = engine
      ?? this.engineBySession[sessionId] ?? this.activeEngine;
    const obj: Record<string, unknown> = { v: PROTOCOL_VERSION, type: "rename_session", session_id: sessionId, title, ts: nowTs() };
    if (engine && engine !== "claude") obj.engine = engine;
    if (space !== "code") obj.space = space;
    this.sendListRefreshingCommand(obj, targetEngine, space);
  }

  sendArchiveSession(sessionId: string, archived: boolean,
                     engine?: "claude" | "codex", space: Space = "code"): void {
    const targetEngine = engine
      ?? this.engineBySession[sessionId] ?? this.activeEngine;
    const obj: Record<string, unknown> = { v: PROTOCOL_VERSION, type: "archive_session", session_id: sessionId, archived, ts: nowTs() };
    if (engine && engine !== "claude") obj.engine = engine;
    if (space !== "code") obj.space = space;
    this.sendListRefreshingCommand(obj, targetEngine, space);
  }

  sendPinSession(sessionId: string, pinned: boolean,
                 engine?: "claude" | "codex", space: Space = "code"): void {
    const targetEngine = engine
      ?? this.engineBySession[sessionId] ?? this.activeEngine;
    const obj: Record<string, unknown> = {
      v: PROTOCOL_VERSION, type: "pin_session", session_id: sessionId,
      pinned, ts: nowTs(),
    };
    if (engine && engine !== "claude") obj.engine = engine;
    if (space !== "code") obj.space = space;
    this.sendListRefreshingCommand(obj, targetEngine, space);
  }

  sendDeleteWorkSession(sessionId: string, engine: "claude" | "codex"): boolean {
    return this.sendListRefreshingCommand({
      v: PROTOCOL_VERSION, type: "delete_work_session", session_id: sessionId,
      engine, space: "work", ts: nowTs(),
    }, engine, "work") !== null;
  }

  sendDeleteSession(sessionId: string, engine: "claude" | "codex",
                    space: Space = "code"): boolean {
    return this.sendListRefreshingCommand({
      v: PROTOCOL_VERSION, type: "delete_session", session_id: sessionId,
      engine, space, ts: nowTs(),
    }, engine, space) !== null;
  }

  sendRollbackSession(sessionId: string, engine: "claude" | "codex",
                      restore: "conversation" | "files" | "both",
                      numTurns = 1, checkpointId?: string): boolean {
    const command: Record<string, unknown> = {
      v: PROTOCOL_VERSION, type: "rollback_session", session_id: sessionId,
      engine, space: "code", restore, num_turns: numTurns, ts: nowTs(),
    };
    if (engine === "claude" && checkpointId) command.checkpoint_id = checkpointId;
    return this.send(command);
  }

  sendCompactSession(sessionId: string, engine: "claude" | "codex"): boolean {
    return this.send({
      v: PROTOCOL_VERSION, type: "compact_session", session_id: sessionId,
      engine, space: "code", ts: nowTs(),
    });
  }

  sendStartReview(sessionId: string,
                  target: "uncommittedChanges" | "baseBranch" | "commit" | "custom",
                  value?: string): boolean {
    const command: Record<string, unknown> = {
      v: PROTOCOL_VERSION, type: "start_review", session_id: sessionId,
      engine: "codex", space: "code", target, ts: nowTs(),
    };
    if (value) command.value = value;
    return this.send(command);
  }

  sendGetWorkDashboard(engine: "claude" | "codex"): void {
    this.send({
      v: PROTOCOL_VERSION, type: "get_work_dashboard", engine, ts: nowTs(),
    });
  }

  sendGetWorkArtifacts(engine: "claude" | "codex", sessionId: string): void {
    this.send({
      v: PROTOCOL_VERSION, type: "get_work_artifacts", engine,
      session_id: sessionId, client_id: this.clientId, ts: nowTs(),
    });
  }

  sendCreateWorkProject(engine: "claude" | "codex", name: string,
                        description: string): boolean {
    return this.send({ v: PROTOCOL_VERSION, type: "create_work_project", engine,
      name, description, ts: nowTs() });
  }

  sendDeleteWorkProject(engine: "claude" | "codex", projectId: string): boolean {
    return this.send({ v: PROTOCOL_VERSION, type: "delete_work_project", engine,
      project_id: projectId, ts: nowTs() });
  }

  sendAddWorkSource(engine: "claude" | "codex", projectId: string,
                    kind: "file" | "link" | "note", title: string,
                    uri?: string, file?: QueryFile): boolean {
    const command: Record<string, unknown> = {
      v: PROTOCOL_VERSION, type: "add_work_source", engine,
      project_id: projectId, kind, title, ts: nowTs(),
    };
    if (uri) command.uri = uri;
    if (file) command.file = file;
    return this.send(command);
  }

  sendDeleteWorkSource(engine: "claude" | "codex", sourceId: string): boolean {
    return this.send({ v: PROTOCOL_VERSION, type: "delete_work_source", engine,
      source_id: sourceId, ts: nowTs() });
  }

  sendCreateWorkPlugin(engine: "claude" | "codex", name: string,
                       instructions: string, projectId?: string): boolean {
    const command: Record<string, unknown> = {
      v: PROTOCOL_VERSION, type: "create_work_plugin", engine,
      name, instructions, ts: nowTs(),
    };
    if (projectId) command.project_id = projectId;
    return this.send(command);
  }

  sendDeleteWorkPlugin(engine: "claude" | "codex", pluginId: string): boolean {
    return this.send({ v: PROTOCOL_VERSION, type: "delete_work_plugin", engine,
      plugin_id: pluginId, ts: nowTs() });
  }

  sendCreateWorkSchedule(engine: "claude" | "codex", title: string,
                         prompt: string, nextRunAt: number,
                         repeatSeconds?: number, projectId?: string,
                         codexProfileId?: string,
                         claudeProfileId?: string): boolean {
    const command: Record<string, unknown> = {
      v: PROTOCOL_VERSION, type: "create_work_schedule", engine,
      title, prompt, next_run_at: nextRunAt, ts: nowTs(),
    };
    if (repeatSeconds) command.repeat_seconds = repeatSeconds;
    if (projectId) command.project_id = projectId;
    if (engine === "codex" && codexProfileId) {
      command.codex_profile_id = codexProfileId;
    }
    if (engine === "claude" && claudeProfileId) {
      command.claude_profile_id = claudeProfileId;
    }
    return this.send(command);
  }

  sendDeleteWorkSchedule(engine: "claude" | "codex", scheduleId: string): boolean {
    return this.send({ v: PROTOCOL_VERSION, type: "delete_work_schedule", engine,
      schedule_id: scheduleId, ts: nowTs() });
  }

  sendListDir(path?: string | null): string | null {
    const obj: Record<string, unknown> = { v: PROTOCOL_VERSION, type: "list_dir", ts: nowTs() };
    if (path) obj.path = path;
    return this.sendTracked(obj);
  }

  /** Send hello with the per-session cursor map (multi-session catch-up). */
  sendHello(flush = true): void {
    // Hello must remain the first frame on a fresh relay socket. Once it binds
    // client_id, replay every still-unacknowledged command in insertion order.
    const replay = this.boundedReplayState();
    this.sendUntracked({
      v: PROTOCOL_VERSION, type: "hello", role: "client", client_id: this.clientId,
      machine_id: this.machineId, cursors: replay.cursors, ts: nowTs(),
      generations: replay.generations,
    });
    if (flush) this.flushOutbox();
  }

  private sendRecoveryPreamble(): void {
    // Preserve socket order: Hello/alias recovery, then make each command's
    // target resident immediately before replaying that exact serialized frame.
    // Pre-warming all targets first can exceed the wrapper's resident-session
    // cap and evict early targets before their command arrives.
    this.sendHello(false);
    const plan = planRecoveryReplay(
      this.outbox.pendingFramesWithSessionIds(), this.focusedSid);
    for (const step of plan) {
      if (step.type === "switch") this.sendRecoverySwitch(step.sid);
      else this.sendRaw(step.raw);
    }
  }

  private sendRecoverySwitch(sid: string): void {
    const frame: Record<string, unknown> = {
      v: PROTOCOL_VERSION, type: "switch_session", session_id: sid, ts: nowTs(),
    };
    if (this.engineBySession[sid] === "codex") frame.engine = "codex";
    if (this.spaceBySession[sid] === "work") frame.space = "work";
    this.sendUntracked(frame);
  }

  private send(obj: Record<string, unknown>): boolean {
    return this.sendTracked(obj) !== null;
  }

  private sendTracked(
    obj: Record<string, unknown>, commandId = uuid(),
  ): string | null {
    const result = this.outbox.enqueue(obj, this.clientId, commandId);
    if (!result.ok) {
      console.error("command not queued", result.reason);
      this.cb.onCommandError?.(result.reason.startsWith("command too large")
        ? "内容过大，暂时无法发送，请缩减附件或内容后重试。"
        : "操作暂未发送，请等待连接恢复后重试。");
      return null;
    }
    this.sendRaw(result.raw);
    return commandId;
  }

  private sendUntracked(obj: Record<string, unknown>): void {
    this.sendRaw(JSON.stringify(obj));
  }

  private sendRaw(raw: string): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(raw);
    }
  }

  private flushOutbox(): void {
    for (const raw of this.outbox.pendingFrames()) this.sendRaw(raw);
  }

  private connect(): void {
    this.cb.onConnState("connecting");
    const socketGeneration = ++this.connectionGeneration;
    this.invalidatedListRefreshByScope.clear();
    this.invalidatedListScopeByRequest.clear();
    const ws = new WebSocket(this.url);
    let compatibleFrameSeen = false;
    this.ws = ws;
    ws.onopen = () => {
      if (socketGeneration !== this.connectionGeneration || this.ws !== ws) return;
      this.backoff = 1;
      this.sendRecoveryPreamble();
      this.startHeartbeat();  // detect a half-open link and auto-recover
      this.cb.onConnState("connected");  // triggers sendListSessions
    };
    ws.onmessage = (e) => {
      if (socketGeneration !== this.connectionGeneration || this.ws !== ws) return;
      try {
        const decoded = JSON.parse(e.data) as ServerEvent;
        if (!compatibleFrameSeen && decoded.type === "error"
            && decoded.code === "protocol" && !decoded.sid
            && !decoded.request_id && !decoded.msg_id) {
          // A mismatched relay sends this uncorrelated handshake error just
          // before closing with 4406. The close handler owns rollout recovery;
          // surfacing this frame would leave a stale "refresh" banner behind
          // even after the connection repairs itself.
          return;
        }
        compatibleFrameSeen = true;
        this.lastRecvAt = Date.now();  // any valid JSON frame proves the link is alive
        // A real frame proves that this bundle and the relay agree. Clear the
        // one-shot mismatch guard so a future deployment may refresh once too.
        clearProtocolReloadMarker();
        const msg = this.filterControl(decoded);
        if (!msg) return;
        if ((msg as { type: string }).type === "pong") return;  // heartbeat reply — consume, don't dispatch
        // A BtwSync can be the first proof of a replacement Wrapper when this
        // browser slept through wrapper_reconnected. Invalidate the old
        // generation before installing the new catalog; doing this afterward
        // would immediately clear the ids below and drop their SyncBtw replay.
        if (msg.type === "btw_sync") {
          this.noteWrapperGeneration(msg.generation);
        }
        // The lightweight owner catalog establishes which private routing keys
        // this account may consume. A live BTW frame can race ahead of Hello's
        // catalog response; drop it without advancing its cursor so SyncBtw can
        // recover that exact suffix after the catalog arrives.
        if (msg.type === "btw_sync") {
          this.knownBtwSids.clear();
          for (const session of msg.sessions) {
            this.knownBtwSids.add(session.btw_sid);
          }
        } else if (msg.type === "btw_opened") {
          this.knownBtwSids.add(msg.btw_sid);
        } else if (msg.type === "btw_closed") {
          this.knownBtwSids.delete(msg.btw_sid);
        } else if (msg.sid?.startsWith("btw-")
            && !this.knownBtwSids.has(msg.sid)) {
          return;
        }
        if (msg.type === "session_list_invalidated") {
          this.refreshInvalidatedSessionList(
            msg.engine, msg.space ?? "code", socketGeneration);
          return;
        }
        if ((msg.type === "user_msg" || msg.type === "turn_steered"
              || msg.type === "turn_binding"
              || msg.type === "error")
            && this.queryAcceptance.accept(msg)) {
          // Keep a runtime protected after command ACK until its narrative
          // acceptance/error is visible, then allow normal pruning again.
          this.cb.onOutboxChanged?.(this.pendingSessionIds());
        }
        if (msg.type === "history") {
          let acceptedFromHistory = false;
          for (const historyEvent of msg.events) {
            if (historyEvent.type !== "user_msg"
                && historyEvent.type !== "turn_steered"
                && historyEvent.type !== "turn_binding"
                && historyEvent.type !== "error") continue;
            acceptedFromHistory = this.queryAcceptance.accept({
              ...historyEvent,
              sid: historyEvent.sid ?? msg.session_id,
            }) || acceptedFromHistory;
          }
          const pending = this.queryAcceptance.pendingMessageId(msg.session_id);
          if (pending && msg.turns?.some((turn) =>
            turn.id === pending || turn.clientMsgId === pending)) {
            acceptedFromHistory =
              this.queryAcceptance.completeSession(msg.session_id)
              || acceptedFromHistory;
          }
          acceptedFromHistory =
            this.queryAcceptance.acceptHistory(msg) || acceptedFromHistory;
          const historyHead = queryAcceptanceHistoryHead(msg);
          if (historyHead) {
            const previous = this.historyHeadBySession[msg.session_id];
            const isOlderSameEpoch = !!previous
              && previous.generation === historyHead.generation
              && previous.revision === historyHead.revision
              && historyHead.buildSeq < previous.buildSeq;
            if (!isOlderSameEpoch) {
              this.historyHeadBySession[msg.session_id] = historyHead;
            }
          }
          if (acceptedFromHistory) {
            // A reconnect may recover the accepted query only through a
            // materialized page after its live echo fell outside replay.
            this.cb.onOutboxChanged?.(this.pendingSessionIds());
          }
        }
        if (msg.type === "command_ack") {
          this.completeInvalidatedSessionListRefresh(
            msg.cmd_id, socketGeneration);
          if (this.outbox.ack(msg.client_id, msg.cmd_id)) {
            this.cb.onCommandCompleted?.(msg.cmd_id);
            this.cb.onOutboxChanged?.(this.pendingSessionIds());
          }
          return;
        }
        if (msg.type === "hello" && msg.role === "wrapper" && msg.wrapper_generation) {
          this.noteWrapperGeneration(msg.wrapper_generation);
        }
        if (msg.type === "wrapper_reconnected") {
          if (msg.generation) this.noteWrapperGeneration(msg.generation);
          this.sendRecoveryPreamble();
        }
        let eventOwnership: EventOwnership | undefined;
        if (msg.type === "session_list") {
          const listedSpace = msg.space ?? "code";
          const scopeKey = sessionScopeKey(
            this.machineId, msg.engine, listedSpace);
          const listedRequest = msg.request_id
            ? this.pendingListOwnershipByRequest.get(msg.request_id)
            : undefined;
          const acceptedOrder =
            this.latestAcceptedListOrderByScope.get(scopeKey) ?? 0;
          if (listedRequest
              && listedRequest.ownership.scopeKey === scopeKey
              && listedRequest.order >= acceptedOrder
              && this.acceptsOwnership(
                listedRequest.ownership, socketGeneration)) {
            const listedOwnership = listedRequest.ownership;
            eventOwnership = listedOwnership;
            // Advance only when a response is accepted, not when its command is
            // sent. A newer Error-only mutation then cannot strand an older
            // valid list, while cached + refreshed frames from one request keep
            // sharing the same order and a truly newer accepted list wins.
            this.latestAcceptedListOrderByScope.set(
              scopeKey, listedRequest.order);
            for (const session of msg.sessions) {
              const sessionOwnership: EventOwnership = {
                ...listedOwnership,
                claudeProfileId: msg.engine === "claude"
                  ? session.claude_profile_id
                    ?? msg.default_claude_profile_id
                    ?? null
                  : null,
                codexProfileId: msg.engine === "codex"
                  ? session.codex_profile_id
                    ?? msg.default_codex_profile_id
                    ?? null
                  : null,
              };
              this.engineBySession[session.session_id] = msg.engine;
              this.spaceBySession[session.session_id] = listedSpace;
              if (session.codex_profile_id) {
                this.codexProfileBySession[session.session_id] =
                  session.codex_profile_id;
              }
              if (session.claude_profile_id) {
                this.claudeProfileBySession[session.session_id] =
                  session.claude_profile_id;
              }
              this.ownershipBySession[session.session_id] = sessionOwnership;
            }
          }
        }
        if (msg.type === "error" && msg.request_id) {
          // A failed mutation can deliberately send Error followed by its
          // authoritative SessionList so the sidebar reconciles an uncertain
          // outcome. Keep bounded list ownership for that possible second
          // frame; Error alone never advances the accepted-response order.
          this.pendingGoalOwnershipByRequest.delete(msg.request_id);
        }
        if (msg.type === "session_focus") {
          // Drop a STALE switch-confirmation: when you click through several
          // sessions quickly, the wrapper processes each switch in turn and emits
          // a SessionFocus for every one. Honoring the late ones would "replay"
          // your clicks — yanking the view through each session. Only honor a
          // focus that matches your current intent, a correlated create response,
          // or the very first focus when we have none yet.
          const isCreatedFocus = !!msg.request_id
            && msg.request_id === this.newSessionFocusRequestId;
          const pendingOwnership = isCreatedFocus && msg.request_id
            ? this.pendingOwnershipByRequest[msg.request_id]
            : this.shiftOwnership(
              this.pendingSwitchOwnership, msg.session_id);
          const ownership = pendingOwnership
            ?? this.ownershipBySession[msg.session_id];
          const targetEngine = ownership?.engine
            ?? (isCreatedFocus
              ? this.newSessionEngine : this.engineBySession[msg.session_id]);
          const targetSpace = ownership?.space
            ?? (isCreatedFocus
              ? this.newSessionSpace : this.spaceBySession[msg.session_id]);
          if (targetEngine !== this.activeEngine || targetSpace !== this.activeSpace) {
            return; // delayed/foreign Code↔Work or Claude↔Codex focus
          }
          if (this.focusedSid != null && msg.session_id !== this.focusedSid && !isCreatedFocus) {
            return; // superseded — ignore
          }
          if (isCreatedFocus) {
            this.newSessionFocusRequestId = null;
            if (msg.request_id) delete this.pendingOwnershipByRequest[msg.request_id];
          }
          this.focusedSid = msg.session_id;
          if (isCreatedFocus) this.engineBySession[msg.session_id] = this.newSessionEngine;
          if (isCreatedFocus) this.spaceBySession[msg.session_id] = this.newSessionSpace;
          if (ownership?.codexProfileId) {
            this.codexProfileBySession[msg.session_id] =
              ownership.codexProfileId;
          }
          if (ownership?.claudeProfileId) {
            this.claudeProfileBySession[msg.session_id] =
              ownership.claudeProfileId;
          }
          if (ownership) this.ownershipBySession[msg.session_id] = ownership;
          this.touchReplay(msg.session_id);
          this.cb.onEvent(
            msg,
            this.acceptsOwnership(ownership, socketGeneration)
              ? ownership : undefined,
          );
          return;
        }
        if (msg.type === "session_rekey") {
          // Runtime re-key (tmp -> real id): migrate the cursor and, ONLY if we
          // were viewing old_key, the focus. Never a focus change by itself.
          const { old_key, session_id } = msg;
          const ownership = this.ownershipBySession[old_key];
          if (old_key !== session_id) {
            const oldSeq = this.lastSeqBySession[old_key];
            const realSeq = this.lastSeqBySession[session_id];
            if (oldSeq != null && (realSeq == null || oldSeq >= realSeq)) {
              const oldGeneration = this.generationBySession[old_key];
              // A live temp runtime wins the alias merge together with its
              // revision epoch. Use the normal generation transition so a
              // cached watermark already stored under the real id is cleared.
              if (oldGeneration) this.noteGeneration(session_id, oldGeneration);
              this.lastSeqBySession[session_id] = oldSeq;
            }
            delete this.lastSeqBySession[old_key];
            delete this.generationBySession[old_key];
            const oldControl = this.controlBySession[old_key];
            if (oldControl) this.acceptControl(session_id, oldControl);
            delete this.controlBySession[old_key];
            if (this.rebuildingSessions.delete(old_key)) {
              this.rebuildingSessions.add(session_id);
            }
            if (this.engineBySession[old_key] && !this.engineBySession[session_id]) {
              this.engineBySession[session_id] = this.engineBySession[old_key];
            }
            delete this.engineBySession[old_key];
            if (this.spaceBySession[old_key] && !this.spaceBySession[session_id]) {
              this.spaceBySession[session_id] = this.spaceBySession[old_key];
            }
            delete this.spaceBySession[old_key];
            if (this.claudeProfileBySession[old_key]
                && !this.claudeProfileBySession[session_id]) {
              this.claudeProfileBySession[session_id] =
                this.claudeProfileBySession[old_key];
            }
            delete this.claudeProfileBySession[old_key];
            if (this.codexProfileBySession[old_key]
                && !this.codexProfileBySession[session_id]) {
              this.codexProfileBySession[session_id] =
                this.codexProfileBySession[old_key];
            }
            delete this.codexProfileBySession[old_key];
            if (this.historyHeadBySession[old_key]
                && !this.historyHeadBySession[session_id]) {
              this.historyHeadBySession[session_id] =
                this.historyHeadBySession[old_key];
            }
            delete this.historyHeadBySession[old_key];
            if (ownership) {
              this.ownershipBySession[session_id] = ownership;
              delete this.ownershipBySession[old_key];
            }
            this.outbox.rekeySession(old_key, session_id);
            this.queryAcceptance.rekeySession(old_key, session_id);
            this.replayOrder = this.replayOrder.filter((sid) => sid !== old_key);
            this.touchReplay(session_id);
            if (this.focusedSid === old_key) this.focusedSid = session_id;
          }
          this.cb.onEvent(
            msg,
            this.acceptsOwnership(ownership, socketGeneration)
              ? ownership : undefined,
          );
          return;
        }
        if (msg.type === "snapshot") {
          const sid = msg.sid ?? msg.cc_session_id;
          if (sid && msg.generation) {
            this.noteGeneration(sid, msg.generation);
            this.rebuildingSessions.delete(sid);
          }
          // Snapshots announce background runtimes. Only an explicit switch or
          // correlated create response may move focus across product surfaces.
        }
        if (msg.type === "history_invalidated") {
          delete this.historyHeadBySession[msg.session_id];
        }
        if (msg.type === "session_migrated") {
          const ownership = this.ownershipBySession[msg.session_id];
          if (this.acceptsOwnership(ownership, socketGeneration)) {
            eventOwnership = ownership;
          }
        }
        if (msg.type === "goal_state" && msg.sid) {
          const pendingOwnership = msg.request_id
            ? this.pendingGoalOwnershipByRequest.get(msg.request_id)
            : undefined;
          if (msg.request_id) {
            this.pendingGoalOwnershipByRequest.delete(msg.request_id);
          }
          const ownership = pendingOwnership
            ?? this.ownershipBySession[msg.sid];
          if (this.acceptsOwnership(ownership, socketGeneration)) {
            eventOwnership = ownership;
          }
        }
        if (msg.type === "replay_start" && msg.sid && msg.generation) {
          this.noteGeneration(msg.sid, msg.generation);
        }
        if (msg.type === "replay_start" && msg.sid) {
          if (msg.rebuild) {
            // Rebuild is a new seq epoch even when the wrapper generation stayed
            // constant (for example an evicted session was re-spawned).
            this.lastSeqBySession[msg.sid] = 0;
            this.rebuildingSessions.add(msg.sid);
          } else {
            // Supersede an interrupted rebuild from an earlier socket.
            this.rebuildingSessions.delete(msg.sid);
          }
        }
        const eventSeq = (msg as { seq?: number | null }).seq;
        if (msg.sid && typeof eventSeq === "number"
            && !this.rebuildingSessions.has(msg.sid)
            && eventSeq <= (this.lastSeqBySession[msg.sid] ?? 0)) {
          // A client is registered at the relay before its Hello reaches the
          // wrapper, so a live event can overlap the ensuing replay. Cached
          // command responses can also arrive after a newer event. Never feed
          // either stale frame into the reducer.
          return;
        }
        this.noteSeq(msg.sid, msg.type === "replay_end"
          ? msg.to_seq : eventSeq);
        this.cb.onEvent(msg, eventOwnership);
        if (msg.type === "replay_end" && msg.sid) {
          this.rebuildingSessions.delete(msg.sid);
        }
      } catch (err) {
        console.warn("dropping malformed frame", err);
      }
    };
    ws.onclose = (ev: CloseEvent) => {
      if (socketGeneration !== this.connectionGeneration || this.ws !== ws) return;
      if (this.ws === ws) this.ws = null;
      this.stopHeartbeat();
      if (ev.code === 4406) {
        this.beginProtocolRecovery();
        return;
      }
      if (ev.code === 1008) {
        this.cb.onAuthFail?.();
        return;
      }
      if (!this.stopped) {
        // Browsers commonly hide an HTTP/WebSocket auth rejection behind 1006,
        // especially during the opening handshake. Ask the HTTP session endpoint
        // before retrying so an expired/revoked cookie cannot loop forever.
        this.cb.onConnState("reconnecting");
        void this.reconnectAfterSessionProbe();
      }
    };
    ws.onerror = () => {
      if (socketGeneration !== this.connectionGeneration || this.ws !== ws) return;
      /* onclose will follow */
    };
  }

  private beginProtocolRecovery(): void {
    if (this.protocolRecoveryStartedAt !== 0) return;
    this.protocolRecoveryStartedAt = Date.now();
    const generation = ++this.protocolRecoveryGeneration;
    this.cb.onConnState(
      "reconnecting",
      "页面版本正在更新，等待服务端完成部署…",
    );
    void this.probeProtocolRecovery(generation);
  }

  private async probeProtocolRecovery(generation: number): Promise<void> {
    const [relayProtocol, webProtocol] = await Promise.all([
      fetchProtocol("/healthz"),
      fetchProtocol("/cc-remote-build.json"),
    ]);
    if (this.stopped || generation !== this.protocolRecoveryGeneration
        || this.ws !== null) return;

    const action = protocolRecoveryAction(relayProtocol, webProtocol);
    if (action.kind === "reconnect") {
      clearProtocolReloadMarker();
      this.cancelProtocolRecovery();
      this.connect();
      return;
    }
    if (action.kind === "reload") {
      const reloadMarker = readProtocolReloadMarker();
      if (reloadMarker !== undefined
          && reloadMarker !== String(action.protocol)
          && writeProtocolReloadMarker(action.protocol)) {
        this.cancelProtocolRecovery();
        window.location.reload();
        return;
      }
    }

    if (Date.now() - this.protocolRecoveryStartedAt
        >= PROTOCOL_RECOVERY_TIMEOUT_MS) {
      this.cancelProtocolRecovery();
      this.cb.onConnState(
        "disconnected",
        "页面与服务端版本仍未同步，请稍后刷新重试。",
      );
      return;
    }
    this.protocolRecoveryTimer = setTimeout(() => {
      this.protocolRecoveryTimer = null;
      void this.probeProtocolRecovery(generation);
    }, PROTOCOL_RECOVERY_POLL_MS);
  }

  private cancelProtocolRecovery(): void {
    this.protocolRecoveryGeneration += 1;
    this.protocolRecoveryStartedAt = 0;
    if (this.protocolRecoveryTimer !== null) {
      clearTimeout(this.protocolRecoveryTimer);
      this.protocolRecoveryTimer = null;
    }
  }

  private async reconnectAfterSessionProbe(): Promise<void> {
    const result = await probeSession();
    if (this.stopped || this.ws !== null) return;
    if (!shouldReconnectAfterSessionProbe(result)) {
      this.cb.onAuthFail?.();
      return;
    }
    this.scheduleReconnect();
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer !== null) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, this.backoff * 1000);
    this.backoff = Math.min(this.backoff * 2, 5);  // cap at 5s so reconnect recovers fast
  }
}
