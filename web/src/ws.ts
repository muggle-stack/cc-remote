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
  DiffTheme, GoalStatus, QueryFile, QueryImg, ServerEvent,
} from "./protocol.ts";
import { makeForkSessionCommand, makeForkSessionWorktreeCommand, makeOpenBtwCommand, PROTOCOL_VERSION } from "./protocol.ts";
import { CommandOutbox, planRecoveryReplay } from "./outbox.ts";
import { probeSession, shouldReconnectAfterSessionProbe } from "./session-auth.ts";
import { uuid } from "./util.ts";

export type ConnState = "connecting" | "connected" | "reconnecting" | "disconnected";

export interface WsCallbacks {
  onEvent: (msg: ServerEvent) => void;
  onConnState: (s: ConnState, detail?: string) => void;
  onAuthFail?: () => void;
  onCommandError?: (detail: string) => void;
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

function nowTs(): number {
  return Date.now() / 1000;
}

export class RelayWs {
  private ws: WebSocket | null = null;
  private lastSeqBySession: Record<string, number> = {};
  private generationBySession: Record<string, string> = {};
  private rebuildingSessions = new Set<string>();
  private replayOrder: string[] = [];
  private engineBySession: Record<string, "claude" | "codex"> = {};
  private focusedSid: string | null = null;
  // Correlates a create response without using SessionFocus as a trigger for the
  // first query. A later explicit switch clears it, so late create focus cannot
  // override the user's newer navigation intent.
  private newSessionFocusRequestId: string | null = null;
  private newSessionEngine: "claude" | "codex" = "claude";
  private readonly outbox = new CommandOutbox(
    OUTBOX_MAX_COMMANDS, OUTBOX_MAX_BYTES, OUTBOX_MAX_FRAME_BYTES);
  private readonly clientId: string;
  private readonly url: string;
  private backoff = 1;
  private stopped = false;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private readonly cb: WsCallbacks;
  // Heartbeat: detect a HALF-OPEN link (dead TCP with no close event) and recover.
  private lastRecvAt = 0;
  private hbTimer: ReturnType<typeof setInterval> | null = null;
  private pingSeq = 0;
  private wrapperGeneration: string | null = null;
  private lastGenerationChangeNotice: string | null = null;

  constructor(cb: WsCallbacks) {
    this.cb = cb;
    this.clientId = uuid();
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    this.url = new URL(`${proto}//${window.location.host}/ws`).toString();
  }

  start(): void {
    this.connect();
  }

  stop(): void {
    this.stopped = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.stopHeartbeat();
    this.ws?.close();
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
    return this.outbox.pendingSessionIds();
  }

  private touchReplay(sid: string): void {
    this.replayOrder = this.replayOrder.filter((known) => known !== sid);
    this.replayOrder.push(sid);
    while (this.replayOrder.length > MAX_REPLAY_SESSIONS) {
      const expired = this.replayOrder.shift();
      if (!expired) break;
      delete this.lastSeqBySession[expired];
      delete this.generationBySession[expired];
      delete this.engineBySession[expired];
      this.rebuildingSessions.delete(expired);
    }
  }

  private dropBtwReplayState(): void {
    for (const knownSid of Object.keys(this.generationBySession)) {
      if (!knownSid.startsWith("btw-")) continue;
      delete this.generationBySession[knownSid];
      delete this.lastSeqBySession[knownSid];
      delete this.engineBySession[knownSid];
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
    }
    this.generationBySession[sid] = generation;
    this.touchReplay(sid);
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
  ): void {
    for (const [sid, seq] of Object.entries(cursors)) {
      if (typeof seq === "number" && seq > (this.lastSeqBySession[sid] ?? 0)) {
        this.lastSeqBySession[sid] = seq;
        this.touchReplay(sid);
      }
    }
    for (const [sid, generation] of Object.entries(generations)) {
      if (generation && this.lastSeqBySession[sid] != null) {
        this.generationBySession[sid] = generation;
        this.touchReplay(sid);
      }
    }
  }

  setFocusedSid(sid: string | null, engine?: "claude" | "codex"): void {
    this.focusedSid = sid;
    if (sid) {
      if (engine) this.engineBySession[sid] = engine;
      this.touchReplay(sid);
    }
    this.newSessionFocusRequestId = null;
  }

  setSessionEngines(sessions: Array<{ session_id: string; engine?: string | null }>): void {
    for (const session of sessions) {
      if (session.engine === "codex" || session.engine === "claude") {
        this.engineBySession[session.session_id] = session.engine;
      }
    }
  }

  private sidObj(): Record<string, unknown> {
    return this.focusedSid ? { sid: this.focusedSid } : {};
  }

  sendQuery(prompt: string, msg_id: string, images?: QueryImg[], files?: QueryFile[]): boolean {
    const obj: Record<string, unknown> = { v: PROTOCOL_VERSION, type: "query", prompt, msg_id, ts: nowTs(), ...this.sidObj() };
    if (images && images.length) obj.images = images;
    if (files && files.length) obj.files = files;
    return this.send(obj);
  }

  // ---- /btw ephemeral side-fork ----
  sendOpenBtw(parentSid: string, requestId = uuid()): string | null {
    const queued = this.send({
      ...makeOpenBtwCommand(parentSid, requestId, nowTs()),
    });
    return queued ? requestId : null;
  }
  sendCloseBtw(btwSid: string): void {
    this.send({ v: PROTOCOL_VERSION, type: "close_btw", sid: btwSid, ts: nowTs() });
  }

  sendForkSessionWorktree(parentSessionId: string, name: string,
                          requestId = uuid(), lastTurnId?: string): string | null {
    const queued = this.send({
      ...makeForkSessionWorktreeCommand(
        parentSessionId, name, requestId, nowTs(), lastTurnId),
    });
    return queued ? requestId : null;
  }

  sendForkSession(parentSessionId: string, forkPointId: string,
                  requestId = uuid()): string | null {
    const queued = this.send({
      ...makeForkSessionCommand(parentSessionId, forkPointId, requestId, nowTs()),
    });
    return queued ? requestId : null;
  }

  // a turn targeted at an explicit sid (the btw fork), NOT the focused session.
  sendQueryTo(sid: string, prompt: string, msg_id: string,
              images?: QueryImg[], files?: QueryFile[]): boolean {
    const obj: Record<string, unknown> = {
      v: PROTOCOL_VERSION, type: "query", prompt, msg_id, sid, ts: nowTs(),
    };
    if (images && images.length) obj.images = images;
    if (files && files.length) obj.files = files;
    return this.send(obj);
  }

  sendInterrupt(): void {
    this.send({ v: PROTOCOL_VERSION, type: "interrupt", ts: nowTs(), ...this.sidObj() });
  }

  sendTakeover(sid: string): boolean {
    return this.send({ v: PROTOCOL_VERSION, type: "takeover", sid, ts: nowTs() });
  }

  sendSetModel(model: string): void {
    this.send({ v: PROTOCOL_VERSION, type: "set_model", model, ts: nowTs(), ...this.sidObj() });
  }

  sendSetEffort(effort: string): void {
    this.send({ v: PROTOCOL_VERSION, type: "set_effort", effort, ts: nowTs(), ...this.sidObj() });
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

  sendGetContext(): void {
    this.send({ v: PROTOCOL_VERSION, type: "get_context", ts: nowTs(), ...this.sidObj() });
  }

  sendGetContextTo(sid: string): void {
    this.send({ v: PROTOCOL_VERSION, type: "get_context", sid, ts: nowTs() });
  }

  sendGetDiff(file: string, theme: DiffTheme): void {
    this.send({ v: PROTOCOL_VERSION, type: "get_diff", file, theme, ts: nowTs(), ...this.sidObj() });
  }

  sendGetFilePreview(path: string, requestId = uuid()): string | null {
    const queued = this.send({
      v: PROTOCOL_VERSION, type: "get_file_preview", path, request_id: requestId,
      ts: nowTs(), ...this.sidObj(),
    });
    return queued ? requestId : null;
  }

  sendGetPreviewAsset(path: string, previewId: string,
                      requestId = uuid()): string | null {
    const queued = this.send({
      v: PROTOCOL_VERSION, type: "get_preview_asset", path,
      preview_id: previewId, request_id: requestId,
      ts: nowTs(), ...this.sidObj(),
    });
    return queued ? requestId : null;
  }

  /** Fetch a session's history as ONE bulk frame, read on-demand from its
   *  transcript (like a web chat's GET /conversation). client_id lets the wrapper
   *  route the History reply to=this client. Replaces per-hello buffer replay. */
  sendGetHistory(sessionId: string, before?: string | null, limit?: number | null): void {
    const obj: Record<string, unknown> = {
      v: PROTOCOL_VERSION, type: "get_history", session_id: sessionId,
      client_id: this.clientId, ts: nowTs(),
    };
    if (before) obj.before = before;
    if (limit) obj.limit = limit;
    this.send(obj);
  }

  /** Ask the engine for its catalog and explicit new-session defaults. Claude
   *  needs cwd because project/local settings can change the selected model. */
  sendGetModels(engine: "cc" | "claude" | "codex", cwd?: string | null): void {
    const frame: Record<string, unknown> = {
      v: PROTOCOL_VERSION, type: "get_models", engine,
      client_id: this.clientId, ts: nowTs(),
    };
    if (cwd) frame.cwd = cwd;
    this.send(frame);
  }

  sendAnswerQuestion(askId: string, answer: string): void {
    this.send({ v: PROTOCOL_VERSION, type: "answer_question", ask_id: askId, answer, ts: nowTs(), ...this.sidObj() });
  }

  sendGetGoal(): void {
    this.send({ v: PROTOCOL_VERSION, type: "get_goal", ts: nowTs(), ...this.sidObj() });
  }

  sendGetStatus(): void {
    this.send({ v: PROTOCOL_VERSION, type: "get_status", client_id: this.clientId, ts: nowTs(), ...this.sidObj() });
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

  sendListSessions(engine?: "claude" | "codex"): void {
    const obj: Record<string, unknown> = { v: PROTOCOL_VERSION, type: "list_sessions", ts: nowTs() };
    if (engine && engine !== "claude") obj.engine = engine;
    this.send(obj);
  }

  sendSwitchSession(sessionId: string, engine?: "claude" | "codex"): void {
    if (engine) this.engineBySession[sessionId] = engine;
    const obj: Record<string, unknown> = { v: PROTOCOL_VERSION, type: "switch_session", session_id: sessionId, ts: nowTs() };
    if (engine && engine !== "claude") obj.engine = engine;
    this.send(obj);
  }

  sendNewSession(cwd?: string | null, engine?: "claude" | "codex",
                 model?: string | null, effort?: string | null,
                 initial?: { prompt: string; msg_id: string; images?: QueryImg[]; files?: QueryFile[] },
                 collaborationMode?: "default" | "plan",
                 permissionMode?: "never" | "on-request" | "untrusted",
                 serviceTier?: "default" | "fast"): boolean {
    const requestId = initial?.msg_id ?? uuid();
    this.newSessionFocusRequestId = requestId;
    this.newSessionEngine = engine ?? "claude";
    const obj: Record<string, unknown> = {
      v: PROTOCOL_VERSION, type: "new_session", request_id: requestId, ts: nowTs(),
    };
    if (cwd) obj.cwd = cwd;
    if (engine && engine !== "claude") obj.engine = engine;
    if (model) obj.model = model;
    if (effort) obj.effort = effort;
    if (engine === "codex" && collaborationMode) {
      obj.collaboration_mode = collaborationMode;
    }
    if (engine === "codex" && permissionMode) {
      obj.permission_mode = permissionMode;
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
    if (!queued) this.newSessionFocusRequestId = null;
    return queued;
  }

  sendRenameSession(sessionId: string, title: string): void {
    this.send({ v: PROTOCOL_VERSION, type: "rename_session", session_id: sessionId, title, ts: nowTs() });
  }

  sendArchiveSession(sessionId: string, archived: boolean): void {
    this.send({ v: PROTOCOL_VERSION, type: "archive_session", session_id: sessionId, archived, ts: nowTs() });
  }

  sendListDir(path?: string | null): void {
    const obj: Record<string, unknown> = { v: PROTOCOL_VERSION, type: "list_dir", ts: nowTs() };
    if (path) obj.path = path;
    this.send(obj);
  }

  /** Send hello with the per-session cursor map (multi-session catch-up). */
  sendHello(flush = true): void {
    // Hello must remain the first frame on a fresh relay socket. Once it binds
    // client_id, replay every still-unacknowledged command in insertion order.
    const replay = this.boundedReplayState();
    this.sendUntracked({
      v: PROTOCOL_VERSION, type: "hello", role: "client", client_id: this.clientId,
      cursors: replay.cursors, ts: nowTs(), generations: replay.generations,
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
    this.sendUntracked(frame);
  }

  private send(obj: Record<string, unknown>): boolean {
    const result = this.outbox.enqueue(obj, this.clientId, uuid());
    if (!result.ok) {
      const detail = `命令未发送：${result.reason}。请等待连接恢复后重试。`;
      console.error(detail);
      this.cb.onCommandError?.(detail);
      return false;
    }
    this.sendRaw(result.raw);
    return true;
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
    const ws = new WebSocket(this.url);
    this.ws = ws;
    ws.onopen = () => {
      this.backoff = 1;
      this.sendRecoveryPreamble();
      this.startHeartbeat();  // detect a half-open link and auto-recover
      this.cb.onConnState("connected");  // triggers sendListSessions
    };
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data) as ServerEvent;
        this.lastRecvAt = Date.now();  // any frame proves the link is alive
        // Debug hook: record inbound frames so a live-sync issue is diagnosable
        // from the browser console. Inspect `window.__wsLog`; set
        // `window.__CCDEBUG = true` to also console.debug each frame.
        try {
          const w = window as unknown as { __wsLog?: unknown[]; __CCDEBUG?: boolean };
          (w.__wsLog ||= []).push({ t: (msg as { type: string }).type, sid: (msg as { sid?: string }).sid, seq: (msg as { seq?: number }).seq });
          if (w.__wsLog!.length > 800) w.__wsLog!.shift();
          if (w.__CCDEBUG) console.debug(`[ws] ${(msg as { type: string }).type} sid=${(msg as { sid?: string }).sid ?? "-"} seq=${(msg as { seq?: number }).seq ?? "-"}`);
        } catch { /* ignore */ }
        if ((msg as { type: string }).type === "pong") return;  // heartbeat reply — consume, don't dispatch
        if (msg.type === "command_ack") {
          if (this.outbox.ack(msg.client_id, msg.cmd_id)) {
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
        if (msg.type === "session_focus") {
          // Drop a STALE switch-confirmation: when you click through several
          // sessions quickly, the wrapper processes each switch in turn and emits
          // a SessionFocus for every one. Honoring the late ones would "replay"
          // your clicks — yanking the view through each session. Only honor a
          // focus that matches your current intent, a correlated create response,
          // or the very first focus when we have none yet.
          const isCreatedFocus = !!msg.request_id
            && msg.request_id === this.newSessionFocusRequestId;
          if (this.focusedSid != null && msg.session_id !== this.focusedSid && !isCreatedFocus) {
            return; // superseded — ignore
          }
          if (isCreatedFocus) this.newSessionFocusRequestId = null;
          this.focusedSid = msg.session_id;
          if (isCreatedFocus) this.engineBySession[msg.session_id] = this.newSessionEngine;
          this.touchReplay(msg.session_id);
          this.cb.onEvent(msg);
          return;
        }
        if (msg.type === "session_rekey") {
          // Runtime re-key (tmp -> real id): migrate the cursor and, ONLY if we
          // were viewing old_key, the focus. Never a focus change by itself.
          const { old_key, session_id } = msg;
          if (old_key !== session_id) {
            const oldSeq = this.lastSeqBySession[old_key];
            const realSeq = this.lastSeqBySession[session_id];
            if (oldSeq != null && (realSeq == null || oldSeq >= realSeq)) {
              this.lastSeqBySession[session_id] = oldSeq;
              if (this.generationBySession[old_key]) {
                this.generationBySession[session_id] = this.generationBySession[old_key];
              }
            }
            delete this.lastSeqBySession[old_key];
            delete this.generationBySession[old_key];
            if (this.rebuildingSessions.delete(old_key)) {
              this.rebuildingSessions.add(session_id);
            }
            if (this.engineBySession[old_key] && !this.engineBySession[session_id]) {
              this.engineBySession[session_id] = this.engineBySession[old_key];
            }
            delete this.engineBySession[old_key];
            this.outbox.rekeySession(old_key, session_id);
            this.replayOrder = this.replayOrder.filter((sid) => sid !== old_key);
            this.touchReplay(session_id);
            if (this.focusedSid === old_key) this.focusedSid = session_id;
          }
          this.cb.onEvent(msg);
          return;
        }
        if (msg.type === "snapshot") {
          const sid = msg.sid ?? msg.cc_session_id;
          if (sid && msg.generation) {
            this.noteGeneration(sid, msg.generation);
            this.rebuildingSessions.delete(sid);
          }
          if (this.focusedSid == null && sid) this.focusedSid = sid;
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
        this.cb.onEvent(msg);
        if (msg.type === "replay_end" && msg.sid) {
          this.rebuildingSessions.delete(msg.sid);
        }
      } catch (err) {
        console.warn("dropping malformed frame", err);
      }
    };
    ws.onclose = (ev: CloseEvent) => {
      if (this.ws === ws) this.ws = null;
      this.stopHeartbeat();
      if (ev.code === 4406) {
        window.location.reload();
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
      /* onclose will follow */
    };
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
