"""Vim-style terminal workspace on the existing cc-remote control link."""

from __future__ import annotations

import asyncio
import json
import time
import re
import uuid
from contextlib import contextmanager
from urllib.parse import urlsplit, urlunsplit

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import BindingsMap
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.geometry import Offset
from textual.widgets import Static, TextArea
from textual.widgets.text_area import Selection

from cc_remote.protocol import (
    AnswerQuestion,
    GetHistory,
    GetTurnDetail,
    ListSessions,
    PROTOCOL_VERSION,
    GetGoal,
    GetContext,
    GetStatus,
    GetModels,
    GetEngineCapabilities,
    GetPermissionProfiles,
    ListDir,
    NewSession,
    AcknowledgeCompletion,
    Steer,
    GetQueuedQuery,
    SyncBtw,
    Query,
    SwitchSession,
    DeleteSession,
    DeleteWorkSession,
    Interrupt,
)
from cc_remote.tui import Tui, _safe_remote_text
from cc_remote.tui_state import WorkspaceState, Block, clip
from cc_remote.tui_questions import pending_async
from cc_remote.tui_widgets import Composer, VimArea
from cc_remote.tui_buffers import SessionBuffers, BufferPicker, tab_line
from cc_remote.tui_preview import references, route_preview, detect_graphics
from cc_remote.tui_preview_views import FileHints, FilePreviewScreen
from cc_remote.tui_inline_images import TranscriptViewport
from cc_remote.tui_tree import SessionExplorer, SessionTree, TreeSearch
from cc_remote.tui_panels import (
    ActionPicker,
    DetailPanel,
    QuestionDialog,
    AsyncQuestionDialog,
    QueuePanel,
    SuggestionPicker,
)
from cc_remote.tui_actions import ACTIONS
from cc_remote.tui_attachments import read_attachment
from cc_remote.tui_clipboard import read_clipboard_image
from cc_remote.tui_chrome import (
    settings_text,
    status_text as styled_status,
)
from cc_remote.attachments import validate_attachments
from cc_remote.tui_presentation import bounded
from cc_remote.tui_keys import KeyConfig, LAYERS
from cc_remote.tui_navigation import SCOPES, scoped_catalog, select_session
from cc_remote.tui_deletion import SessionDeletion


class WorkspaceClient(Tui):
    """Reuse the legacy client's transport/replay; replace only its projection."""

    def __init__(self, *args, space: str = "code", **kwargs):
        super().__init__(*args, **kwargs)
        self.space = space
        self.explicit_engine = False
        self.explicit_space = False
        self.keys = KeyConfig()
        self.last_focus: dict[tuple[str, str], str] = {}
        self.catalog_ready: set[tuple[str, str]] = set()
        self.session_catalog_requests: dict[tuple[str, str], str] = {}
        self.catalog_retries: set[tuple[str, str]] = set()
        self.restore_pending = self.attached_sid is None
        self.direct_session_pending: str | None = None
        self.navigation_revision = 0
        self.pending_new_space = space
        self.workspace = WorkspaceState()
        self.notice = "Connecting…"
        self.catalog_dirty: set[tuple[str, str]] = set()
        self.protocol_error = False
        self.demo = False
        self.panel_reads: set[str] = set()
        self.read_tickets: dict[tuple[str, str], tuple[str, int]] = {}
        self.goal_versions: dict[str, int] = {}
        self.goal_read_requests: dict[str, str] = {}
        self.queue_reads: dict[str, tuple[str, str]] = {}
        self.queue_details: dict[str, dict] = {}
        self.queue_updates: dict[str, tuple[str, str]] = {}
        self.queue_update_results: dict[str, dict] = {}
        self.capability_cache: dict[tuple, dict] = {}
        self.capability_requests: set[tuple] = set()
        self.catalog_reads: dict[str, dict] = {}
        self.settings_catalogs: dict[tuple, dict] = {}
        self.action_results: dict[str, dict] = {}
        self.directory_cache: dict[str, tuple[float, dict]] = {}
        self.directory_waiters: dict[str, asyncio.Future] = {}
        self.preview_waiters: dict[
            str, tuple[str, asyncio.Future, str | None]
        ] = {}
        self.buffers = SessionBuffers()
        self.cached_history: set[str] = set()
        self.tab_store = None
        self.saved_workspace = False
        self.tab_save_signature = None
        self.deletions = SessionDeletion(self)

    def restore_tabs(self, store) -> None:
        self.tab_store = store
        try:
            saved = store.load()
        except (OSError, ValueError):
            self.notice = "Cannot read saved tabs; existing file left untouched"
            self.tab_store = None
            return
        if saved is None:
            return
        self.saved_workspace = True
        self.buffers.ids = saved["tabs"][:]
        if not self.want_sid:
            if not self.explicit_engine:
                self.engine = saved["scope"][0]
            if not self.explicit_space:
                self.space = saved["scope"][1]
            self.attached_engine = self.engine
            if saved["active"] and self.scope == tuple(saved["scope"]):
                self.last_focus[self.scope] = saved["active"]

    def save_tabs(self) -> None:
        if not self.tab_store or self.restore_pending:
            return
        signature = (tuple(self.buffers.ids), self.attached_sid, self.scope)
        if signature == self.tab_save_signature:
            return
        try:
            self.tab_store.save(self.buffers.ids, self.attached_sid, self.scope)
            self.tab_save_signature = signature
        except (OSError, ValueError):
            self.notice = "Cannot save TUI tabs; check local state permissions"
            self.tab_store = None

    async def _send(self, message) -> bool:
        if self.demo:
            self.notice = "Offline demo: no command was sent"
            return False
        if isinstance(message, ListSessions):
            message = message.model_copy(
                update={"cmd_id": message.cmd_id or uuid.uuid4().hex}
            )
            scope = (message.engine, message.space)
            previous_request = self.session_catalog_requests.get(scope)
            # Install before transport can yield to an immediate reply, then
            # roll back if reliable-outbox admission rejects the command.
            self.session_catalog_requests[scope] = message.cmd_id
        updates = {
            name: self.workspace.rekeys[value]
            for name in ("sid", "session_id")
            if (value := getattr(message, name, None)) in self.workspace.rekeys
        }
        if updates:
            message = message.model_copy(update=updates)
        if isinstance(
            message, (GetModels, GetEngineCapabilities, GetPermissionProfiles)
        ):
            message = message.model_copy(
                update={"cmd_id": message.cmd_id or uuid.uuid4().hex}
            )
            self.catalog_reads[message.cmd_id] = message.model_dump()
            self.catalog_reads = dict(list(self.catalog_reads.items())[-64:])
        if (
            isinstance(message, (GetGoal, GetStatus, GetContext))
            and message.sid
        ):
            message = message.model_copy(
                update={"cmd_id": message.cmd_id or uuid.uuid4().hex}
            )
            response = {
                GetGoal: "goal_state",
                GetStatus: "status_report",
                GetContext: "context_report",
            }[type(message)]
            revision = (
                self.goal_versions.get(message.sid, 0)
                if isinstance(message, GetGoal)
                else self.workspace.view(
                    message.sid
                ).presentation.live_rate_revision
            )
            previous_ticket = self.read_tickets.get((message.sid, response))
            self.read_tickets[message.sid, response] = (
                message.cmd_id,
                revision,
            )
            if isinstance(message, GetGoal):
                self.goal_read_requests[message.cmd_id] = message.sid
                self.goal_read_requests = dict(
                    list(self.goal_read_requests.items())[-128:]
                )
        if isinstance(message, GetQueuedQuery):
            self.queue_reads[message.cmd_id] = (message.sid, message.msg_id)
        accepted = await super()._send(message)
        if isinstance(message, GetGoal) and message.sid and not accepted:
            key = (message.sid, "goal_state")
            if self.read_tickets.get(key, (None,))[0] == message.cmd_id:
                if previous_ticket is None:
                    self.read_tickets.pop(key, None)
                else:
                    self.read_tickets[key] = previous_ticket
            self.goal_read_requests.pop(message.cmd_id, None)
        if isinstance(message, ListSessions):
            if self.session_catalog_requests.get(scope) == message.cmd_id:
                if accepted:
                    self.catalog_retries.discard(scope)
                else:
                    if previous_request is None:
                        self.session_catalog_requests.pop(scope, None)
                    else:
                        self.session_catalog_requests[scope] = previous_request
                    self.catalog_retries.add(scope)
        return accepted

    def _line(self, text: str) -> None:
        # Trusted legacy color sequences are removed along with remote controls.
        self.notice = _safe_remote_text(Text.from_ansi(text).plain)

    def _write(self, text: str) -> None:
        pass

    def _nl(self) -> None:
        pass

    def _handle(self, event: dict) -> None:
        if event.get("to") and event["to"] != self.client_id:
            return
        if event.get("v", PROTOCOL_VERSION) != PROTOCOL_VERSION:
            self.protocol_error = True
            self._quitting = True
            self.notice = (
                f"Protocol mismatch: client v{PROTOCOL_VERSION}, "
                f"server v{event.get('v')}. Use a matching client checkout."
            )
            return
        self.deletions.observe(event)
        if (event.get("type") == "command_ack"
                and event.get("client_id") == self.client_id):
            pending = self._outbox.get(str(event.get("cmd_id", "")))
            if pending:
                command = json.loads(pending[0])
                if command["type"] in {
                    "rename_session", "delete_session", "delete_work_session",
                    "archive_session", "pin_session",
                }:
                    row = self.workspace.catalog.get(
                        command.get("session_id"), {}
                    )
                    engine = command.get("engine") or row.get("engine")
                    # Mutation replies use the mutation's request ID, not the
                    # latest ListSessions ID. Keep that stale-response fence
                    # and request a fresh catalog after handler completion.
                    self.catalog_dirty.update(
                        {(engine, command.get("space", "code"))}
                        if engine else SCOPES
                    )
        generation = event.get("generation")
        if (
            generation
            and self._wrapper_generation
            and generation != self._wrapper_generation
        ):
            self.cached_history.clear()
        if event.get("type") == "session_list":
            scope = (event.get("engine", "claude"), event.get("space", "code"))
            expected = self.session_catalog_requests.get(scope)
            if (scope in self.catalog_dirty
                    or expected and event.get("request_id") != expected):
                return  # Old/replayed/other-client catalogs cannot restore focus.
        new_focus = (
            event.get("type") == "session_focus"
            and event.get("request_id")
            and event.get("request_id") == self._pending_new_request
        )
        notice = self.notice
        ticket = event.get("request_id") or event.get("cmd_id")
        pending_action = self.action_results.get(ticket)
        if pending_action is not None and (
            event.get("type") == "error"
            and event.get("sid") == self.workspace.rekeys.get(
                pending_action.get("sid"), pending_action.get("sid")
            )
        ):
            pending_action["error"] = _safe_remote_text(
                event.get("message", "Action rejected")
            )
        super()._handle(event)
        if event.get("type") in {
            "tool_use", "tool_result", "tool_delta", "process", "state",
            "assistant_msg_start", "assistant_msg_end", "delta", "turn_end",
        }:
            # Legacy line-oriented output belongs in the transcript, not the
            # persistent two-line workspace status area.
            self.notice = notice
        if (event.get("type") == "error" and event.get("sid")
                and not self._for_me(event)):
            # Keep the error in its session projection, not focused chrome.
            self.notice = notice
        if new_focus:
            self.engine, self.space = (
                self.attached_engine,
                self.pending_new_space,
            )
            self.restore_pending = False
            self.navigation_revision += 1
        if event.get("type") == "session_rekey":
            old, new = event.get("old_key"), event.get("session_id")
            if old and new:
                self.last_focus = {
                    scope: new if sid == old else sid
                    for scope, sid in self.last_focus.items()
                }
        self.remember_focus()
        if (
            event.get("type") == "ask_user"
            and event.get("sid") in self.pending_asks
        ):
            self.pending_asks[event["sid"]].update(
                {key: event.get(key, False) for key in ("secret", "allow_text")}
            )

    def _on_event(self, event: dict) -> None:
        if route_preview(self, event):
            return
        kind = event.get("type")
        sid = event.get("sid")
        request = event.get("request_id")
        update = self.queue_updates.get(request)
        if update and kind in {"queued_query_updated", "error"}:
            expected_sid = self.workspace.rekeys.get(update[0], update[0])
            if sid == expected_sid and (
                kind == "error" or event.get("msg_id") == update[1]
            ):
                self.queue_updates.pop(request, None)
                self.queue_update_results[request] = dict(event)
        if kind in {"dir_list", "error"}:
            waiter = self.directory_waiters.get(event.get("request_id"))
            if waiter and not waiter.done():
                waiter.set_result(event)
        read = self.catalog_reads.pop(event.get("request_id"), None)
        if read and kind == "permission_profiles":
            self.settings_catalogs[(kind, *self.capability_key(read))] = (
                bounded(event)
            )
        if (
            read
            and kind in {"engine_capabilities", "permission_profiles"}
            and read.get("sid")
        ):
            sid = self.workspace.rekeys.get(read["sid"], read["sid"])
            event = {**event, "sid": sid}
        if (
            read
            and kind == "error"
            and read["type"] == "get_engine_capabilities"
        ):
            self.capability_requests.discard(self.capability_key(read))
        if kind == "models":
            # Models has no request_id/sid on the wire. Its native engine,
            # profile and (for Claude) cwd identify the pending read surfaces.
            for request_id, source in list(self.catalog_reads.items()):
                if source["type"] != "get_models" or source.get(
                    "engine"
                ) != event.get("engine"):
                    continue
                profile = (
                    "codex_profile_id"
                    if event["engine"] == "codex"
                    else "claude_profile_id"
                )
                expected_profile = source.get(profile)
                if expected_profile is None:
                    expected_profile = self.workspace.reports.get(
                        event["engine"] + "_profiles", {}
                    ).get("default_" + profile)
                if expected_profile != event.get(profile):
                    continue
                if event["engine"] == "claude" and source.get(
                    "cwd"
                ) != event.get("cwd"):
                    continue
                if source.get("sid"):
                    self.workspace.view(source["sid"]).presentation.reports[
                        "models"
                    ] = bounded(event)
                self.settings_catalogs[(kind, *self.capability_key(source))] = (
                    bounded(event)
                )
                self.catalog_reads.pop(request_id)
        self.settings_catalogs = dict(
            list(self.settings_catalogs.items())[-64:]
        )
        if sid in self._history_replay_suppressed and kind in {
            "process",
            "turn_plan",
            "turn_diff",
        }:
            # Rebuild history is the baseline for these too. Re-appending a
            # replayed process delta to a retained tool duplicates its output.
            return
        if kind == "engine_capabilities":
            key = self.capability_key(event)
            # Completion needs names, not thousands of potentially large
            # descriptions. Detailed capability output lives in Reports.
            self.capability_cache.pop(key, None)
            self.capability_cache[key] = {
                "items": [
                    {
                        name: item[name]
                        for name in ("kind", "name", "enabled")
                        if name in item
                    }
                    for item in event.get("items", [])
                ]
            }
            while len(self.capability_cache) > 32:
                self.capability_cache.pop(next(iter(self.capability_cache)))
            self.capability_requests.discard(key)
            if read and read["type"] == "get_engine_capabilities":
                requested_key = self.capability_key(read)
                self.capability_cache[requested_key] = self.capability_cache[
                    key
                ]
                self.capability_requests.discard(requested_key)
                while len(self.capability_cache) > 32:
                    self.capability_cache.pop(next(iter(self.capability_cache)))
        if kind == "queued_query_detail":
            request_id = event.get("request_id")
            expected = self.queue_reads.pop(request_id, None)
            if expected and (
                self.workspace.rekeys.get(expected[0], expected[0]),
                expected[1],
            ) == (sid, event.get("msg_id")):
                self.queue_details[request_id] = dict(event)
                self.queue_details = dict(list(self.queue_details.items())[-4:])
            return  # Private one-shot payload; never retain it in Reports.
        ticket = self.read_tickets.get((sid, kind))
        if kind == "goal_state":
            if request in self.goal_read_requests:
                source_sid = self.goal_read_requests[request]
                if (
                    self.workspace.rekeys.get(source_sid, source_sid) != sid
                    or not ticket or ticket[0] != request
                ):
                    return
                self.read_tickets.pop((sid, kind), None)
                if ticket[1] != self.goal_versions.get(sid, 0):
                    return
            else:
                # Mutation confirmations and broadcasts are authoritative;
                # only known GetGoal replies belong to the read fence.
                self.goal_versions[sid] = self.goal_versions.get(sid, 0) + 1
        if kind == "error" and request in self.goal_read_requests:
            key = (sid, "goal_state")
            if self.read_tickets.get(key, (None,))[0] == request:
                self.read_tickets.pop(key, None)
        if event.get("request_id") and kind in {
            "context_report",
            "status_report",
        }:
            if ticket and event["request_id"] != ticket[0]:
                return
            if ticket and kind == "status_report":
                event = {
                    **event,
                    "_rates_current": ticket[1]
                    == self.workspace.view(sid).presentation.live_rate_revision,
                }
        self.workspace.event(event)
        if kind == "history":
            history_sid = event.get("session_id")
            view = self.workspace.views.get(history_sid)
            if (
                history_sid
                and view
                and not event.get("before")
                and not event.get("error")
                and event.get("authoritative", True)
                and view.pending_revision is None
                and view.revision == event.get("revision", "")
                and view.generation == event.get("generation")
                and view.build_seq == event.get("build_seq", 0)
            ):
                self.cached_history.add(history_sid)
        elif kind == "history_invalidated":
            self.cached_history.discard(event.get("session_id"))
        elif kind in {"replay_start", "replay_end"} and (
            event.get("rebuild") or event.get("truncated")
        ):
            self.cached_history.discard(sid)
        if kind == "session_list":
            scope = (event.get("engine", "claude"), event.get("space", "code"))
            self.catalog_ready.add(scope)
            for row in event.get("sessions", []):
                if row.get("state") is None:
                    # Evicted/native-only sessions have no continuous Wrapper
                    # stream. Revalidate once when reopened, keeping the local
                    # projection visible while the history request is in flight.
                    self.cached_history.discard(row.get("session_id"))
        if kind == "session_rekey":
            old, new = event["old_key"], event["session_id"]
            self.buffers.rekey(old, new)
            if old in self.cached_history:
                self.cached_history.discard(old)
                self.cached_history.add(new)
            self.read_tickets = {
                (new if key == old else key, kind): value
                for (key, kind), value in self.read_tickets.items()
            }
            if old in self.goal_versions:
                self.goal_versions[new] = self.goal_versions.pop(old)
        if kind in {
            "session_list_invalidated",
            "session_focus",
            "session_rekey",
            "session_forked",
        }:
            if kind == "session_list_invalidated":
                self.catalog_dirty.add(
                    (event["engine"], event.get("space", "code"))
                )
            else:
                self.catalog_dirty.update(SCOPES)
        if kind in {"snapshot", "session_focus", "session_rekey"}:
            sid = event.get("session_id") or event.get("sid")
            if sid:
                self.panel_reads.add(sid)
        if kind == "history_invalidated" and event.get("session_id"):
            self._history_refresh_now.add(event["session_id"])
        if kind == "error" and event.get("sid"):
            self._history_requested.discard(event["sid"])
            self.workspace.view(event["sid"]).loading = False
        if kind == "session_list" and self.attached_sid:
            self.panel_reads.add(self.attached_sid)
        if kind == "notice" and sid == self.attached_sid:
            self.notice = _safe_remote_text(
                f"{event.get('severity', 'info')}: {event.get('title', '')} · "
                f"{self.keys.label('notices')}: details"
            )

    @property
    def scope(self) -> tuple[str, str]:
        return self.engine, self.space

    def visible_catalog(self) -> dict:
        return scoped_catalog(self.workspace.catalog, *self.scope)

    def remember_focus(self) -> None:
        row = self.workspace.catalog.get(self.attached_sid)
        if row:
            self.last_focus[
                (row.get("engine") or self.engine, row.get("space") or "code")
            ] = self.attached_sid

    async def switch_surface(self, engine: str, space: str) -> None:
        if (engine, space) not in SCOPES:
            raise ValueError("Choose claude/codex and code/work")
        if (engine, space) == self.scope:
            return
        self.direct_session_pending = None
        self.remember_focus()
        self.navigation_revision += 1
        self.engine, self.space = engine, space
        self.attached_sid = None
        self.attached_engine = engine
        self._pending_new_request = None
        self.restore_pending = True
        self.notice = f"Loading {engine.title()} / {space.title()} sessions…"
        if self.demo:
            self.catalog_ready.add(self.scope)
            await self.restore_surface()
        else:
            # A cached row may have been deleted by another client. Wait for
            # this surface's authoritative catalog before making it writable.
            self.catalog_ready.discard(self.scope)
            await self._send(ListSessions(engine=engine, space=space))

    async def restore_surface(self) -> None:
        if self.direct_session_pending:
            sid = self.direct_session_pending
            row = self.workspace.catalog.get(sid)
            if row:
                await self._attach(sid, row["engine"])
            elif SCOPES <= self.catalog_ready:
                self.notice = "Requested session is not in the device catalog"
            return
        if not self.restore_pending or self.scope not in self.catalog_ready:
            return
        self.restore_pending = False
        sid = select_session(
            self.visible_catalog(), self.last_focus.get(self.scope)
        )
        if self.saved_workspace:
            available = self.visible_catalog()
            saved = self.last_focus.get(self.scope)
            sid = (
                saved if saved in available and saved in self.buffers.ids
                else next((item for item in self.buffers.ids
                           if item in available), None)
            )
        if sid:
            await self._attach(sid, self.engine)
        elif self.saved_workspace:
            self.notice = (
                "No saved tabs available here · "
                f"{self.keys.label('tree')}: open a session"
            )
        else:
            command = (
                ":new" if self.space == "work" else ":new /absolute/directory"
            )
            self.notice = (
                f"No {self.engine.title()} / {self.space.title()} sessions · "
                f"{command} creates one"
            )

    def _render_sessions(self, sessions: list[dict]) -> None:
        # Keep the legacy /attach index and engine routing without printing rows.
        self.sessions = [
            {
                **s,
                "engine": s.get("engine")
                or self.workspace.catalog.get(s["session_id"], {}).get("engine")
                or self.engine,
            }
            for s in self.visible_catalog().values()
            if s.get("tag") != "archived"
        ]
        for sid, row in self.workspace.catalog.items():
            self.session_engines[sid] = row.get("engine", self.engine)

    def _session_space(self, sid: str) -> str:
        return self.workspace.catalog.get(sid, {}).get("space", self.space)

    def _history_backed(self, sid: str) -> bool:
        return not sid.startswith("btw-")

    async def sync_side_chat(self, sid: str) -> None:
        await self._send(
            SyncBtw(
                sid=sid,
                cursor=self.cursors.get(sid),
                generation=self.generations.get(sid),
            )
        )

    async def _recovery_preamble(self) -> None:
        # Keep projections/drafts visible immediately, but validate once after
        # reconnect; old background buffers may be outside the replay window.
        self.cached_history.clear()
        self.catalog_ready.clear()
        self.session_catalog_requests.clear()
        self.catalog_retries.clear()
        if (self.attached_sid == self.want_sid
                and self.want_sid
                and self.want_sid not in self.workspace.catalog):
            # A positional ID is not evidence of its engine or workspace.
            self.direct_session_pending = self.want_sid
            self.attached_sid = None
            self.restore_pending = True
        await super()._recovery_preamble()
        if self.attached_sid and not self._history_backed(self.attached_sid):
            await self.sync_side_chat(self.attached_sid)
        # Warm all four catalogs, but only the selected scope can pick focus.
        # These are read-only catalog queries, never engine/model turns.
        for engine, space in sorted(SCOPES):
            if (engine, space) != (self.engine, "code"):
                await self._send(ListSessions(engine=engine, space=space))

    async def _attach(self, sid: str, engine: str | None = None) -> None:
        self.direct_session_pending = None
        previous_scope = self.scope
        self.remember_focus()
        self.navigation_revision += 1
        revision = self.navigation_revision
        self.restore_pending = False
        row = self.workspace.catalog.get(sid, {})
        engine = row.get("engine") or engine or self.engine
        self.engine = engine
        self.space = row.get("space", self.space)
        if self.demo:
            self.attached_sid, self.attached_engine = sid, engine
            self.remember_focus()
            return
        if sid.startswith("btw-"):
            if sid not in self.workspace.catalog:
                self.notice = "This side conversation is no longer available"
                return
            self.attached_sid = sid
            self.attached_engine = engine or self.engine
            self.session_engines[sid] = self.attached_engine
            await self.sync_side_chat(sid)
            self.remember_focus()
            return
        self._pending_new_request = None
        previous, previous_engine = self.attached_sid, self.attached_engine
        self.attached_sid, self.attached_engine = sid, engine
        self.session_engines[sid] = engine
        self.sent_msg_ids.clear()
        accepted = await self._send(
            SwitchSession(
                session_id=sid,
                engine=engine,
                space=self.space,
            )
        )
        if revision != self.navigation_revision:
            return  # A newer UI selection owns focus, even if this send failed.
        if not accepted:
            self.attached_sid, self.attached_engine = previous, previous_engine
            self.engine, self.space = previous_scope
            return
        self.remember_focus()
        await self._request_history(sid, force=sid not in self.cached_history)
        self.panel_reads.add(sid)

    def _render_history(self, event: dict) -> None:
        pass

    async def _request_history(self, sid: str, *, force: bool = False) -> bool:
        if not self._history_backed(sid):
            return False  # SyncBtw's bounded ring is this ephemeral baseline.
        if sid in self._history_requested:
            return False
        if not force and sid in self._history_loaded:
            return False
        self._history_requested.add(sid)
        accepted = await self._send(
            GetHistory(
                session_id=sid,
                client_id=self.client_id,
                detail="summary",
                limit=4,
            )
        )
        if not accepted:
            self._history_requested.discard(sid)
        return accepted

    async def _flush_history_refreshes(self) -> None:
        if self.protocol_error:
            if self.ws:
                await self.ws.close()
            return
        await super()._flush_history_refreshes()
        dirty, self.catalog_dirty = self.catalog_dirty, set()
        retries, self.catalog_retries = self.catalog_retries, set()
        for engine, space in sorted(dirty | retries):
            accepted = await self._send(ListSessions(engine=engine, space=space))
            if not accepted and (engine, space) in dirty:
                # A failed refresh cannot make an invalidated catalog valid.
                self.catalog_dirty.add((engine, space))
        await self.restore_surface()
        pending, self.panel_reads = self.panel_reads, set()
        for sid in pending:
            if sid == self.attached_sid:
                await self.refresh_panel(sid, "Goal / Plan")
                await self.refresh_panel(sid, "Usage / Context", explicit=False)
                await self.prefetch_capabilities(sid)

    @staticmethod
    def capability_key(row: dict) -> tuple:
        return tuple(
            row.get(k)
            for k in (
                "engine",
                "space",
                "cwd",
                "claude_profile_id",
                "codex_profile_id",
            )
        )

    async def prefetch_capabilities(self, sid: str) -> None:
        row = self.workspace.catalog.get(sid, {})
        args = {
            "engine": row.get("engine") or self.engine,
            "space": row.get("space", "code"),
            "cwd": row.get("cwd"),
            "claude_profile_id": row.get("claude_profile_id"),
            "codex_profile_id": row.get("codex_profile_id"),
        }
        key = self.capability_key(args)
        if (
            key not in self.capability_cache
            and key not in self.capability_requests
        ):
            self.capability_requests.add(key)
            if not await self._send(
                GetEngineCapabilities(sid=sid, skills_only=True, **args)
            ):
                self.capability_requests.discard(key)

    def web_url(self) -> str:
        url = urlsplit(self.url)
        return urlunsplit(
            (
                "https" if url.scheme == "wss" else "http",
                url.netloc,
                "/",
                "",
                "",
            )
        )

    async def refresh_panel(
        self, sid: str, name: str, *, explicit: bool = True
    ) -> None:
        row = self.workspace.catalog.get(sid, {})
        messages = {
            "Goal / Plan": lambda: [GetGoal(sid=sid)],
            "Usage / Context": lambda: [GetContext(sid=sid, refresh=explicit)],
            "Status": lambda: [GetStatus(sid=sid)],
            "Settings": lambda: [
                GetModels(
                    sid=sid,
                    engine=self.session_engines.get(sid, self.engine),
                    cwd=row.get("cwd"),
                    claude_profile_id=row.get("claude_profile_id"),
                    codex_profile_id=row.get("codex_profile_id"),
                ),
                GetPermissionProfiles(
                    sid=sid,
                    cwd=row.get("cwd"),
                    codex_profile_id=row.get("codex_profile_id"),
                ),
            ],
            "Reports": lambda: [
                GetEngineCapabilities(
                    sid=sid,
                    engine=self.session_engines.get(sid, self.engine),
                    space=self._session_space(sid),
                    cwd=self.workspace.catalog.get(sid, {}).get("cwd"),
                    claude_profile_id=self.workspace.catalog.get(sid, {}).get(
                        "claude_profile_id"
                    ),
                    codex_profile_id=self.workspace.catalog.get(sid, {}).get(
                        "codex_profile_id"
                    ),
                )
            ],
        }.get(name, lambda: [])()
        if name == "Usage / Context" and (
            explicit or self.session_engines.get(sid, self.engine) == "codex"
        ):
            messages.append(GetStatus(sid=sid))
        for message in messages:
            await self._send(message)

    async def execute_action(self, message) -> bool:
        if message.cmd_id:
            self.action_results[message.cmd_id] = {"sid": message.sid}
            self.action_results = dict(list(self.action_results.items())[-128:])
        if isinstance(message, (DeleteSession, DeleteWorkSession)):
            if self.demo:
                self.notice = "Offline demo: no command was sent"
                return False
            return self.deletions.start(message)
        if isinstance(message, NewSession):
            self._pending_new_request = message.request_id
            self._pending_new_engine = message.engine
            self.pending_new_space = message.space
            self.restore_pending = False
        accepted = await self._send(message)
        if not accepted and isinstance(message, NewSession):
            self._pending_new_request = None
        return accepted

    async def list_directories(self, path: str, *, refresh=False) -> dict:
        cached = self.directory_cache.get(path)
        if cached and not refresh and time.monotonic() - cached[0] < 30:
            return cached[1]
        if self.demo:
            known = [row.get("cwd") for row in self.workspace.catalog.values()]
            base = "/example" if path == "~" else path
            return {
                "path": base,
                "parent": "/" if base != "/" else None,
                "dirs": [
                    {"path": value}
                    for value in dict.fromkeys(known)
                    if value and value.startswith(base.rstrip("/") + "/")
                ],
                "type": "dir_list",
            }
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.directory_waiters[request_id] = future
        try:
            if not await self._send(
                ListDir(path=path, cmd_id=request_id, client_id=self.client_id)
            ):
                raise ValueError(self.notice)
            event = await asyncio.wait_for(future, timeout=15)
            if event.get("type") == "error":
                raise ValueError(event["message"])
            self.directory_cache[path] = (time.monotonic(), event)
            self.directory_cache = dict(
                list(self.directory_cache.items())[-64:]
            )
            return event
        except TimeoutError as exc:
            raise ValueError("Directory listing timed out; use Refresh") from exc
        finally:
            self.directory_waiters.pop(request_id, None)

    async def _command(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        name = parts[0].lstrip("/") if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""
        if name == "engine":
            await self.switch_surface(arg, self.space)
        elif name == "space":
            await self.switch_surface(self.engine, arg)
        elif name == "sessions":
            await self._send(ListSessions(engine=self.engine, space=self.space))
        elif name == "new":
            if self.space == "work" and arg:
                raise ValueError(
                    "Work assigns its own directory; use :new without a path"
                )
            self.restore_pending = False
            await self.execute_action(
                NewSession(
                    cwd=(arg or "~") if self.space == "code" else None,
                    engine=self.engine,
                    space=self.space,
                    request_id=uuid.uuid4().hex,
                )
            )
        else:
            await super()._command(text)

    async def submit(
        self, text: str, *, queue: bool = False, sid: str | None = None,
        async_questions: tuple[str, ...] = (),
    ) -> bool:
        sid = sid or self.attached_sid
        if not sid:
            self.notice = "Select a session first, or :new /absolute/directory"
            return False
        view = self.workspace.view(sid)
        if view.write_state != "writable":
            self.notice = (
                f"Input unavailable: {view.write_state}. Draft retained."
            )
            return False
        if max(len(view.pending_messages), len(view.pending_attachments)) >= 64:
            self.notice = "Too many unconfirmed messages. Draft retained."
            return False
        if queue and (
            len(view.pending_queued_text) + len(view.recovered_queued_text) >= 64
            or sum(len(r["prompt"].encode("utf-8"))
                   for r in view.pending_queued_text.values())
            + sum(len(p.encode("utf-8")) for p in view.recovered_queued_text)
            + len(text.encode("utf-8")) > 8 * 1024 * 1024
        ):
            self.notice = "Too much unconfirmed queue text. Draft retained."
            return False
        args = dict(
            prompt=text,
            msg_id=uuid.uuid4().hex,
            sid=sid,
            cmd_id=uuid.uuid4().hex,
            client_id=self.client_id,
        )
        attachments = [] if async_questions else view.attachments[:]
        images = [a["content"] for a in attachments if a["image"]]
        files = [a["content"] for a in attachments if not a["image"]]
        error = validate_attachments(images, files)
        if error:
            self.notice = error
            return False
        args.update(images=images or None, files=files or None)
        if (
            not queue
            and self.session_engines.get(sid, self.engine) == "codex"
            and view.state == "running"
        ):
            msg = Steer(**args)
        else:
            msg = Query(**args, delivery="queue" if queue else "immediate")
        receipt = None
        if queue:
            receipt = {"cmd_id": msg.cmd_id, "prompt": text, "rejected": False}
            view.pending_queued_text[msg.msg_id] = receipt
        if not queue:
            # Paint immediately, including while the socket write is awaiting
            # transport. Do not invent a native turn or completion state.
            view.pending_messages[msg.msg_id] = Block(
                "user:" + msg.msg_id, "user",
                clip(view.with_attachments(text, args)),
                data={"status": "awaiting confirmation",
                      "async_questions": async_questions},
            )
            view.version += 1
        if attachments:
            view.pending_attachments[msg.msg_id] = (msg.cmd_id, attachments)
            view.attachments.clear()
        if not await self._send(msg):
            view.pending_queued_text.pop(msg.msg_id, None)
            view.restore_attachments(msg.msg_id)
            if not queue:
                view.pending_messages.pop(msg.msg_id, None)
                view.version += 1
            return False
        if receipt and receipt["rejected"]:
            # A rejection can arrive while send yields; do not clear the draft.
            if text in view.recovered_queued_text:
                view.recovered_queued_text.remove(text)
            return False
        # Only the server echo confirms delivery. Queued text stays in queue.
        self.notice = (
            "Queued command submitted" if queue else "Message submitted"
        )
        return True

    async def answer_async(self, sid, identities, text) -> bool:
        if not text.strip() or len(text) > 2 * 1024 * 1024:
            self.notice = "Answer must be non-empty and at most 2 MiB of text."
            return False
        sid = self.workspace.rekeys.get(sid, sid)
        view = self.workspace.view(sid)
        pending = {block.id for block in pending_async(view)}
        if not identities or not set(identities) <= pending:
            self.notice = "This question is no longer pending"
            return False
        if self.session_engines.get(sid, self.engine) != "codex":
            self.notice = "Async answers require a Codex session"
            return False
        if view.state not in {"running", "idle"}:
            self.notice = "Session is synchronizing; retry the retained answer."
            return False
        return await self.submit(
            text, sid=sid, async_questions=tuple(identities)
        )

    async def answer(self, text: str) -> bool:
        ask = self._pending_ask_for_attached()
        if not ask:
            self.notice = "No pending question in this session"
            return False
        return await self.answer_for(ask, text)

    def _pending_ask_for_attached(self):
        if not self.attached_sid:
            return None
        questions = self.workspace.view(
            self.attached_sid
        ).presentation.questions
        return next(iter(questions.values()), None)

    async def answer_for(self, ask: dict, text: str) -> bool:
        sid = self.workspace.rekeys.get(ask["sid"], ask["sid"])
        pending = self.workspace.view(sid).presentation.questions
        if ask["ask_id"] not in pending:
            self.notice = "This question is no longer pending"
            return False
        options = ask.get("options", [])
        picks = [p.strip() for p in text.split(",")]
        if picks and all(p.isdigit() for p in picks) and options:
            indices = [int(p) - 1 for p in picks]
            if (
                any(i < 0 or i >= len(options) for i in indices)
                or len(set(indices)) != len(indices)
                or (len(indices) > 1 and not ask.get("multi_select"))
            ):
                self.notice = "Invalid option selection"
                return False
            labels = [options[i]["label"] for i in indices]
            answer = labels if ask.get("multi_select") else labels[0]
        elif ask.get("allow_text"):
            if not text.strip():
                self.notice = "Enter a non-empty answer; nothing was sent"
                return False
            answer = text
        else:
            self.notice = "Enter option number(s), separated by commas"
            return False
        accepted = await self._send(
            AnswerQuestion(ask_id=ask["ask_id"], answer=answer, sid=ask["sid"])
        )
        if accepted:
            if (self.pending_asks.get(sid) or {}).get("ask_id") == ask["ask_id"]:
                self.pending_asks.pop(sid, None)
            pending.pop(ask["ask_id"], None)
            self.notice = "Answer submitted"
        return accepted


def location(text: str, offset: int) -> tuple[int, int]:
    offset = max(0, min(offset, len(text)))
    return text.count("\n", 0, offset), offset - text.rfind("\n", 0, offset) - 1


def offset(text: str, point: tuple[int, int]) -> int:
    lines = text.splitlines(keepends=True)
    row, col = point
    return min(len(text), sum(map(len, lines[:row])) + col)


class Transcript(VimArea):
    """Read-only document with a logical cursor independent of the composer."""

    line_styles: dict[int, str] = {}
    resize_bookmark = None
    follow_scroll_sid = None
    follow_revision = 0
    follow_layout_pending = False
    _presentation_depth = 0

    @contextmanager
    def presentation_update(self):
        """A reader projection must not invoke an editor's cursor scrolling."""
        self._presentation_depth += 1
        try:
            yield
        finally:
            self._presentation_depth -= 1

    def scroll_cursor_visible(self, center=False, animate=False):
        if self._presentation_depth:
            return Offset(0, 0)
        return super().scroll_cursor_visible(center=center, animate=animate)

    def reconcile_read_cursor(self) -> None:
        """Bring an off-screen cursor to the viewport, never the reverse."""
        top = int(self.scroll_y)
        bottom = top + max(1, self.scrollable_content_region.height) - 1
        cursor = self.wrapped_document.location_to_offset(self.cursor_location)
        if top <= cursor.y <= bottom:
            return
        if self.scroll_y >= self.max_scroll_y:
            point = self.document.end
        else:
            point = self.wrapped_document.offset_to_location(
                Offset(cursor.x, max(top, min(bottom, cursor.y)))
            )
        with self.presentation_update():
            self.move_cursor(point)

    def _size_updated(self, size, virtual_size, container_size, layout=True):
        # Resize events run after the widget geometry changes. Capture before
        # ScrollView clamps offsets or TextArea replaces its old wrapping map.
        if (size != self._size and self._size.width and self._size.height
                and self.is_mounted and self.resize_bookmark is None
                and self.app.shown_sid == self.app.client.attached_sid
                and self.app.shown_sid and self.app.starts):
            self.app.remember()
            view = self.app.client.workspace.view(self.app.shown_sid)
            bottom = (view.follow if self.follow_layout_pending else
                      self.scroll_y >= self.max_scroll_y)
            view.follow = bottom and not view.tail_hidden
            self.resize_bookmark = (
                self.app.shown_sid, view.anchor, view.selection, view.viewport,
                bottom,
                self.selection.end == self.document.end,
                self.selection.start == self.document.end,
            )
        changed = super()._size_updated(
            size, virtual_size, container_size, layout=layout
        )
        if self.resize_bookmark is not None and self.resize_bookmark[4]:
            # The compositor can paint the new height before Resize dispatch.
            # Keep the first such frame pinned, too; width rewrap follows.
            self.scroll_end(animate=False, immediate=True, force=True)
        return changed

    def restore_resize(self, bookmark):
        if bookmark is not self.resize_bookmark:
            return
        if (bookmark[0] != self.app.shown_sid
                or bookmark[0] != self.app.client.attached_sid):
            self.resize_bookmark = None
            return
        view = self.app.client.workspace.view(bookmark[0])
        (_, anchor, selection, viewport, bottom,
         anchor_end, selection_end) = bookmark

        def resolve(point):
            return location(self.text, self.projection.resolve(
                view, point, self.app.starts, len(self.text)
            ))

        with self.presentation_update():
            self.selection = Selection(
                self.document.end if selection_end else resolve(selection),
                self.document.end if anchor_end else resolve(anchor),
            )
        if bottom:
            self.scroll_end(animate=False, immediate=True, force=True)
        else:
            top = self.wrapped_document.location_to_offset(resolve(viewport))
            self.scroll_to(top.x, top.y, animate=False,
                           immediate=True, force=True)
        self.resize_bookmark = None
        if view.follow:
            # Streaming or image reflow can capture a transient non-bottom
            # offset. A following reader must not restore that stale offset.
            self.app.follow_tail(bookmark[0])
        self.app.read_position_signature = None
        self.app.remember()
        self.parent.sync()

    @property
    def projection(self):
        return self.parent.projection

    @property
    def selected_text(self):
        start, end = self.selection
        position = self.document.get_index_from_location
        return self.projection.extract(self.text, position(start), position(end))

    def yank_text(self, start, end):
        position = self.document.get_index_from_location
        return self.projection.extract(
            self.text, position(start), position(end)
        )

    def watch_scroll_y(self, old_value, new_value):
        super().watch_scroll_y(old_value, new_value)
        if isinstance(self.parent, TranscriptViewport):
            self.call_after_refresh(self.parent.sync)
            if (self.follow_scroll_sid
                    and new_value >= self.max_scroll_y - 1):
                self.call_after_refresh(
                    self.app.resume_following_at_bottom,
                    False, self.follow_scroll_sid, self.follow_revision,
                )

    def get_line(self, line_index: int) -> Text:
        line = super().get_line(line_index)
        rich_lines = (self.parent.rich_lines
                      if isinstance(self.parent, TranscriptViewport) else [])
        if (line_index < len(rich_lines)
                and rich_lines[line_index].plain == line.plain):
            line = rich_lines[line_index].copy()
        line.stylize(self.line_styles.get(line_index, ""))
        return line

    def style_messages(self, text: str, starts: list) -> None:
        # Inline styles can change while the rendered characters stay equal.
        self.notify_style_update()
        self.line_styles = {}
        row = 0
        for index, (start, block) in enumerate(starts):
            end = starts[index + 1][0] if index + 1 < len(starts) else len(text)
            length = text[start:end].count("\n")
            muted = block.channel == "thinking" or block.role in {
                "tool",
                "process",
                "detail",
                "tool_group",
            }
            body = "bright_black" if muted else ""
            if block.role == "user":
                body = "on #202a36"
            failed = block.data.get("is_error") or block.data.get("status") in {
                "failed",
                "error",
                "interrupted",
            }
            if failed:
                body = "red"
            for line in range(row, row + length):
                self.line_styles[line] = body
            self.line_styles[row] = (
                "bold cyan on #202a36"
                if block.role == "user"
                else "bold red"
                if failed
                else "bright_black"
                if muted
                else "bold green"
            )
            if block.role == "detail" and block.expanded:
                sections = block.data.get("sections", [])
                projection = self.parent.projection
                source = projection.content.original
                source_start = projection.source(start)
                body_start = source.find("\n", source_start) + 1
                source_rows = [body_start]
                source_end = projection.source(end)
                for line in source[body_start:source_end].splitlines(
                    keepends=True
                ):
                    source_rows.append(source_rows[-1] + len(line))

                def section_row(line):
                    point = source_rows[min(line, len(source_rows) - 1)]
                    return text.count("\n", 0, projection.display(point))

                for n, section in enumerate(sections):
                    begin = section_row(section["line"])
                    end_row = (section_row(sections[n + 1]["line"])
                               if n + 1 < len(sections) else row + length)
                    if section["role"] == "assistant" and (
                        section["channel"] != "thinking"
                    ):
                        style = "red" if section.get("is_error") or (
                            section.get("status") in
                            {"failed", "error", "interrupted"}
                        ) else ""
                        for line in range(begin, min(end_row, row + length)):
                            self.line_styles[line] = style
                        self.line_styles[begin] = (
                            "bold red" if style else "bold green"
                        )
            row += length

    async def _on_key(self, event: events.Key) -> None:
        self.resize_bookmark = None
        self.follow_scroll_sid = None
        self.follow_revision += 1
        if await self.app.read_key(event.key):
            event.stop()
            event.prevent_default()
        else:
            await TextArea._on_key(self, event)
        if event.key in {"j", "ctrl+d", "down", "pagedown", "end"}:
            self.call_after_refresh(
                self.app.resume_following_at_bottom,
                True, self.app.shown_sid, self.follow_revision,
            )

    def on_mouse_down(self) -> None:
        self.resize_bookmark = None
        self.app.pending_jump = None
        self.app.stop_following()

    def on_mouse_scroll_up(self) -> None:
        self.resize_bookmark = None
        self.app.pending_jump = None
        self.app.stop_following()

    def on_mouse_scroll_down(self) -> None:
        self.resize_bookmark = None
        self.follow_revision += 1
        self.follow_scroll_sid = self.app.shown_sid
        self.call_after_refresh(
            self.app.resume_following_at_bottom,
            False, self.app.shown_sid, self.follow_revision,
        )

    def on_mouse_up(self) -> None:
        self.resize_bookmark = None
        self.call_after_refresh(
            self.app.resume_following_at_bottom,
            False, self.app.shown_sid, self.follow_revision,
        )

    def on_resize(self) -> None:
        bookmark = self.resize_bookmark
        if (self.parent.projection.slots
                or (self.parent.projection.content.responsive
                    and self.parent.render_width != max(8, self.wrap_width))):
            self.app.rendered_version = -1
            self.app.paint()
        if bookmark is not None:
            self.call_after_refresh(self.restore_resize, bookmark)
        else:
            self.call_after_refresh(self.app.follow_tail, self.app.shown_sid)

    def on_focus(self) -> None:
        self.app.mode = "NORMAL"


class WorkspaceApp(App, inherit_bindings=False):
    TITLE = "cc-remote"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    #conversation { width: 1fr; height: 1fr; }
    #session-tabs, #session-title {
        height: 1; background: $boost;
        text-wrap: nowrap; text-overflow: ellipsis;
    }
    #transcript { height: 100%; border: none; }
    #question { height: auto; max-height: 8; background: $boost; }
    #progress, #usage, #attachments, #settings, #suggestions {
        height: auto; max-height: 2;
        text-overflow: ellipsis;
    }
    #composer { height: 7; border: solid $primary; }
    #status { height: 2; background: $boost; text-wrap: nowrap; }
    """
    BINDINGS = []

    def __init__(self, client: WorkspaceClient, *, connect: bool = True):
        super().__init__()
        self.client = client
        # Instance-local: a second workspace/config must not inherit bindings.
        self._bindings = BindingsMap(client.keys.bindings())
        self.shortcut_prefix: tuple[str, ...] = ()
        self.explorer_return_draft = False
        self.connect_enabled = connect
        self.mode = "NORMAL"
        self.prefix = ""
        self.shown_sid: str | None = None
        self.rendered_version = -1
        self.rendered_clock = None
        self.starts = []
        self.question_text = None
        self.status_text = None
        self.command_mode = False
        self.command_backup = ""
        self.answer_mode = False
        self.network = None
        self.submitting = False
        self.clipboard_busy = False
        self.acknowledged: set[tuple[str, str]] = set()
        self.chrome_signature: dict[str, object] = {}
        self.read_position_signature = None
        self.pending_jump = None
        self.graphics = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="workspace-body"):
            yield SessionExplorer()
            with Vertical(id="conversation"):
                yield Static("", id="session-tabs", markup=False)
                yield Static("", id="session-title", markup=False)
                yield TranscriptViewport(
                    Transcript(read_only=True, id="transcript")
                )
                yield Static("", id="progress", markup=False)
                yield Static("", id="usage", markup=False)
                yield Static("", id="settings", markup=False)
                yield Static("", id="attachments", markup=False)
                yield Static("", id="suggestions", markup=False)
                yield Static("", id="question", markup=False)
                yield Composer(
                    id="composer",
                    placeholder=(
                        f"{self.client.keys.label('focus_draft')}: draft · "
                        f"i: insert · {self.client.keys.label('focus_read')}: read"
                    ),
                )
        yield Static("", id="status", markup=False)

    def on_screen_resume(self) -> None:
        if len(self.screen_stack) == 1:
            for editor in self.query(Composer):
                editor.set_mode("NORMAL")
            self.mode = "NORMAL"
            self.prefix = ""
            self.shortcut_prefix = ()

    def on_mount(self) -> None:
        # Do not leave hidden Screen Tab/Ctrl+c actions behind the registry.
        self.screen._bindings = BindingsMap([])
        self.query_one(Transcript).focus()
        if self.client.demo:
            self.seed_demo_settings()
        self.set_interval(0.1, self.paint)
        if self.connect_enabled:
            self.network = self.run_worker(self.client._connection_loop())
        self.paint()

    def seed_demo_settings(self) -> None:
        for row in self.client.workspace.catalog.values():
            for kind in ("models", "permission_profiles"):
                args = {"cwd": row.get("cwd")}
                if kind == "models":
                    args["engine"] = row.get("engine")
                    data = {
                        "default_model": "demo-model",
                        "models": [
                            {
                                "id": "demo-model",
                                "display_name": "Demo model (offline)",
                                "efforts": ["low", "high"],
                            },
                        ],
                    }
                else:
                    data = {
                        "profiles": [
                            {"id": ":workspace", "allowed": True},
                            {"id": ":danger-full-access", "allowed": True},
                        ]
                    }
                key = (kind, *self.client.capability_key(args))
                self.client.settings_catalogs[key] = data

    def suggestions(self) -> tuple[int, list[str]]:
        editor = self.query_one(Composer)
        cursor = offset(editor.text, editor.cursor_location)
        match = re.search(r"(?:^|\s)([$/])([\w-]*)$", editor.text[:cursor])
        if not match:
            return cursor, []
        prefix = match[1] + match[2]
        if match[1] == "/":
            names = {
                "goal",
                "plan",
                "context",
                "status",
                "settings",
                "queue",
                "actions",
                "help",
                "web",
                *ACTIONS,
            }
        else:
            row = self.client.workspace.catalog.get(
                self.client.attached_sid, {}
            )
            key = self.client.capability_key(
                {**row, "space": row.get("space", "code")}
            )
            report = self.client.capability_cache.get(key, {})
            names = {
                item["name"]
                for item in report.get("items", [])
                if item.get("kind") == "skill"
                and item.get("enabled") is not False
            }
        return cursor - len(prefix), sorted(
            match[1] + name
            for name in names
            if (match[1] + name).startswith(prefix)
        )

    def completion_hint(self, choices: list[str]) -> str:
        return (
            " · ".join(choices[:4])
            + f"  [{self.client.keys.label('complete')}]"
            if choices
            else ""
        )

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if (
            isinstance(event.text_area, Composer)
            and len(self.screen_stack) == 1
        ):
            _, choices = self.suggestions()
            self.update_chrome(
                "#suggestions",
                self.completion_hint(choices),
            )

    def action_complete(self) -> None:
        self.shortcut_prefix = ()
        if len(self.screen_stack) > 1 or self.focused is not self.query_one(
            Composer
        ):
            return
        editor = self.query_one(Composer)
        start, choices = self.suggestions()
        if not choices:
            self.client.notice = (
                "No cached completions; "
                f"{self.client.keys.label('reports')} refreshes capabilities"
            )
            return
        end = offset(editor.text, editor.cursor_location)
        original = editor.text

        def chosen(value: str | None) -> None:
            if value and editor.text == original:
                editor.replace(
                    value + " ",
                    location(original, start),
                    location(original, end),
                    maintain_selection_offset=False,
                )
                editor.set_mode("INSERT")
                editor.focus()

        self.push_screen(SuggestionPicker(choices), chosen)

    async def on_unmount(self) -> None:
        self.client.deletions.close()
        self.client.save_tabs()
        self.client._quitting = True
        if self.network:
            self.network.cancel()
        if self.client.ws:
            await self.client.ws.close()

    def stop_following(self) -> None:
        reader = self.query_one(Transcript)
        reader.anchor(False)
        reader.follow_scroll_sid = None
        reader.follow_revision += 1
        if self.shown_sid:
            self.client.workspace.view(self.shown_sid).follow = False

    def resume_following_at_bottom(
        self, keyboard=False, sid=None, revision=None
    ) -> None:
        if (not self.shown_sid or len(self.screen_stack) != 1
                or (sid is not None and sid != self.shown_sid)):
            return
        reader = self.query_one(Transcript)
        if revision is not None and revision != reader.follow_revision:
            return
        view = self.client.workspace.view(self.shown_sid)
        view.follow = not view.tail_hidden and reader.scroll_y >= reader.max_scroll_y
        reader.anchor(view.follow)
        if view.follow:
            reader.follow_scroll_sid = None

    def sample_follow_position(self) -> None:
        """Sample the old viewport before content changes its scroll range."""
        if not self.shown_sid or self.shown_sid != self.client.attached_sid:
            return
        reader = self.query_one(Transcript)
        if reader.resize_bookmark is None and not reader.follow_layout_pending:
            self.resume_following_at_bottom()

    def finish_follow_layout(self, sid) -> None:
        sid = self.client.workspace.rekeys.get(sid, sid)
        if sid != self.shown_sid:
            return
        reader = self.query_one(Transcript)
        reader.follow_layout_pending = False
        self.follow_tail(sid)

    def follow_tail(self, sid) -> None:
        # TextArea rewraps after load/resize. Pin only after layout is ready,
        # and never let a stale callback steal another buffer's reading cursor.
        if sid != self.shown_sid or not sid:
            return
        if not self.client.workspace.view(sid).follow:
            return
        reader = self.query_one(Transcript)
        if reader.resize_bookmark is not None:
            return
        reader.anchor()

    def on_resize(self) -> None:
        self.call_after_refresh(self.follow_tail, self.shown_sid)

    def remember(self) -> None:
        if not self.shown_sid:
            return
        self.shown_sid = self.client.workspace.rekeys.get(
            self.shown_sid, self.shown_sid
        )
        view = self.client.workspace.view(self.shown_sid)
        reader = self.query_one(Transcript)
        editor = self.query_one(Composer)
        signature = (
            self.shown_sid,
            self.rendered_version,
            reader.selection,
            reader.scroll_offset,
            reader.size.width,
        )
        if (reader.resize_bookmark is None
                and signature != self.read_position_signature):
            position = reader.document.get_index_from_location
            view.anchor = reader.projection.locate(
                view, position(reader.selection.end), self.starts
            )
            view.selection = reader.projection.locate(
                view, position(reader.selection.start), self.starts
            )
            top = reader.wrapped_document.offset_to_location(
                reader.scroll_offset
            )
            view.viewport = reader.projection.locate(
                view, position(top), self.starts
            )
            selected = next(
                (b for _, b in self.starts if b.id == view.anchor[0]), None
            )
            if selected:
                view.presentation.selected_turn = selected.turn
            self.read_position_signature = signature
        if not self.command_mode and not self.answer_mode:
            view.draft = editor.text
            view.draft_cursor = editor.cursor_location

    async def acknowledge_completion(self, sid, completion_id):
        accepted = False
        try:
            accepted = await self.client._send(
                AcknowledgeCompletion(sid=sid, completion_id=completion_id)
            )
        finally:
            if not accepted:
                self.acknowledged.discard((sid, completion_id))

    def paint(self) -> None:
        if len(self.screen_stack) > 1:
            return
        # A queued timer can run after Screen children begin unmounting.
        try:
            reader = self.query_one(Transcript)
            editor = self.query_one(Composer)
            question_widget = self.query_one("#question", Static)
            status_widget = self.query_one("#status", Static)
        except NoMatches:
            return
        self.sample_follow_position()
        explorer = self.query_one(SessionExplorer)
        if explorer.display:
            explorer.refresh_catalog()
        self.shown_sid = self.client.workspace.rekeys.get(
            self.shown_sid, self.shown_sid
        )
        sid = self.client.attached_sid
        self.client.buffers.open(sid)
        self.client.save_tabs()
        tabs = tab_line(self.client, self.size.width)
        signature = (tabs.plain, tuple(tabs.spans))
        if signature != getattr(self, "tab_signature", None):
            self.query_one("#session-tabs", Static).update(tabs)
            self.tab_signature = signature
        if sid != self.shown_sid:
            reader.clear_yank()
            reader.follow_layout_pending = False
            reader.follow_scroll_sid = None
            reader.follow_revision += 1
            self.remember()
            self.shown_sid = sid
            self.pending_jump = None
            self.starts = []
            self.rendered_version = -1
            self.command_mode = self.answer_mode = False
            self.mode = "NORMAL"
            self.shortcut_prefix = ()
            editor.set_mode("NORMAL")
            reader.set_mode("NORMAL")
            self.prefix = ""
            if sid:
                view = self.client.workspace.view(sid)
                editor.load_text(view.draft)
                editor.move_cursor(view.draft_cursor)
            else:
                editor.load_text("")
                reader.load_text("")
                reader.parent.reset(None)
            reader.focus()
        elif sid:
            self.remember()
        view = self.client.workspace.view(sid) if sid else None
        if view and view.recovered_queued_text and not (
            self.command_mode or self.answer_mode
        ):
            text = editor.text
            for prompt in view.recovered_queued_text:
                if text != prompt:
                    text += ("\n\n" if text else "") + prompt
            view.recovered_queued_text.clear()
            if text != editor.text:
                editor.load_text(text)
                editor.move_cursor(editor.document.end)
            view.draft = text
            self.client.notice = "Queue rejected; text restored to draft"
        row = self.client.workspace.catalog.get(sid, {})
        projection_identity = (
            (sid, row.get("cwd"), view.artifact_epoch) if view else None
        )
        if reader.parent.identity != projection_identity:
            self.rendered_version = -1
        title = row.get("summary") or row.get("first_prompt") or sid
        heading = (
            "Session: " + " ".join(_safe_remote_text(title[:512]).split())
            if title
            else f"No session · {self.client.keys.label('tree')}: session tree"
        )
        self.update_chrome(
            "#session-title",
            f"{self.client.engine.title()} / {self.client.space.title()} · {heading}",
        )
        if view:
            p = view.presentation
            completed = (p.visible_goal() or {}).get(
                "status"
            ) == "complete" or (
                not p.visible_goal() and p.plan and p.plan_terminal()
            )
            failed = p.plan and p.plan.get("status") in {
                "failed",
                "interrupted",
                "cancelled",
            }
            color = "red" if failed else "green" if completed else "cyan"
            self.update_chrome(
                "#progress",
                p.progress_label(self.client.keys.label("goal")),
                color,
            )
            self.update_chrome("#usage", p.usage_label(), "dim")
            self.update_chrome("#settings", settings_text(p))
            self.update_chrome(
                "#attachments",
                "Attachments: " + ", ".join(a["name"] for a in view.attachments)
                if view.attachments
                else "",
            )
            _, choices = self.suggestions()
            self.update_chrome(
                "#suggestions",
                self.completion_hint(choices),
            )
            completion = p.completion
            completion_id = completion.get("completion_id")
            key = (sid, completion_id)
            if (
                completion.get("unread")
                and completion_id
                and key not in self.acknowledged
                and view.follow
                and self.app_focus
                and not self.client.demo
            ):
                self.acknowledged.add(key)
                self.run_worker(
                    self.acknowledge_completion(sid, completion_id)
                )
        if view and view.version != self.rendered_version:
            # A replaced history page may no longer contain the stored block.
            # Keep the old logical row as a bounded fallback in this buffer,
            # but never leak another session's viewport into a newly opened one.
            previous_selection = reader.selection if self.starts else (
                Selection.cursor((0, 0))
            )
            previous_top = reader.wrapped_document.offset_to_location(
                reader.scroll_offset
            ) if self.starts else (0, 0)
            text, starts = view.render(
                detail_key=self.client.keys.layer_label("reader", "details"),
                older_key=self.client.keys.layer_label("reader", "older"),
                newer_key=self.client.keys.layer_label("reader", "newer"),
                close_key=self.client.keys.layer_label("reader", "close"),
            )
            text, starts = reader.parent.project(
                text, starts, projection_identity
            )
            reader.style_messages(text, starts)
            reader.follow_layout_pending = True
            with reader.presentation_update():
                if text != reader.text:
                    reader.load_text(text)
                else:
                    reader.refresh()  # Styles may change without new text.
                if view.follow and not view.anchor[0]:
                    reader.move_cursor(location(text, len(text)))
                else:
                    reader.selection = Selection(
                        location(
                            text, reader.projection.resolve(
                                view, view.selection, starts, len(text),
                                fallback=offset(text, previous_selection.start),
                            )
                        ),
                        location(
                            text, reader.projection.resolve(
                                view, view.anchor, starts, len(text),
                                fallback=offset(text, previous_selection.end),
                            )
                        ),
                    )
            if view.follow:
                # Commit the viewport before the next frame, not afterwards.
                self.follow_tail(sid)
            if not view.follow:
                top = location(
                    text, reader.projection.resolve(
                        view, view.viewport, starts, len(text),
                        fallback=offset(text, previous_top),
                    )
                )
                scroll = reader.wrapped_document.location_to_offset(top)
                reader.scroll_to(scroll.x, scroll.y, animate=False, force=True)
            self.call_after_refresh(self.finish_follow_layout, sid)
            self.starts = starts
            self.rendered_version = view.version
            self.rendered_clock = None
            if self.pending_jump and not view.loading:
                _, role, direction = self.pending_jump
                self.pending_jump = None
                self.jump(role, direction=direction)
        if view:
            self.refresh_turn_clocks(view, reader)
            if self.app_focus and not view.loading and self.starts:
                view.read_tab()
        reader.parent.sync()
        self.call_after_refresh(reader.parent.sync)
        if view is None:
            for selector in (
                "#progress",
                "#usage",
                "#settings",
                "#attachments",
                "#suggestions",
            ):
                self.update_chrome(selector, "")
        ask = self.client._pending_ask_for_attached()
        async_questions = pending_async(view) if view else []
        question = ""
        if ask:
            options = "  ".join(
                f"{i}. {o['label']}"
                for i, o in enumerate(ask.get("options", []), 1)
            )
            question = (
                f"? {ask.get('question', '')}\n{options}\n"
                f"{self.client.keys.label('answer')}: answer"
            )
        elif async_questions:
            count = sum(len(b.data["questions"]) for b in async_questions)
            question = (
                f"? {count} non-blocking question(s) · task may continue\n"
                f"{self.client.keys.label('answer')}: answer without stopping"
            )
        question = _safe_remote_text(question)
        if question != self.question_text:
            question_widget.update(question)
            question_widget.display = bool(question)
            self.question_text = question
        state = view.state if view else "Select a session"
        if view:
            if view.write_state not in {"unknown", "writable"}:
                state += " · " + view.write_state
            if view.queue:
                state += f" · queued {len(view.queue)}"
            current = view.presentation.turns.get(view.presentation.active)
            if current:
                if view.state == "running" and current.status != "running":
                    state += " · Processing (synchronizing turn)"
                elif view.state != "idle" or current.status != "running":
                    activity, _, elapsed = current.label().rpartition(" · ")
                    state += " · " + elapsed
                    if activity != view.state:
                        state += " · " + " ".join(activity.split())
            if view.presentation.control.get("reason"):
                state += " · " + " ".join(
                    view.presentation.control["reason"].split()
                )
        reading = " · reading" if view and not view.follow else ""
        demo = "OFFLINE DEMO · " if self.client.demo else ""
        pane = "DRAFT" if self.focused is editor else "READ"
        status = (
            f"{demo}{pane} {self.mode} · {state}{reading}\n"
            f"{self.client.keys.label('help')}: help · {self.client.notice}"
        )
        status_key = (status, self.size.width)
        if status_key != self.status_text:
            status_widget.update(styled_status(status, width=self.size.width))
            self.status_text = status_key

    def update_chrome(
        self, selector: str, text: str | Text, color: str = ""
    ) -> None:
        rendered = text if isinstance(text, Text) else Text(text, style=color)
        signature = (rendered.plain, tuple(rendered.spans), rendered.style)
        if self.chrome_signature.get(selector) != signature:
            widget = self.query_one(selector, Static)
            widget.update(rendered)
            widget.display = bool(text)
            self.chrome_signature[selector] = signature

    def refresh_turn_clocks(self, view, reader) -> None:
        """Update elapsed headers without reloading history or the document."""
        now = time.time()
        tick = int(now)
        if self.rendered_clock == tick:
            return
        self.rendered_clock = tick
        seen = set()
        updates = []
        for start, block in self.starts:
            if block.turn in seen:
                continue
            seen.add(block.turn)
            turn = view.presentation.turns.get(block.turn)
            if not turn or turn.status != "running" or turn.started is None:
                continue
            if turn.ended is not None or turn.duration_ms is not None:
                continue
            row, _ = reader.document.get_location_from_index(start)
            old = reader.document.get_line(row)
            new = view.block_header(
                block, show_turn=True, now=now,
                detail_key=self.client.keys.layer_label("reader", "details"),
            )
            # Clock updates only replace a single header line. Never touch
            # multi-line activity text or replace content from another block.
            if old != new and "\n" not in new:
                updates.append((start, row, old, new))
        if not updates:
            return
        top = reader.wrapped_document.offset_to_location(reader.scroll_offset)
        with reader.presentation_update():
            for _, row, old, new in reversed(updates):
                reader.replace(
                    new, (row, 0), (row, len(old)), maintain_selection_offset=True
                )
        # Earlier headers can gain a digit (9s -> 10s). Rebase block offsets
        # before remembering selections, or the next data event shifts them.
        deltas = {start: len(new) - len(old) for start, _, old, new in updates}
        reader.projection.shift_headers(updates)
        shift = 0
        starts = []
        for start, block in self.starts:
            starts.append((start + shift, block))
            shift += deltas.get(start, 0)
        self.starts = starts
        if view.follow:
            self.follow_tail(self.shown_sid)
            self.call_after_refresh(self.follow_tail, self.shown_sid)
        else:
            scroll = reader.wrapped_document.location_to_offset(top)
            reader.scroll_to(scroll.x, scroll.y, animate=False, force=True)
        # Clock replacements are presentation only, not user undo history.
        reader.history.clear()
        self.read_position_signature = None
        self.remember()

    def cancel_editor_overlay(self) -> None:
        if self.command_mode or self.answer_mode:
            editor = self.query_one(Composer)
            editor.load_text(self.command_backup)
            editor.move_cursor(self.command_backup_cursor)
        self.command_mode = self.answer_mode = False

    def normal(self) -> None:
        self.cancel_editor_overlay()
        self.query_one(Composer).set_mode("NORMAL")
        self.query_one(Transcript).set_mode("NORMAL")
        self.mode = "NORMAL"
        self.prefix = ""
        self.shortcut_prefix = ()
        reader = self.query_one(Transcript)
        reader.reconcile_read_cursor()
        reader.focus(scroll_visible=False)
        self.remember()

    def action_sessions(self) -> None:
        self.shortcut_prefix = ()
        if len(self.screen_stack) > 1:
            return
        self.remember()
        self.prefix = ""
        self.query_one(Composer).prefix = ""
        explorer = self.query_one(SessionExplorer)
        explorer.display = not explorer.display
        if explorer.display:
            self.explorer_return_draft = self.focused is self.query_one(
                Composer
            )
            explorer.refresh_catalog()
            explorer.query_one(SessionTree).focus()
        else:
            if self.explorer_return_draft:
                self.query_one(Composer).focus()
            else:
                self.query_one(Transcript).focus()

    async def action_toggle_engine(self) -> None:
        await self.change_surface(
            "claude" if self.client.engine == "codex" else "codex",
            self.client.space,
        )

    async def action_toggle_space(self) -> None:
        await self.change_surface(
            self.client.engine,
            "work" if self.client.space == "code" else "code",
        )

    async def change_surface(self, engine: str, space: str) -> None:
        if len(self.screen_stack) > 1:
            return
        from_tree = isinstance(self.focused, (SessionTree, TreeSearch))
        self.remember()
        self.normal()
        await self.client.switch_surface(engine, space)
        self.paint()
        if from_tree:
            self.query_one(SessionTree).focus()

    async def normal_shortcut(self, key: str) -> bool:
        """Leader actions only in the main Normal/Visual panes, never typing."""
        if len(self.screen_stack) > 1 or self.command_mode or self.answer_mode:
            self.shortcut_prefix = ()
            return False
        chords = dict(self.client.keys.chords)
        if self.focused is self.query_one(Transcript):
            chords.update({tuple(k.split()): LAYERS["reader"][name].action
                           for name, keys in self.client.keys.layers["reader"].items()
                           for k in keys})
        chord = (*self.shortcut_prefix, key)
        pending = bool(self.shortcut_prefix)
        self.shortcut_prefix = ()
        if action := chords.get(chord):
            self.prefix = ""
            self.query_one(Composer).prefix = ""
            await self.run_action(action)
            return True
        if any(keys[: len(chord)] == chord for keys in chords):
            self.shortcut_prefix = chord
            self.client.notice = "Shortcut: " + " ".join(chord) + " …"
            return True
        if (self.focused is self.query_one(Transcript)
                and chord in {("g", "e"), ("g", "E")}):
            reader = self.query_one(Transcript)
            reader.prefix = "g"
            reader.edit_key(key)
            self.stop_following()
            self.remember()
            return True
        return pending  # Invalid/cancelled chord never edits the draft.

    def action_cancel_draft_command(self):
        self.cancel_editor_overlay()

    def action_command_editor(self):
        self.open_editor()

    def action_latest_message(self, role):
        self.jump(role)
        self.remember()

    def action_read_start(self):
        self.record_jump()
        self.stop_following()
        self.query_one(Transcript).move_cursor((0, 0))
        self.remember()

    async def action_read_follow(self):
        self.record_jump()
        sid = self.client.attached_sid
        if sid:
            view = self.client.workspace.view(sid)
            view.follow = True
            reader = self.query_one(Transcript)
            reader.move_cursor(reader.document.end)
            self.follow_tail(sid)
            if view.tail_hidden:
                self.client.notice = "Loading newest turns…"
                await self.client._request_history(sid, force=True)
        self.remember()

    async def action_read_details(self):
        if self.client.attached_sid:
            await self.expand()
            self.remember()

    def action_read_close(self):
        if self.mode == "NORMAL" and self.collapse_detail():
            return
        reader = self.query_one(Transcript)
        reader.selection = Selection.cursor(reader.cursor_location)
        self.normal()

    async def action_read_older(self):
        sid = self.client.attached_sid
        if not sid:
            return
        view = self.client.workspace.view(sid)
        block = self.current_block()
        while block and (parent := view.group_parents.get(block.id)):
            owner = next((b for _, b in self.starts if b.id == parent), None)
            if owner is None or owner is block:
                break
            block = owner
        if block and block.role == "detail" and block.expanded:
            if view.details.get(block.turn):
                await self.request_detail(block, older=True)
        elif view.has_more and view.oldest and not view.loading:
            view.loading = True
            if not await self.client._send(GetHistory(
                session_id=sid, detail="summary", before=view.oldest,
                limit=4, client_id=self.client.client_id,
            )):
                view.loading = False

    async def action_read_newer(self):
        sid = self.client.attached_sid
        if not sid:
            return
        view = self.client.workspace.view(sid)
        block = self.current_block()
        while block and (parent := view.group_parents.get(block.id)):
            owner = next((b for _, b in self.starts if b.id == parent), None)
            if owner is None or owner is block:
                break
            block = owner
        if (block and block.role == "detail" and block.expanded
                and view.details_newer.get(block.turn)):
            await self.request_detail(block, newer=True)

    def action_quote_selection(self) -> None:
        self.quote()

    def action_toggle_pane(self) -> None:
        self.shortcut_prefix = ()
        if len(self.screen_stack) > 1:
            self.screen.focus_next()
            return
        editor = self.query_one(Composer)
        if isinstance(self.focused, (SessionTree, TreeSearch)):
            editor.set_mode("NORMAL")
            editor.focus()
            return
        if self.focused is editor:
            self.action_focus_read()
        else:
            self.action_focus_draft()

    def action_focus_draft(self) -> None:
        self.shortcut_prefix = ()
        if isinstance(self.focused, (SessionTree, TreeSearch)):
            self.query_one(SessionExplorer).move_selection(True)
            return
        if len(self.screen_stack) > 1:
            if hasattr(self.screen, "move_selection"):
                self.screen.move_selection(down=True)
            return
        editor = self.query_one(Composer)
        if self.focused is editor:
            return
        self.remember()
        self.prefix = ""
        self.shortcut_prefix = ()
        self.stop_following()
        editor.set_mode("NORMAL")
        editor.focus()

    def action_focus_read(self) -> None:
        self.shortcut_prefix = ()
        if isinstance(self.focused, (SessionTree, TreeSearch)):
            self.query_one(SessionExplorer).move_selection(False)
            return
        if len(self.screen_stack) > 1:
            if hasattr(self.screen, "move_selection"):
                self.screen.move_selection(down=False)
            return
        if self.focused is self.query_one(Transcript):
            self.query_one(Transcript).reconcile_read_cursor()
            self.remember()
            return
        self.remember()
        self.normal()

    async def pick_session(self, sid: str | None) -> None:
        if sid is None:
            return
        await self.attach_session(sid)

    async def action_cycle_buffer(self, direction: int) -> None:
        if len(self.screen_stack) != 1:
            return
        sid = self.client.buffers.neighbor(self.client.attached_sid, direction)
        if sid and sid != self.client.attached_sid:
            await self.attach_session(sid)

    def action_search_buffers(self) -> None:
        if len(self.screen_stack) == 1:
            self.remember()
            self.push_screen(BufferPicker(self.client), self.pick_buffer)

    async def pick_buffer(self, sid) -> None:
        if sid in self.client.buffers.ids:
            await self.attach_session(sid)

    async def action_close_buffer(self) -> None:
        if len(self.screen_stack) != 1:
            return
        self.remember()
        sid = self.client.attached_sid
        if sid not in self.client.buffers.ids:
            return
        target = self.client.buffers.close(sid)
        self.client.navigation_revision += 1
        self.client.restore_pending = False
        self.client._pending_new_request = None
        # Clear focus before repainting, otherwise paint would reopen the tab.
        self.client.attached_sid = None
        if target:
            await self.attach_session(target)
        else:
            self.paint()
            self.normal()
        self.client.notice = (
            "Closed local tab; server session and tasks are unchanged"
        )

    async def attach_session(self, sid: str) -> None:
        self.remember()
        row = self.client.workspace.catalog.get(sid, {})
        await self.client._attach(sid, row.get("engine", self.client.engine))
        self.paint()
        self.normal()
        self.set_class(self.size.width < 85, "narrow")

    async def action_submit(self) -> None:
        if len(self.screen_stack) > 1:
            if hasattr(self.screen, "action_submit"):
                await self.screen.action_submit()
            return
        await self.send(False)

    def action_paste_image(self) -> None:
        editor = self.query_one("#composer", Composer)
        if len(self.screen_stack) != 1 or self.focused is not editor:
            # Keep Textual's ordinary text-paste behavior in modal forms.
            if isinstance(self.focused, TextArea):
                self.focused.action_paste()
            return
        sid = self.client.attached_sid
        if not sid:
            self.client.notice = "Select a session before pasting an image"
            return
        if self.clipboard_busy:
            self.client.notice = "Reading clipboard image…"
            return
        self.clipboard_busy = True
        self.client.notice = "Reading clipboard image…"
        self.run_worker(self.paste_clipboard_image(sid), group="clipboard")

    async def paste_clipboard_image(self, sid):
        try:
            attachment = await read_clipboard_image()
            # A switch/re-key while the helper runs must not move the image
            # to another session. Stage only; never send a Query here.
            sid = self.client.workspace.rekeys.get(sid, sid)
            view = self.client.workspace.view(sid)
            candidate = [*view.attachments, attachment]
            error = validate_attachments(
                [a["content"] for a in candidate if a["image"]],
                [a["content"] for a in candidate if not a["image"]],
            )
            if error:
                raise ValueError(error)
            view.attachments.append(attachment)
            self.client.notice = (
                "Image attached to this draft; not sent"
                if self.client.attached_sid == sid
                else f"Image attached to original session {sid}; not sent"
            )
        except (ValueError, OSError) as exc:
            self.client.notice = _safe_remote_text(str(exc))
        finally:
            self.clipboard_busy = False

    async def action_queue(self) -> None:
        await self.send(True)

    async def action_stop(self) -> None:
        # Tree selection and modal targets may differ from the attached chat.
        # Never stop a background session from one of those local key scopes.
        if len(self.screen_stack) > 1 or isinstance(
            self.focused, (SessionTree, TreeSearch)
        ):
            return
        sid = self.client.attached_sid
        if not sid:
            self.client.notice = "Select a session first"
            return
        view = self.client.workspace.view(sid)
        if view.write_state != "writable":
            self.client.notice = f"Stop unavailable: {view.write_state}"
            return
        if view.state != "running":
            self.client.notice = "No running turn to stop"
            return
        self.shortcut_prefix = ()
        if await self.client._send(Interrupt(sid=sid)):
            # Only the server's terminal event ends the turn. Preserve the
            # draft, attachments, queue, and reading position in the meantime.
            self.client.notice = "Stop requested; queued messages are kept"

    async def send(self, queue: bool) -> None:
        self.shortcut_prefix = ()
        if isinstance(self.focused, (SessionTree, TreeSearch)):
            return
        if len(self.screen_stack) > 1:
            return
        if self.submitting:
            return
        if queue and (self.command_mode or self.answer_mode):
            self.client.notice = (
                "Queue is for prompts; "
                f"{self.client.keys.label('send')} submits commands/answers"
            )
            return
        editor = self.query_one(Composer)
        text = editor.text
        current_view = (
            self.client.workspace.view(self.client.attached_sid)
            if self.client.attached_sid
            else None
        )
        if not text.strip() and not (current_view and current_view.attachments):
            return
        self.submitting = True
        self.sample_follow_position()
        sid = self.client.attached_sid
        try:
            if self.command_mode:
                self.normal()
                await self.command(text.lstrip(":/"))
                if self.client._quitting:
                    self.exit()
                return
            if text.startswith("/") and text.split(maxsplit=1)[0][1:] in {
                "goal",
                "plan",
                "status",
                "context",
                "settings",
                "queue",
                "actions",
                "reports",
                "background",
                "notices",
                "web",
                "help",
                *ACTIONS,
            }:
                editor.load_text("")
                self.normal()
                await self.command(text[1:])
                return
            if self.answer_mode:
                if await self.client.answer(text.strip()):
                    self.normal()
                return
            if await self.client.submit(text, queue=queue):
                sid = self.client.workspace.rekeys.get(sid, sid)
                if sid:
                    self.client.workspace.view(sid).draft = ""
                if self.client.attached_sid == sid and editor.text == text:
                    editor.load_text("")
                if not queue and self.client.attached_sid == sid:
                    self.pending_jump = None
                    self.paint()
                    self.call_after_refresh(self.follow_tail, sid)
        except (ValueError, OSError) as exc:
            self.client.notice = _safe_remote_text(exc)
        finally:
            self.submitting = False

    def action_answer(self) -> None:
        self.shortcut_prefix = ()
        if isinstance(self.focused, (SessionTree, TreeSearch)):
            return
        if len(self.screen_stack) > 1:
            return
        ask = self.client._pending_ask_for_attached()
        if not ask:
            sid = self.client.attached_sid
            questions = (
                pending_async(self.client.workspace.view(sid)) if sid else []
            )
            if questions:
                self.remember()
                self.push_screen(
                    # One native question message per canonical Web envelope.
                    AsyncQuestionDialog(self.client, sid, questions[:1])
                )
                return
            self.client.notice = "No pending question"
            return
        self.remember()
        self.push_screen(QuestionDialog(self.client, ask))

    def open_editor(self, *, answer: bool = False) -> None:
        if self.command_mode or self.answer_mode:
            return
        self.remember()
        editor = self.query_one(Composer)
        self.command_backup = editor.text
        self.command_backup_cursor = editor.cursor_location
        self.answer_mode = answer
        self.command_mode = not answer
        editor.load_text("")
        editor.set_mode("INSERT")
        editor.focus()
        self.mode = "INSERT"
        self.client.notice = (
            f"Answer, then {self.client.keys.label('send')}"
            if answer
            else (
                "Command: new /directory | stop | sessions | engine codex | "
                f"space work | quit; {self.client.keys.label('send')}"
            )
        )

    async def read_key(self, key: str) -> bool:
        # A subsequent reading gesture supersedes an outstanding page jump.
        self.pending_jump = None
        reader = self.query_one(Transcript)
        reader.vim_mode = self.mode
        if key == "escape" and reader.prefix:
            reader.prefix = ""
            return True
        if (
            not self.prefix
            and not reader.prefix
            and await self.normal_shortcut(key)
        ):
            return True
        # These keys have workspace-specific meanings only outside a pending
        # Vim command. For example ya( must not open a menu, and yi( must not
        # focus the draft. Share the exact parser with every text editor.
        special = {
            "g",
            "i",
            "escape",
            "v",
            "V",
            "j",
            "k",
            "h",
            "l",
            "ctrl+d",
            "ctrl+u",
        }
        if not self.prefix and (
            reader.prefix
            or key not in special
            or (key == "i" and self.mode.startswith("VISUAL"))
        ):
            before = reader.selection
            if reader.edit_key(key):
                if (reader.prefix or reader.selection != before
                        or reader.vim_mode.startswith("VISUAL")):
                    self.stop_following()
                self.remember()
                return True
        prefix, self.prefix = self.prefix, ""
        if prefix:
            if prefix == "g" and key == "g":
                self.stop_following()
                if self.mode == "VISUAL":
                    reader.move_visual((0, 0))
                else:
                    reader.move_cursor((0, 0))
            elif prefix == "g" and key in {"e", "E"}:
                reader.prefix = "g"
                reader.edit_key(key)
                self.stop_following()
                self.remember()
            return True
        if key == "g":
            self.prefix = key
        elif key == "i":
            self.client.notice = (
                f"{self.client.keys.label('focus_draft')} focuses the draft; "
                "i enters Insert there"
            )
        elif key == "escape":
            reader.selection = Selection.cursor(reader.cursor_location)
            self.normal()
        elif key in {"v", "V"}:
            self.stop_following()
            self.mode = "VISUAL LINE" if key == "V" else "VISUAL"
            reader.set_mode(self.mode)
            if key == "V":
                row, _ = reader.cursor_location
                reader.selection = Selection(
                    (row, 0), (row, len(reader.document.get_line(row)))
                )
        elif key in {"j", "k", "h", "l", "ctrl+d", "ctrl+u"}:
            self.stop_following()
            row, col = reader.vim_cursor_location()
            step = max(1, reader.size.height // 2)
            row += {"j": 1, "k": -1, "ctrl+d": step, "ctrl+u": -step}.get(
                key, 0
            )
            col += {"h": -1, "l": 1}.get(key, 0)
            row = min(max(0, row), reader.document.line_count - 1)
            col = min(max(0, col), len(reader.document.get_line(row)))
            if self.mode == "VISUAL LINE":
                start_row = reader.selection.start[0]
                if row >= start_row:
                    reader.selection = Selection(
                        (start_row, 0),
                        (row, len(reader.document.get_line(row))),
                    )
                else:
                    reader.selection = Selection(
                        (start_row, len(reader.document.get_line(start_row))),
                        (row, 0),
                    )
                reader.scroll_cursor_visible()
            elif self.mode == "VISUAL":
                reader.move_visual((row, col))
            else:
                reader.move_cursor((row, col))
        else:
            if key in {
                "up",
                "down",
                "left",
                "right",
                "pageup",
                "pagedown",
                "home",
            }:
                self.stop_following()
            return False
        self.remember()
        return True

    def jump_position(self):
        self.remember()
        view = self.client.workspace.view(self.shown_sid)
        return view.anchor, view.viewport

    def record_jump(self) -> None:
        if not self.shown_sid:
            return
        view = self.client.workspace.view(self.shown_sid)
        point = self.jump_position()
        if not view.jump_back or view.jump_back[-1] != point:
            view.jump_back.append(point)
            del view.jump_back[:-100]
        view.jump_forward.clear()

    def action_jump_history(self, direction: int) -> None:
        if (len(self.screen_stack) != 1 or not self.shown_sid
                or self.focused is not self.query_one(Transcript)):
            return
        view = self.client.workspace.view(self.shown_sid)
        source, target = ((view.jump_back, view.jump_forward)
                          if direction < 0
                          else (view.jump_forward, view.jump_back))
        if not source:
            return
        target.append(self.jump_position())
        del target[:-100]
        anchor, viewport = source.pop()
        self.stop_following()
        self.pending_jump = None
        reader = self.query_one(Transcript)
        reader.move_cursor(location(
            reader.text, reader.projection.resolve(
                view, anchor, self.starts, len(reader.text)
            )
        ))
        top = location(
            reader.text, reader.projection.resolve(
                view, viewport, self.starts, len(reader.text)
            )
        )
        scroll = reader.wrapped_document.location_to_offset(top)
        reader.scroll_to(scroll.x, scroll.y, animate=False, force=True)
        self.remember()

    def jump(self, role: str | None = None, *, direction: int = 0) -> bool:
        reader = self.query_one(Transcript)
        position = offset(reader.text, reader.cursor_location)
        candidates = [
            (start, block)
            for start, block in self.starts
            if block.role in {"user", "assistant"}
            and (role is None or block.role == role)
            and block.channel != "thinking"
        ]
        if role == "assistant" and not direction:
            latest_turn = candidates[-1][1].turn if candidates else ""
            candidates = [
                (start, b) for start, b in candidates if b.turn == latest_turn
            ]
            finals = [
                (start, b) for start, b in candidates if b.channel == "final"
            ]
            candidates = finals or candidates
        elif role == "assistant":
            # One answer anchor per turn, preferring the final over progress.
            answers = {}
            for start, block in candidates:
                previous = answers.get(block.turn)
                if previous is None or (
                    block.channel == "final" and previous[1].channel != "final"
                ):
                    answers[block.turn] = (start, block)
            candidates = sorted(answers.values(), key=lambda item: item[0])
        if direction:
            candidates = [
                (start, b)
                for start, b in candidates
                if (start - position) * direction > 0
            ]
        if candidates:
            start, _ = candidates[0] if direction == 1 else candidates[-1]
            self.record_jump()
            self.stop_following()
            reader.move_cursor(location(reader.text, start), center=True)
            return True
        else:
            self.client.notice = (
                "No matching message on this page; "
                f"{self.client.keys.layer_label('reader', 'older')} "
                "loads older turns"
            )
        return False

    async def action_jump_message(self, role: str, direction: int) -> None:
        self.query_one(Transcript).focus()
        found = self.jump(role or None, direction=direction)
        self.remember()
        sid = self.client.attached_sid
        if found or direction != -1 or not sid:
            return
        view = self.client.workspace.view(sid)
        if view.has_more and view.oldest and not view.loading:
            view.loading = True
            self.pending_jump = (sid, role or None, direction)
            if not await self.client._send(
                GetHistory(
                    session_id=sid,
                    detail="summary",
                    before=view.oldest,
                    limit=4,
                    client_id=self.client.client_id,
                )
            ):
                view.loading = False
                self.pending_jump = None

    def quote(self) -> None:
        reader = self.query_one(Transcript)
        selected = reader.selected_text
        if not selected:
            self.client.notice = "Select text with v/V first"
            return
        editor = self.query_one(Composer)
        quote = "\n".join("> " + line for line in selected.splitlines())
        editor.insert(
            ("\n\n" if editor.text else "") + quote + "\n",
            location(editor.text, len(editor.text)),
            maintain_selection_offset=False,
        )
        editor.move_cursor(location(editor.text, len(editor.text)))
        reader.selection = Selection.cursor(reader.cursor_location)
        self.mode = "NORMAL"
        self.client.notice = "Added to draft; not sent"
        self.remember()

    def current_block(self):
        reader = self.query_one(Transcript)
        sid = self.client.attached_sid
        if not sid:
            return None
        view = self.client.workspace.view(sid)
        identity, _ = view.locate(
            offset(reader.text, reader.cursor_location), self.starts
        )
        return next((b for _, b in self.starts if b.id == identity), None)

    def collapse_detail(self) -> bool:
        block = self.current_block()
        if not block:
            return False
        view = self.client.workspace.view(self.client.attached_sid)
        parent = view.group_parents.get(block.id)
        if parent and block.role not in {"detail", "tool_group"}:
            block = next((b for _, b in self.starts if b.id == parent), block)
        if (
            not block.expanded
            or not (
                block.role in {"detail", "tool_group", "tool", "process"}
                or block.channel == "thinking"
            )
        ):
            return False
        block.expanded = False
        if block.role == "detail":
            view.collapsed_details.add(block.turn)
        view.version += 1
        self.stop_following()
        # Collapse to the owning header, not an offset into a vanished body.
        start = next(
            start for start, item in self.starts if item.id == block.id
        )
        reader = self.query_one(Transcript)
        reader.move_cursor(location(reader.text, start))
        self.remember()
        return True

    async def request_detail(self, block, *, older=False, newer=False) -> None:
        sid = self.client.attached_sid
        view = self.client.workspace.view(sid)
        view.collapsed_details.discard(block.turn)
        if block.turn and view.revision:
            await self.client._send(
                GetTurnDetail(
                    session_id=sid,
                    turn_id=block.turn,
                    revision=view.revision,
                    before=(
                        view.details_newer.get(block.turn) if newer else
                        view.details.get(block.turn) if older else None
                    ),
                    client_id=self.client.client_id,
                )
            )

    async def expand(self) -> None:
        if self.collapse_detail():
            return
        block = self.current_block()
        if not block:
            return
        view = self.client.workspace.view(self.client.attached_sid)
        self.stop_following()
        if block.role == "tool_group":
            if block.data.get("request_detail"):
                await self.request_detail(block)
            else:
                block.expanded = True
                view.version += 1
            return
        if block.role in {"assistant", "user"} and (
            block.channel != "thinking"
        ) and block.turn:
            # Following the tail leaves the cursor on the final answer, not
            # on its earlier folded header. Prefer that same turn's detail.
            target = next((b for _, b in self.starts
                           if b.role == "detail" and b.turn == block.turn), None)
            if target:
                block = target
                reader = self.query_one(Transcript)
                start = next(i for i, b in self.starts if b is target)
                reader.move_cursor(location(reader.text, start))
                if self.collapse_detail():
                    return
        if block.role in {"tool", "process"} or block.channel == "thinking":
            block.expanded = True
            view.version += 1
        elif block.role == "detail" and (
            block.turn in view.details or block.data.get("local")
        ):
            block.expanded = True
            view.collapsed_details.discard(block.turn)
            view.version += 1
        else:
            await self.request_detail(block)

    def action_actions(self) -> None:
        if len(self.screen_stack) == 1:
            self.remember()
            self.push_screen(
                ActionPicker(self.client, self.client.attached_sid)
            )

    async def action_diagram_browser(self) -> None:
        if len(self.screen_stack) != 1 or not self.client.attached_sid:
            return
        from cc_remote.tui_diagram_browser import open_session_browser
        try:
            self.client.notice = await open_session_browser(
                self.client, self.client.attached_sid,
            )
        except ValueError as error:
            self.client.notice = str(error)

    def action_preview_files(self) -> None:
        if len(self.screen_stack) != 1 or not self.client.attached_sid:
            return
        self.remember()
        sid = self.client.attached_sid
        view = self.client.workspace.view(sid)
        found = {}
        for block in reversed(view.blocks):
            if block.role not in {"assistant", "detail"}:
                continue
            for ref in references(block.text):
                found.setdefault(ref.path, ref)
                if len(found) == 64:
                    break
            if len(found) == 64:
                break

        def chosen(ref):
            if ref and self.client.attached_sid == sid:
                self.push_screen(
                    FilePreviewScreen(self.client, sid, ref, self.graphics)
                )

        refs = list(found.values())
        if len(refs) == 1:
            chosen(refs[0])
        elif refs:
            self.push_screen(FileHints(refs, open_preview=chosen))
        else:
            self.client.notice = (
                "No Markdown/image paths in loaded assistant messages"
            )

    def action_new_session(self) -> None:
        if len(self.screen_stack) != 1:
            return
        from cc_remote.tui_settings import SettingsForm

        self.remember()
        self.push_screen(SettingsForm(self.client, None, new=True))

    def action_choose_setting(self, field: str | None = None) -> None:
        if len(self.screen_stack) != 1 or not self.client.attached_sid:
            return
        from cc_remote.tui_settings import SettingsForm

        self.remember()
        self.push_screen(
            SettingsForm(
                self.client, self.client.attached_sid, initial_field=field
            )
        )

    async def action_panel(self, name: str) -> None:
        if name == "Settings":
            self.action_choose_setting()
            return
        if len(self.screen_stack) > 1 or (
            not self.client.attached_sid and name != "Help"
        ):
            return
        self.remember()
        sid = self.client.attached_sid or ""
        self.push_screen(
            QueuePanel(self.client, sid)
            if name == "Queue"
            else DetailPanel(self.client, sid, name)
        )
        await self.client.refresh_panel(sid, name)

    async def command(self, text: str) -> None:
        parts = text.split(maxsplit=1)
        if not parts:
            return
        name, arg = parts[0], parts[1] if len(parts) > 1 else ""
        panels = {
            "goal": "Goal / Plan",
            "plan": "Goal / Plan",
            "status": "Status",
            "context": "Usage / Context",
            "settings": "Settings",
            "queue": "Queue",
            "reports": "Reports",
            "background": "Background",
            "notices": "Notices",
            "web": "Web handoff",
        }
        if name in {"engine", "space"}:
            await self.change_surface(
                arg if name == "engine" else self.client.engine,
                arg if name == "space" else self.client.space,
            )
        elif name == "sessions":
            self.action_sessions()
        elif name == "help":
            await self.action_panel("Help")
        elif name == "actions":
            self.action_actions()
        elif name == "new" and not arg:
            self.action_new_session()
        elif name in panels and not arg:
            await self.action_panel(panels[name])
        elif name == "goal" and arg:
            from cc_remote.tui_panels import ActionForm
            from cc_remote.protocol import SetGoal

            sid = self.client.attached_sid
            if not sid:
                raise ValueError("Select a session first")
            if arg == "clear":
                self.push_screen(ActionForm(self.client, sid, "clear_goal"))
            else:
                message = (
                    SetGoal(
                        sid=sid,
                        status={"resume": "active", "pause": "paused"}[arg],
                    )
                    if arg in {"resume", "pause"}
                    else SetGoal(sid=sid, objective=arg)
                )
                await self.client._send(message)
        elif name in ACTIONS:
            from cc_remote.tui_panels import ActionForm

            self.push_screen(
                ActionForm(self.client, self.client.attached_sid, name)
            )
        elif name in {"file", "image", "detach", "attachments"}:
            sid = self.client.attached_sid
            if not sid:
                raise ValueError("Select a session first")
            view = self.client.workspace.view(sid)
            if name in {"file", "image"}:
                attachment = await asyncio.to_thread(
                    read_attachment, arg, image=name == "image"
                )
                candidate = [*view.attachments, attachment]
                error = validate_attachments(
                    [a["content"] for a in candidate if a["image"]],
                    [a["content"] for a in candidate if not a["image"]],
                )
                if error:
                    raise ValueError(error)
                view.attachments.append(attachment)
            elif name == "detach":
                if arg == "all":
                    view.attachments.clear()
                elif arg.isdigit() and 1 <= int(arg) <= len(view.attachments):
                    view.attachments.pop(int(arg) - 1)
                else:
                    raise ValueError(
                        "detach requires an attachment number or all"
                    )
            self.client.notice = "Attachments: " + ", ".join(
                a["name"] for a in view.attachments
            )
        else:
            await self.client._command("/" + text)


def seed_demo(client: WorkspaceClient) -> None:
    """Read/copy/draft preview only: all network writes remain disabled."""
    client.demo = True
    client.attached_sid = "demo-review"
    client.workspace.event(
        {
            "type": "session_list",
            "engine": "codex",
            "sessions": [
                {
                    "session_id": "demo-review",
                    "summary": "Review a change",
                    "cwd": "/example/project",
                    "state": "running",
                    "engine": "codex",
                    "last_modified": "2",
                },
                {
                    "session_id": "demo-tests",
                    "summary": "Inspect test results",
                    "cwd": "/example/tests",
                    "state": "idle",
                    "engine": "codex",
                    "last_modified": "1",
                },
            ],
        }
    )
    for engine, space in sorted(SCOPES - {("codex", "code")}):
        sid = f"demo-{engine}-{space}"
        client.workspace.event(
            {
                "type": "session_list",
                "engine": engine,
                "space": space,
                "sessions": [
                    {
                        "session_id": sid,
                        "summary": f"{engine.title()} {space.title()} example",
                        "cwd": "/example/" + space,
                        "state": "idle",
                        "last_modified": "1",
                    }
                ],
            }
        )
        view = client.workspace.view(sid)
        view.event(
            {
                "type": "user_msg",
                "msg_id": sid,
                "prompt": "Offline scope example.",
            }
        )
        view.write_state = "writable"
    client.catalog_ready.update(SCOPES)
    client.attached_sid = select_session(client.visible_catalog(), None)
    client.attached_engine = client.engine
    client.restore_pending = False
    client.remember_focus()
    for sid, prompt in (
        ("demo-review", "Explain this change and its tests."),
        ("demo-tests", "Summarize the test results."),
    ):
        view = client.workspace.view(sid)
        view.write_state = "writable"
        view.state = "running" if sid == "demo-review" else "idle"
        view.event(
            {
                "type": "user_msg",
                "msg_id": sid + "-question",
                "prompt": prompt,
                "ts": time.time() - 97,
            }
        )
        view.event(
            {
                "type": "delta",
                "message_id": sid + "-answer",
                "channel": "final",
                "text": "This is an offline interaction preview.\n\n"
                f"Try {client.keys.layer_label('reader', 'latest_user')} / "
                f"{client.keys.layer_label('reader', 'latest_assistant')} to jump between messages.\n"
                "Use j/k, v/V and y to select and copy text.\n"
                f"{client.keys.label('quote')} quotes into the draft without sending.\n"
                "In the draft, i inserts and Esc returns to Normal.\n"
                "Use hjkl/w/b, ci(, daw, dd/dw, x, p and u to edit the draft.\n"
                f"{client.keys.label('focus_draft')} focuses the draft; "
                f"{client.keys.label('focus_read')} returns here.\n\n"
                f"{client.keys.label('tree')} opens the left session tree; "
                f"{client.keys.layer_label('tree', 'search')} searches.\n"
                f"{client.keys.label('engine')} switches Claude/Codex; "
                f"{client.keys.label('space')} switches Code/Work.\n"
                "The active shortcuts are listed in Help.\n"
                f"{client.keys.label('answer')} answers a pending model question.\n"
                "No server, model or task is started by this demo.",
            }
        )
        view.event(
            {
                "type": "delta",
                "message_id": sid + "-thinking",
                "channel": "thinking",
                "text": "Offline sample thinking summary.\nExpand this block with "
                + client.keys.layer_label("reader", "details") + ".",
            }
        )
        view.event(
            {
                "type": "turn_plan",
                "item_id": sid + "-plan",
                "plan": [
                    {"step": "Read the change", "status": "completed"},
                    {"step": "Check regressions", "status": "inProgress"},
                    {"step": "Report results", "status": "pending"},
                ],
            }
        )
        view.event(
            {
                "type": "process",
                "item_id": sid + "-command",
                "kind": "command",
                "title": "Running tests (offline sample)",
                "status": "running",
                "phase": "start",
                "command": "pytest",
                "output": "Sample output only; nothing is executed.",
            }
        )
        view.event({"type": "model", "model": "Demo model"})
        view.event({"type": "effort", "effort": "high"})
        view.event(
            {
                "type": "context_report",
                "total_tokens": 42000,
                "max_tokens": 128000,
                "percentage": 32.8125,
                "available": True,
            }
        )
        view.event(
            {
                "type": "rate_limit_update",
                "primary": {"used_percent": 27},
                "limit_id": "codex",
                "secondary": {
                    "used_percent": 41,
                    "window_duration_mins": 10080,
                },
            }
        )
        view.event(
            {
                "type": "rate_limit_update",
                "limit_id": "codex",
                "primary": {"window_duration_mins": 300},
            }
        )
        if sid == "demo-review":
            view.event(
                {
                    "type": "goal_state",
                    "goal_id": "demo-goal",
                    "goal": {
                        "objective": "Review the example change",
                        "status": "active",
                        "tokensUsed": 124000,
                        "tokenBudget": 500000,
                        "timeUsedSeconds": 97,
                        "engine": "codex",
                    },
                }
            )
        else:
            view.event(
                {
                    "type": "turn_end",
                    "result": {
                        "subtype": "success",
                        "is_error": False,
                        "duration_ms": 97000,
                    },
                    "ts": time.time(),
                }
            )
    client.notice = (
        f"Offline demo · {client.keys.label('help')} help · "
        f"{client.keys.label('engine')} engine · {client.keys.label('space')} space"
    )


def run_workspace(client: WorkspaceClient, *, demo: bool = False) -> None:
    def launch(connect=True):
        # Terminal probes must complete before Textual starts reading stdin.
        graphics = detect_graphics()
        app = WorkspaceApp(client, connect=connect)
        app.graphics = graphics
        app.run()

    if demo:
        seed_demo(client)
        launch(connect=False)
        return
    # Local login reuses same-user service configuration without prompting.
    # Remote login, if needed, completes before Textual takes terminal input.
    try:
        asyncio.run(client._authenticate())
    except Exception as exc:
        raise ValueError(f"Login failed: {_safe_remote_text(exc)}") from None
    from cc_remote.tui_tab_store import TabStore

    client.restore_tabs(TabStore(client.url, client.machine_id, client.username))
    try:
        launch()
    finally:
        client.save_tabs()
