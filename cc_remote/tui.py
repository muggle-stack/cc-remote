"""Interactive terminal client for cc-remote — Option B: the terminal as a client.

Runs in your terminal and ATTACHES to a wrapper-owned session over the relay, so
the terminal and the phone/browser are all clients of the SAME session and sync
bidirectionally: send here → the phone sees it; send on the phone → it shows up
here. The agent still runs on the wrapper host (this is a thin view+input, like
the web client), reusing the exact same wire protocol — so the relay/wrapper need
zero changes.

This is NOT the native `claude` TUI; it's a cc-remote client rendered in the
terminal. That's the trade for true two-way sync (see the design discussion).

Env:
  RELAY_URL       default ws://127.0.0.1:8765/ws
  LOGIN_PASSWORD  optional; if omitted the TUI prompts without echo
  PUBLIC_ORIGIN   optional WebSocket Origin override (normally derived from URL)
  ENGINE          claude | codex   (which store to list / which backend for /new)

Usage:
  python -m cc_remote.tui [session_id]     # attach to session_id, or pick from a list

Commands (each on its own line):
  <text>            send as a query to the attached session
  /sessions         list sessions (then /attach <n>)
  /attach <n|id>    attach to a session by list-number or id
  /new [cwd]        start a new session (cwd defaults to ~)
  /stop             interrupt the running turn
  /model [id]       set model (no arg = list options)
  /effort <id>      set reasoning effort (low|medium|high|xhigh|max)
  /context          show context-window usage
  /engine <e>       switch engine (claude|codex) for listing/new
  /help             show commands
  /quit
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import getpass
import ipaddress
import json
import os
import sys
import time
import uuid
from http.cookies import SimpleCookie
from typing import Optional
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

import cc_remote.config  # noqa: F401  (import side-effect: loads .env)
from cc_remote.log import logger, setup
from cc_remote.protocol import (
    Hello, Query, Interrupt, SetModel, SetEffort, GetContext,
    GetHistory, ListSessions, SwitchSession, NewSession, AnswerQuestion,
    Ping, is_reliable_command, serialize,
)
from cc_remote.relay.auth import SESSION_COOKIE_NAME
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

setup("cc_remote.tui", os.environ.get("LOG_LEVEL", "WARNING"))
log = logger("cc_remote.tui")

RELAY_URL = os.environ.get("RELAY_URL", "ws://127.0.0.1:8765/ws")
LOGIN_PASSWORD = os.environ.get("LOGIN_PASSWORD", "")
PUBLIC_ORIGIN = os.environ.get("PUBLIC_ORIGIN", "")
ENGINE = os.environ.get("ENGINE", "claude")
TUI_OUTBOX_CAP = 256
TUI_OUTBOX_BYTES = 32 * 1024 * 1024
TUI_MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_REPLAY_SESSIONS = 128

# Terminal control input is a security boundary: model/tool/transcript text is
# untrusted even though the relay itself is authenticated.  Keep ordinary LF so
# streamed Markdown remains readable, turn tabs into inert spaces, and remove
# every other C0/C1 byte plus Unicode bidi controls that can visually reorder a
# shell prompt or filename.  Trusted ANSI produced by the helpers below is
# applied only *after* remote fields pass through this function.
_BIDI_CONTROLS = frozenset({
    "\u061c", "\u200e", "\u200f",
    *map(chr, range(0x202A, 0x202F)),
    *map(chr, range(0x2066, 0x2070)),
})
_HISTORY_NARRATIVE_TYPES = frozenset({
    "user_msg", "assistant_msg_start", "delta", "tool_use", "tool_result",
    "assistant_msg_end",
})


def _safe_remote_text(value: object) -> str:
    """Return terminal-safe text while preserving ordinary newlines."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    safe: list[str] = []
    for char in text:
        codepoint = ord(char)
        if char == "\n":
            safe.append(char)
        elif char == "\t":
            safe.append("    ")
        elif codepoint < 0x20 or 0x7F <= codepoint <= 0x9F:
            continue
        elif char in _BIDI_CONTROLS:
            continue
        else:
            safe.append(char)
    return "".join(safe)

# ---- ANSI helpers (disabled when stdout isn't a tty) ----
_TTY = sys.stdout.isatty()


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _TTY else s


def DIM(s: str) -> str: return _c("2", s)
def GREEN(s: str) -> str: return _c("32", s)
def CYAN(s: str) -> str: return _c("36", s)
def RED(s: str) -> str: return _c("31", s)
def YELLOW(s: str) -> str: return _c("33", s)
def BOLD(s: str) -> str: return _c("1", s)


def _short(s: str, n: int) -> str:
    s = " ".join(_safe_remote_text(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _http_endpoint(ws_url: str, path: str) -> str:
    parsed = urlsplit(_validate_relay_url(ws_url))
    scheme = "https" if parsed.scheme == "wss" else "http"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def _ws_origin(ws_url: str, configured: str = "") -> str:
    if configured.strip():
        return configured.strip().rstrip("/")
    parsed = urlsplit(_validate_relay_url(ws_url))
    scheme = "https" if parsed.scheme == "wss" else "http"
    return urlunsplit((scheme, parsed.netloc, "", "", ""))


def _validate_relay_url(ws_url: str) -> str:
    """Fail before login if the password would cross an unsafe endpoint."""
    parsed = urlsplit(ws_url)
    if (
        parsed.scheme not in ("ws", "wss")
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/ws"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "RELAY_URL must be ws(s)://host[:port]/ws without credentials, query, or fragment"
        )
    host = parsed.hostname or ""
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower() == "localhost"
    if parsed.scheme != "wss" and not loopback:
        raise ValueError("RELAY_URL must use wss:// outside loopback")
    return ws_url


def _login_cookie(ws_url: str, password: str) -> str:
    """Log in with the stdlib HTTP client and return a Cookie header value."""
    body = json.dumps({"password": password}).encode("utf-8")
    request = Request(
        _http_endpoint(ws_url, "/api/login"),
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "cc-remote-tui"},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        raw_cookie = response.headers.get("Set-Cookie", "")
    cookies = SimpleCookie()
    cookies.load(raw_cookie)
    session = cookies.get(SESSION_COOKIE_NAME)
    if session is None or not session.value:
        raise RuntimeError("relay login response did not include a session cookie")
    return f"{SESSION_COOKIE_NAME}={session.value}"


class Tui:
    def __init__(self, url: str, password: str, origin: str, engine: str,
                 want_sid: Optional[str]):
        self.url = _validate_relay_url(url)
        self.password = password
        self.origin = _ws_origin(url, origin)
        self.cookie = ""
        self.engine = engine
        self.client_id = uuid.uuid4().hex
        self.attached_sid: Optional[str] = want_sid
        self.attached_engine = engine
        self.want_sid = want_sid            # attach target passed on the CLI
        self.cursors: dict[str, int] = {}
        self.generations: dict[str, str] = {}
        self._rebuilding_sessions: set[str] = set()
        self.sessions: list[dict] = []      # last session_list payload
        self.session_engines: dict[str, str] = {}
        self.sent_msg_ids: set[str] = set()  # our own queries — dedup the user_msg echo
        # AskUser is session-scoped.  Keep background prompts so attaching that
        # session later can surface and answer the still-pending question.
        self.pending_asks: dict[str, dict] = {}
        self._pending_new_request: Optional[str] = None
        self._pending_new_engine = engine
        # Transcript history is a baseline, not a reconnect mechanism.  Once a
        # session has loaded it, ordinary socket reconnects recover only the ring
        # delta from Hello(cursors=...).  A rebuild/truncated replay or a wrapper
        # process generation change explicitly invalidates that baseline.
        self._history_loaded: set[str] = set()
        self._history_requested: set[str] = set()
        self._history_refresh_after_replay: set[str] = set()
        self._history_refresh_now: set[str] = set()
        # A rebuild/truncated ring is followed by an authoritative History.  Do
        # not print the overlapping replay narrative as well; control/state
        # events still flow so asks and runtime state remain current.
        self._history_replay_suppressed: set[str] = set()
        self._wrapper_generation: Optional[str] = None
        self.state = "idle"
        self._midline = False               # cursor is mid-line from streaming deltas
        self.ws = None
        self.last_recv = 0.0                 # monotonic time of the last inbound frame (heartbeat)
        self._ping_n = 0
        self._quitting = False
        self._cmd_q: asyncio.Queue = asyncio.Queue()
        self._stdin_task: Optional[asyncio.Task] = None
        # Reliable commands remain here until the wrapper ACKs them.  Reusing the
        # exact serialized frame preserves cmd_id across reconnect retries.
        self._outbox: OrderedDict[str, tuple[str, int]] = OrderedDict()
        self._outbox_bytes = 0
        self._send_lock = asyncio.Lock()

    # ---- output helpers (keep streaming deltas and block prints from colliding) ----

    def _nl(self) -> None:
        if self._midline:
            sys.stdout.write("\n")
            self._midline = False

    def _line(self, s: str) -> None:
        self._nl()
        sys.stdout.write(s + "\n")
        sys.stdout.flush()

    def _write(self, s: str) -> None:
        sys.stdout.write(s)
        sys.stdout.flush()
        self._midline = not s.endswith("\n")

    # ---- lifecycle ----

    async def run(self) -> None:
        if not self.password:
            self.password = await asyncio.to_thread(getpass.getpass, "Access password: ")
        if not self.password:
            print(RED("No password provided — cannot authenticate."))
            return
        try:
            await self._authenticate()
        except Exception as e:  # noqa: BLE001 — concise login failure for an interactive client
            print(RED(f"Login failed: {_safe_remote_text(e)}"))
            return
        self._stdin_task = asyncio.create_task(self._stdin_reader())
        self._line(BOLD("cc-remote tui") + DIM(f"  client={self.client_id[:8]}  engine={self.engine}"))
        self._line(DIM("commands: /sessions /attach <n|id> /new [cwd] /stop /model /effort /context /engine <e> /help /quit"))
        try:
            await self._connection_loop()
        finally:
            if self._stdin_task:
                self._stdin_task.cancel()

    async def _authenticate(self) -> None:
        self.cookie = await asyncio.to_thread(_login_cookie, self.url, self.password)

    async def _stdin_reader(self) -> None:
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        while True:
            line = await reader.readline()
            if not line:
                await self._cmd_q.put("/quit")
                return
            await self._cmd_q.put(line.decode(errors="replace").rstrip("\n"))

    async def _connection_loop(self) -> None:
        backoff = 1.0
        auth_retry_used = False
        while not self._quitting:
            try:
                async with connect(self.url, additional_headers={"Cookie": self.cookie},
                                   origin=self.origin,
                                   max_size=32 * 1024 * 1024, close_timeout=3, open_timeout=30) as ws:
                    self.ws = ws
                    auth_retry_used = False
                    await self._recovery_preamble()
                    self._line(GREEN(f"[connected to {self.url}]"))
                    # ask for the session list; attach flow continues when it arrives
                    await self._send(ListSessions(engine=self.engine))
                    if self.attached_sid:
                        self._line(CYAN(
                            f"[attaching {_safe_remote_text(self.attached_sid)}…]"))
                        await self._request_history(self.attached_sid)
                    backoff = 1.0
                    await self._session(ws)
            except InvalidStatus as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status == 403 and not auth_retry_used:
                    try:
                        await self._authenticate()
                    except Exception as login_error:  # noqa: BLE001
                        self._line(RED(
                            f"[login failed: {_safe_remote_text(login_error)}]"))
                        return
                    auth_retry_used = True
                    continue
                if status == 403:
                    self._line(RED("[authentication or PUBLIC_ORIGIN rejected]"))
                    return
                if not self._quitting:
                    self._line(RED(
                        f"[conn error: {_safe_remote_text(e)}] "
                        f"retry in {backoff:.0f}s"))
            except Exception as e:  # noqa: BLE001 — surface + retry
                if not self._quitting:
                    self._line(RED(
                        f"[conn error: {_safe_remote_text(e)}] "
                        f"retry in {backoff:.0f}s"))
            if not self._quitting:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5.0)

    async def _session(self, ws) -> None:
        self.last_recv = time.monotonic()
        recv = asyncio.create_task(self._receiver(ws))
        cmd = asyncio.create_task(self._cmd_consumer(ws))
        hb = asyncio.create_task(self._heartbeat(ws))
        _, pending = await asyncio.wait({recv, cmd, hb}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()

    async def _heartbeat(self, ws) -> None:
        # Same half-open guard as the web client. If the WS silently dies (dead TCP,
        # no close frame — common on flaky/proxied links), `async for raw in ws`
        # never returns and we'd stop receiving broadcasts without noticing (no
        # error → no reconnect → the terminal misses messages other clients send).
        # Ping every 20s; if NO frame arrives for 45s, close the ws so the
        # connection loop reconnects — Hello's cursor replay backfills anything
        # missed during the gap without printing the transcript baseline again.
        while True:
            await asyncio.sleep(20)
            if time.monotonic() - self.last_recv > 45:
                self._line(DIM("[链路空闲,重连中…]"))
                try:
                    await ws.close()
                except Exception:
                    pass
                return
            try:
                self._ping_n += 1
                await ws.send(serialize(Ping(n=self._ping_n)))
            except Exception:
                return

    async def _receiver(self, ws) -> None:
        try:
            async for raw in ws:
                self.last_recv = time.monotonic()  # any frame proves the link is alive
                try:
                    d = json.loads(raw)
                except Exception:
                    continue
                self._handle(d)
                if d.get("type") == "wrapper_reconnected":
                    # The relay socket stayed up while a fresh wrapper process
                    # arrived. Hello must precede retry, and the focused session
                    # must be resident before any queued Query is replayed.
                    await self._recovery_preamble()
                    await self._send(ListSessions(engine=self.engine))
                await self._flush_history_refreshes()
        except ConnectionClosed:
            pass  # normal on /quit or a dropped link — the loop reconnects if needed

    async def _send_raw(self, raw: str) -> bool:
        ws = self.ws
        if ws is None:
            return False
        try:
            async with self._send_lock:
                if self.ws is not ws:
                    return False
                await ws.send(raw)
            return True
        except Exception as e:  # noqa: BLE001
            self._line(RED(f"[send failed: {_safe_remote_text(e)}]"))
            return False

    async def _flush_outbox(self) -> None:
        for raw, _ in list(self._outbox.values()):
            if not await self._send_raw(raw):
                return

    async def _request_history(self, sid: str, *, force: bool = False) -> bool:
        """Load one transcript baseline without duplicating it on reconnect.

        Add the request marker before awaiting the send: the receiver can process a
        very fast History response while ``ws.send`` is yielding.  The reliable
        outbox then retries the same request if the link dies before its response.
        """
        if sid in self._history_requested:
            return False
        if not force and sid in self._history_loaded:
            return False
        self._history_requested.add(sid)
        if await self._send(GetHistory(
                session_id=sid, client_id=self.client_id)):
            return True
        self._history_requested.discard(sid)
        return False

    async def _flush_history_refreshes(self) -> None:
        """Run refreshes discovered while synchronously reducing inbound frames."""
        pending = list(self._history_refresh_now)
        self._history_refresh_now.clear()
        for sid in pending:
            if sid == self.attached_sid:
                await self._request_history(sid, force=True)

    def _touch_replay(self, sid: str) -> None:
        if sid in self.cursors:
            cursor = self.cursors.pop(sid)
            self.cursors[sid] = cursor
        if sid in self.generations:
            generation = self.generations.pop(sid)
            self.generations[sid] = generation
        known = list(dict.fromkeys([*self.cursors, *self.generations]))
        for expired in known[:-MAX_REPLAY_SESSIONS]:
            self.cursors.pop(expired, None)
            self.generations.pop(expired, None)
            self._rebuilding_sessions.discard(expired)
            self._history_replay_suppressed.discard(expired)

    def _replay_state(self) -> tuple[dict[str, int], dict[str, str]]:
        keys = list(self.cursors)[-MAX_REPLAY_SESSIONS:]
        cursors = {sid: self.cursors[sid] for sid in keys}
        generations = {
            sid: self.generations[sid]
            for sid in keys if self.generations.get(sid)
        }
        return cursors, generations

    async def _recovery_preamble(self) -> None:
        cursors, generations = self._replay_state()
        if not await self._send(Hello(
            role="client", client_id=self.client_id,
            cursors=cursors or None, generations=generations or None)):
            return
        queued: list[tuple[str, str, Optional[str]]] = []
        targets: list[str] = []
        for cmd_id, (raw, _) in self._outbox.items():
            try:
                frame = json.loads(raw)
                sid = frame.get("sid")
                if (not isinstance(sid, str)
                        and frame.get("type") == "switch_session"):
                    sid = frame.get("session_id")
            except (TypeError, ValueError):
                sid = None
            queued.append((cmd_id, raw, sid if isinstance(sid, str) else None))
            if isinstance(sid, str) and sid and not sid.startswith("btw-"):
                targets.append(sid)
        if self.attached_sid and not self.attached_sid.startswith("btw-"):
            targets = [sid for sid in targets if sid != self.attached_sid]
            targets.append(self.attached_sid)
        # Make each target resident immediately before replaying its commands.
        # Sending every Switch first lets a low session cap evict an earlier
        # target before its Query is flushed.  The attached session remains last
        # so this still restores the visible focus after background recovery.
        sent: set[str] = set()
        for sid in dict.fromkeys(targets):
            engine = self.session_engines.get(
                sid, self.attached_engine if sid == self.attached_sid else "claude")
            if not await self._send_raw(serialize(SwitchSession(
                    session_id=sid, engine=engine))):
                return
            for cmd_id, raw, command_sid in queued:
                if command_sid != sid:
                    continue
                if not await self._send_raw(raw):
                    return
                sent.add(cmd_id)
        # Sessionless controls and ephemeral /btw commands do not have a durable
        # session to prewarm. Preserve their original outbox order after the
        # recoverable session groups.
        for cmd_id, raw, _ in queued:
            if cmd_id in sent:
                continue
            if not await self._send_raw(raw):
                return

    def _rekey_outbox(self, old_key: str, session_id: str) -> None:
        for cmd_id, (raw, old_size) in list(self._outbox.items()):
            try:
                frame = json.loads(raw)
            except (TypeError, ValueError):
                continue
            changed = False
            if frame.get("sid") == old_key:
                frame["sid"] = session_id
                changed = True
            if frame.get("session_id") == old_key:
                frame["session_id"] = session_id
                changed = True
            if not changed:
                continue
            updated = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
            size = len(updated.encode("utf-8"))
            if (size > TUI_MAX_FRAME_BYTES
                    or self._outbox_bytes - old_size + size > TUI_OUTBOX_BYTES):
                continue  # wrapper's persistent alias resolver remains the fallback
            self._outbox[cmd_id] = (updated, size)
            self._outbox_bytes += size - old_size

    async def _send(self, msg) -> bool:
        if not is_reliable_command(msg):
            return await self._send_raw(serialize(msg))

        cmd_id = getattr(msg, "cmd_id", None) or uuid.uuid4().hex
        queued = self._outbox.get(cmd_id)
        if queued is None:
            msg = msg.model_copy(update={"cmd_id": cmd_id, "client_id": self.client_id})
            raw = serialize(msg)
            size = len(raw.encode("utf-8"))
            if size > TUI_MAX_FRAME_BYTES:
                self._line(RED(f"[command too large: {size} bytes]"))
                return False
            if (len(self._outbox) >= TUI_OUTBOX_CAP
                    or self._outbox_bytes + size > TUI_OUTBOX_BYTES):
                self._line(RED("[command queue full — wait for reconnect/ACK, then retry]"))
                return False
            self._outbox[cmd_id] = (raw, size)
            self._outbox_bytes += size
        else:
            raw, _ = queued

        # Once durably accepted into the bounded in-memory outbox, a transient
        # send failure is retried on reconnect/wrapper_reconnected.
        await self._send_raw(raw)
        return True

    # ---- commands ----

    async def _attach(self, sid: str, engine: Optional[str] = None) -> None:
        self._pending_new_request = None
        previous = self.attached_sid
        previous_engine = self.attached_engine
        self.attached_sid = sid
        self.attached_engine = engine or self.engine
        self.session_engines[sid] = self.attached_engine
        self.sent_msg_ids.clear()
        # switch_session makes it resident (spawns/resumes) so queries land; then
        # pull its history from the transcript like the web client does.
        if not await self._send(SwitchSession(
                session_id=sid, engine=self.attached_engine)):
            self.attached_sid = previous
            self.attached_engine = previous_engine
            return
        self._line(CYAN(f"[attaching {_safe_remote_text(sid)}…]"))
        await self._request_history(sid, force=True)
        self._render_pending_ask(sid)

    async def _cmd_consumer(self, ws) -> None:
        while True:
            line = await self._cmd_q.get()
            s = line.strip()
            if not s:
                continue
            # answering an ask_user prompt: a bare number picks an option
            if self._pending_ask_for_attached() and s.isdigit():
                await self._answer_ask(int(s))
                continue
            if s.startswith("/"):
                await self._command(s)
            else:
                await self._query(s)
            if self._quitting:
                return

    async def _query(self, text: str) -> None:
        if not self.attached_sid:
            self._line(YELLOW("[no session attached — /sessions then /attach <n>, or /new]"))
            return
        mid = uuid.uuid4().hex
        if await self._send(Query(prompt=text, msg_id=mid, sid=self.attached_sid)):
            self.sent_msg_ids.add(mid)
            self._line(GREEN("» ") + text)

    async def _command(self, s: str) -> None:
        parts = s.split(maxsplit=1)
        cmd = parts[0][1:].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("quit", "q", "exit"):
            self._quitting = True
            try:
                if self.ws:
                    await self.ws.close()
            except Exception:
                pass
        elif cmd in ("help", "h"):
            self._line(DIM("/sessions /attach <n|id> /new [cwd] /stop /model [id] /effort <id> /context /engine <e> /quit"))
        elif cmd == "sessions":
            if await self._send(ListSessions(engine=self.engine)):
                self._line(DIM("[refreshing sessions…]"))
        elif cmd == "attach":
            await self._do_attach_arg(arg)
        elif cmd == "new":
            cwd = arg or "~"
            request_id = uuid.uuid4().hex
            self._pending_new_request = request_id  # response can race send()
            self._pending_new_engine = self.engine
            if await self._send(NewSession(
                    cwd=cwd, engine=self.engine, request_id=request_id)):
                self._line(CYAN(f"[new {self.engine} session in {cwd}]"))
            else:
                self._pending_new_request = None
        elif cmd == "stop":
            if self.attached_sid:
                if await self._send(Interrupt(sid=self.attached_sid)):
                    self._line(DIM("[interrupt queued]"))
        elif cmd == "model":
            if arg:
                if await self._send(SetModel(model=arg, sid=self.attached_sid)):
                    self._line(CYAN(f"[model → {arg}]"))
            else:
                self._line(DIM("usage: /model <id>   (e.g. claude-opus-4-8 / gpt-5.5)"))
        elif cmd == "effort":
            if arg:
                if await self._send(SetEffort(effort=arg, sid=self.attached_sid)):
                    self._line(CYAN(f"[effort → {arg}]"))
            else:
                self._line(DIM("usage: /effort <low|medium|high|xhigh|max>"))
        elif cmd == "context":
            if self.attached_sid:
                await self._send(GetContext(sid=self.attached_sid))
        elif cmd == "engine":
            if arg in ("claude", "codex"):
                self.engine = arg
                await self._send(ListSessions(engine=self.engine))
                self._line(CYAN(f"[engine → {arg}] (applies to /sessions and /new)"))
            else:
                self._line(DIM("usage: /engine <claude|codex>"))
        else:
            self._line(YELLOW(f"[unknown command: /{cmd}] — /help"))

    async def _do_attach_arg(self, arg: str) -> None:
        if not arg:
            self._line(DIM("usage: /attach <list-number|session-id>"))
            return
        sid = arg
        target_engine = self.engine
        if arg.isdigit():
            i = int(arg) - 1
            if 0 <= i < len(self.sessions):
                sid = self.sessions[i]["session_id"]
                candidate = self.sessions[i].get("engine")
                if candidate in ("claude", "codex"):
                    target_engine = candidate
            else:
                self._line(YELLOW(f"[no session #{arg}] — /sessions"))
                return
        await self._attach(sid, target_engine)

    async def _answer_ask(self, n: int) -> None:
        ask = self._pending_ask_for_attached()
        if not ask:
            return
        opts = ask.get("options") or []
        if not (1 <= n <= len(opts)):
            self._line(YELLOW(f"[pick 1..{len(opts)}]"))
            return
        answer = opts[n - 1].get("label", str(n))
        if await self._send(AnswerQuestion(
                ask_id=ask["ask_id"], answer=answer, sid=ask["sid"])):
            self._line(CYAN(f"[answered: {_safe_remote_text(answer)}]"))
            self.pending_asks.pop(ask["sid"], None)

    # ---- inbound event rendering ----

    def _for_me(self, d: dict) -> bool:
        """Narrative events are session-scoped; only render the attached one (so a
        background session another device is driving doesn't bleed into our view).
        Before attaching, render nothing narrative — the user picks a session first."""
        return self.attached_sid is not None and d.get("sid") == self.attached_sid

    def _handle(self, d: dict) -> None:
        t = d.get("type")
        if t == "command_ack":
            if d.get("client_id") == self.client_id:
                item = self._outbox.pop(str(d.get("cmd_id", "")), None)
                if item is not None:
                    self._outbox_bytes -= item[1]
            return
        # track per-session cursor for reconnect
        sid, event_seq = d.get("sid"), d.get("seq")
        generation = d.get("generation")
        generation_changed = False
        if isinstance(generation, str):
            generation_changed = (
                self._wrapper_generation is not None
                and self._wrapper_generation != generation
            )
            self._wrapper_generation = generation
        if isinstance(sid, str) and isinstance(generation, str):
            previous_generation = self.generations.get(sid)
            if (previous_generation is not None
                    and previous_generation != generation):
                # seq is monotonic only within one wrapper generation.  A new
                # generation starts a fresh cursor domain even if this envelope
                # is a Snapshot rather than ReplayStart.
                self.cursors[sid] = 0
            self.generations[sid] = generation
            self._touch_replay(sid)
        refreshing_replay = (
            isinstance(sid, str) and t == "replay_start"
            and bool(d.get("rebuild") or d.get("truncated")
                     or generation_changed
                     or sid in self._history_refresh_after_replay)
        )
        if refreshing_replay:
            # Wait until ReplayEnd before replacing the transcript baseline so all
            # replay frames are consumed first and only one refresh is requested.
            # Narrative events overlap that History baseline, so advance their
            # cursor without printing them twice.
            self._history_refresh_after_replay.add(sid)
            self._history_replay_suppressed.add(sid)
        elif t == "replay_start" and isinstance(sid, str):
            self._history_replay_suppressed.discard(sid)
        if t == "replay_start" and d.get("rebuild") and isinstance(sid, str):
            self.cursors[sid] = 0
            self._rebuilding_sessions.add(sid)
            self._touch_replay(sid)
        elif t == "replay_start" and isinstance(sid, str):
            # A new non-rebuild envelope supersedes an interrupted rebuild from
            # an earlier socket.
            self._rebuilding_sessions.discard(sid)
        if t == "replay_end" and isinstance(sid, str) and isinstance(d.get("to_seq"), int):
            replay_to_seq = d["to_seq"]
            if d.get("truncated") or sid in self._history_refresh_after_replay:
                self._history_refresh_after_replay.discard(sid)
                self._history_refresh_now.add(sid)
        elif (generation_changed and self.attached_sid
              and t != "replay_start"):
            # Snapshot or wrapper_reconnected can be the first proof of a fresh
            # process when the target was not resident during Hello replay.
            self._history_refresh_now.add(self.attached_sid)
        inbound_msg_id = d.get("msg_id")
        own_user_echo = (
            t == "user_msg" and isinstance(inbound_msg_id, str)
            and inbound_msg_id in self.sent_msg_ids
        )
        if own_user_echo:
            # Discard before cursor-based stale filtering: a reconnect overlap may
            # prove delivery with an event whose seq was already advanced.
            self.sent_msg_ids.discard(inbound_msg_id)
        if (isinstance(sid, str) and isinstance(event_seq, int)
                and sid not in self._rebuilding_sessions
                and event_seq <= self.cursors.get(sid, 0)):
            # The relay can register this client before forwarding Hello, so a
            # live event may arrive once directly and once in replay.  Reliable
            # command response caching can likewise resend an older seq.  Both
            # must be dropped before rendering or mutating visible state.
            return
        if isinstance(sid, str) and isinstance(event_seq, int):
            self.cursors[sid] = max(self.cursors.get(sid, 0), event_seq)
            self._touch_replay(sid)
        if (isinstance(sid, str) and sid in self._history_replay_suppressed
                and t in _HISTORY_NARRATIVE_TYPES):
            return
        if t == "replay_end" and isinstance(sid, str):
            if isinstance(d.get("to_seq"), int):
                self.cursors[sid] = max(
                    self.cursors.get(sid, 0), replay_to_seq)
                self._touch_replay(sid)
            self._rebuilding_sessions.discard(sid)
            self._history_replay_suppressed.discard(sid)

        if t == "session_list":
            self._render_sessions(d.get("sessions") or [])
        elif t == "history":
            history_sid = d.get("session_id")
            if isinstance(history_sid, str):
                self._history_requested.discard(history_sid)
                self._history_loaded.add(history_sid)
            self._render_history(d)
        elif t == "session_focus":
            # only auto-attach to a session WE just created via /new; ignore other
            # focus frames (a switch confirmation, or a focus another device caused).
            nsid = d.get("session_id")
            if (nsid and d.get("request_id")
                    and d.get("request_id") == self._pending_new_request):
                self._pending_new_request = None
                self.attached_sid = nsid
                self.attached_engine = self._pending_new_engine
                self.session_engines[nsid] = self.attached_engine
                # A newly-created session starts with an empty transcript baseline;
                # its live turn is recovered from the ring on ordinary reconnect.
                self._history_loaded.add(nsid)
                self.sent_msg_ids.clear()
                self._line(CYAN(
                    f"[new session {_safe_remote_text(nsid)} — start typing]"))
                self._render_pending_ask(nsid)
        elif t == "session_rekey":
            if d.get("old_key") == self.attached_sid:
                self.attached_sid = d.get("session_id")
            old, new = d.get("old_key"), d.get("session_id")
            if isinstance(old, str) and isinstance(new, str):
                old_cursor = self.cursors.get(old)
                new_cursor = self.cursors.get(new)
                if old_cursor is not None and (new_cursor is None or old_cursor >= new_cursor):
                    self.cursors[new] = old_cursor
                    if old in self.generations:
                        self.generations[new] = self.generations[old]
                self.cursors.pop(old, None)
                self.generations.pop(old, None)
                if old in self._rebuilding_sessions:
                    self._rebuilding_sessions.discard(old)
                    self._rebuilding_sessions.add(new)
                if old in self.pending_asks and new not in self.pending_asks:
                    ask = self.pending_asks[old]
                    self.pending_asks[new] = {**ask, "sid": new}
                self.pending_asks.pop(old, None)
                self._rekey_outbox(old, new)
                for bucket in (
                    self._history_loaded,
                    self._history_requested,
                    self._history_refresh_after_replay,
                    self._history_refresh_now,
                    self._history_replay_suppressed,
                ):
                    if old in bucket:
                        bucket.discard(old)
                        bucket.add(new)
                if old in self.session_engines and new not in self.session_engines:
                    self.session_engines[new] = self.session_engines[old]
                self.session_engines.pop(old, None)
                self._touch_replay(new)
        elif t == "user_msg":
            if self._for_me(d):
                if own_user_echo:
                    return  # our own — already echoed locally
                self._line(GREEN("» ") + _safe_remote_text(d.get("prompt", "")))
        elif t == "assistant_msg_start":
            if self._for_me(d):
                self._nl()
                self._write(DIM("🤖 "))
        elif t == "delta":
            if self._for_me(d):
                self._write(_safe_remote_text(d.get("text", "")))
        elif t == "assistant_msg_end":
            if self._for_me(d):
                self._nl()
        elif t == "tool_use":
            if self._for_me(d):
                inp = _short(json.dumps(d.get("input", {}), ensure_ascii=False), 120)
                self._line(DIM(
                    f"  ⚙ {_safe_remote_text(d.get('tool'))} {inp}"))
        elif t == "tool_result":
            if self._for_me(d):
                tag = RED("err") if d.get("is_error") else DIM("ok")
                trunc = DIM(" (truncated)") if d.get("truncated") else ""
                self._line(DIM("  ↳ ") + tag + trunc + " " + DIM(_short(d.get("content", ""), 160)))
        elif t == "turn_end":
            if isinstance(sid, str):
                self.pending_asks.pop(sid, None)
            if self._for_me(d):
                r = d.get("result") or {}
                bad = r.get("is_error")
                msg = (
                    f"— turn {_safe_remote_text(r.get('subtype', '?'))} "
                    f"{_safe_remote_text(r.get('duration_ms', '?'))}ms —"
                )
                self._line(RED(msg) if bad else DIM(msg))
        elif t == "state":
            new = d.get("state", "idle")
            if new == "idle" and isinstance(sid, str):
                self.pending_asks.pop(sid, None)
            if self._for_me(d):
                if d.get("detail"):
                    self._line(YELLOW(
                        f"[{_safe_remote_text(d.get('detail'))}]"))
                if new != self.state:
                    self.state = new
                    if new in ("running", "idle"):
                        self._line(DIM(f"[{_safe_remote_text(new)}]"))
        elif t == "model":
            if self._for_me(d):
                self._line(CYAN(
                    f"[model: {_safe_remote_text(d.get('model'))}]"))
        elif t == "effort":
            if self._for_me(d):
                self._line(CYAN(
                    f"[effort: {_safe_remote_text(d.get('effort'))}]"))
        elif t == "perm":
            if self._for_me(d):
                self._line(CYAN(
                    f"[perm: {_safe_remote_text(d.get('mode'))}]"))
        elif t == "fast":
            if self._for_me(d):
                self._line(CYAN("[fast: " + ("on" if d.get("on") else "off") + "]"))
        elif t == "error":
            if (d.get("code") != "wrapper_offline" and d.get("request_id")
                    and d.get("request_id") == self._pending_new_request):
                self._pending_new_request = None
            if d.get("msg_id") and isinstance(sid, str):
                self.pending_asks.pop(sid, None)
            self._line(RED(
                f"!! {_safe_remote_text(d.get('code'))}: "
                f"{_safe_remote_text(d.get('message'))}"))
        elif t == "ask_user":
            if isinstance(sid, str):
                ask = {
                    "sid": sid,
                    "ask_id": d.get("ask_id"),
                    "question": d.get("question", ""),
                    "options": d.get("options") or [],
                }
                previous = self.pending_asks.get(sid)
                self.pending_asks[sid] = ask
                if self._for_me(d) and previous != ask:
                    self._render_ask(ask)
        elif t == "context_report":
            self._render_context(d)
        elif t == "wrapper_disconnected":
            self._line(YELLOW("[wrapper offline — waiting…]"))
        elif t == "wrapper_reconnected":
            self._line(GREEN("[wrapper back]"))
        # snapshot / dir_list / diff_report / replay_* / pong: quietly ignored

    def _render_sessions(self, sessions: list[dict]) -> None:
        # hide archived; keep order (wrapper sorts by recency)
        self.sessions = [s for s in sessions if s.get("tag") != "archived"]
        for session in self.sessions:
            sid, engine = session.get("session_id"), session.get("engine")
            if isinstance(sid, str) and engine in ("claude", "codex"):
                self.session_engines[sid] = engine
        if not self.sessions:
            self._line(DIM("[no sessions] — /new to start one"))
            return
        self._line(BOLD(f"sessions ({self.engine}):"))
        for i, s in enumerate(self.sessions[:20], 1):
            sid = _safe_remote_text(s.get("session_id"))[:8]
            mark = "●" if s.get("state") in ("running", "idle") else "·"
            here = GREEN(" ←attached") if s.get("session_id") == self.attached_sid else ""
            label = s.get("summary") or s.get("first_prompt") or "(empty)"
            self._line(f"  {i:>2} {mark} {DIM(sid)} {_short(label, 60)}{here}")
        self._line(DIM("→ /attach <n>"))

    def _render_history(self, d: dict) -> None:
        if d.get("session_id") != self.attached_sid:
            return
        events = d.get("events") or []
        if not events:
            self._line(DIM("── (no history) ──"))
            return
        self._line(DIM(f"── history ({len(events)} events) ──"))
        for ev in events:
            # render through the same path; suppress own-echo dedup for history
            self._handle_history_event(ev)
        self._nl()
        self._line(DIM("── live ──"))

    def _handle_history_event(self, ev: dict) -> None:
        t = ev.get("type")
        if t == "user_msg":
            self._line(GREEN("» ") + _safe_remote_text(ev.get("prompt", "")))
        elif t == "assistant_msg_start":
            self._nl(); self._write(DIM("🤖 "))
        elif t == "delta":
            self._write(_safe_remote_text(ev.get("text", "")))
        elif t == "assistant_msg_end":
            self._nl()
        elif t == "tool_use":
            inp = _short(json.dumps(ev.get("input", {}), ensure_ascii=False), 120)
            self._line(DIM(
                f"  ⚙ {_safe_remote_text(ev.get('tool'))} {inp}"))
        elif t == "tool_result":
            tag = RED("err") if ev.get("is_error") else DIM("ok")
            self._line(DIM("  ↳ ") + tag + " " + DIM(_short(ev.get("content", ""), 160)))
        # turn_end/state/model in history: skip (noise)

    def _pending_ask_for_attached(self) -> Optional[dict]:
        if self.attached_sid is None:
            return None
        return self.pending_asks.get(self.attached_sid)

    def _render_pending_ask(self, sid: str) -> None:
        ask = self.pending_asks.get(sid)
        if ask is not None:
            self._render_ask(ask)

    def _render_ask(self, ask: dict) -> None:
        self._line(YELLOW("? ") + BOLD(
            _safe_remote_text(ask.get("question", ""))))
        for i, o in enumerate(ask.get("options") or [], 1):
            ds = o.get("ds")
            label = _safe_remote_text(o.get("label", ""))
            description = _safe_remote_text(ds)
            self._line(
                f"  {i}. {label}"
                + (DIM(f"  — {description}") if description else ""))
        self._line(DIM("→ type the number to answer"))

    def _render_context(self, d: dict) -> None:
        tot, mx = d.get("total_tokens", 0), d.get("max_tokens", 0)
        pct = d.get("percentage", 0)
        self._line(CYAN(
            f"[context: {tot:,}/{mx:,} tokens ({pct:.0f}%)  "
            f"{_safe_remote_text(d.get('model', ''))}]"))


def main() -> None:
    want_sid = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        tui = Tui(
            RELAY_URL,
            LOGIN_PASSWORD,
            PUBLIC_ORIGIN,
            ENGINE if ENGINE in ("claude", "codex") else "claude",
            want_sid,
        )
        asyncio.run(tui.run())
    except ValueError as exc:
        print(RED(f"Invalid configuration: {exc}"))
    except KeyboardInterrupt:
        print("\n" + DIM("[bye]"))


if __name__ == "__main__":
    main()
