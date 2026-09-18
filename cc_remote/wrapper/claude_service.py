"""Machine-side reconstruction of service-owned Claude sessions."""

from __future__ import annotations

import asyncio
from pathlib import Path

from cc_remote.claude_service.client import Connection
from cc_remote.log import logger
from cc_remote.protocol import Delta, UserMsg

log = logger("cc_remote.wrapper.claude_service")


def preserve_timestamp(events, message) -> None:
    """An offline replay retains when native output arrived, not reconnect time."""
    ts = getattr(message, "_cc_service_ts", None)
    if ts is not None:
        for event in events:
            event.ts = ts


class ReplayProjection:
    """Coalesce a native backlog before exposing it to an existing browser.

    The translator still consumes every frame to rebuild tool/channel state.
    Replayed text replaces its exact message's old prefix once; it must not be
    appended onto the browser's already-rendered copy of that same prefix.
    """

    def __init__(self, head: int):
        self.head = head
        self.events = []
        self.text: dict[tuple, int] = {}

    def add(self, events) -> None:
        for event in events:
            if isinstance(event, Delta):
                key = (event.message_id, event.channel)
                index = self.text.get(key)
                if index is None:
                    self.text[key] = len(self.events)
                    self.events.append(event.model_copy(update={"replace": True}))
                else:
                    current = self.events[index]
                    current.text = event.text if event.replace else current.text + event.text
            else:
                self.events.append(event)

    def drain(self):
        events, self.events = self.events, []
        self.text.clear()
        return events


def configure(machine, ctx) -> None:
    if not machine.cfg.claude_service_socket or ctx.btw:
        return
    profile = machine._claude_profile(ctx.claude_profile_id)
    ctx.sdk.service_metadata = {
        "profile_id": profile.id,
        "profile_root": str(profile.config_dir),
        "session_id": ctx.session_id,
        "cwd": ctx.cwd,
        "space": ctx.space,
        "work_id": ctx.work_id,
        "btw": False,
    }


async def restore(machine) -> None:
    if not machine.cfg.claude_service_socket:
        return
    sockets = [("primary", machine.cfg.claude_service_socket)]
    if machine.cfg.claude_service_drain_socket:
        sockets.insert(0, ("drain", machine.cfg.claude_service_drain_socket))
    sessions = []
    identities = set()
    for role, socket in sockets:
        try:
            listed = await _list_sessions(socket)
        except Exception as exc:
            log.warning("persistent Claude service listing failed",
                        service_role=role, error_type=type(exc).__name__)
            continue
        for item in listed:
            metadata = item["metadata"]
            identity = (metadata.get("profile_root"), metadata.get("session_id"),
                        metadata.get("space"), metadata.get("work_id"), metadata.get("btw"))
            if metadata.get("session_id") and identity in identities:
                raise RuntimeError("Claude session exists in both SDK services")
            identities.add(identity)
            sessions.append((socket, item))
    for socket, item in sessions:
        try:
            await _restore_session(machine, socket, item)
        except Exception as exc:
            # All reachable services passed the identity check. A failed session
            # must not strand other sessions' already accepted turns.
            log.warning("persistent Claude session recovery failed",
                        service_id=item.get("id"), error_type=type(exc).__name__)


async def _list_sessions(socket):
    connection = Connection(socket)
    await connection.connect()
    try:
        return await connection.call("list")
    finally:
        await connection.disconnect()


async def _restore_session(machine, socket, item) -> None:
    metadata = item["metadata"]
    if metadata.get("btw"):
        return
    # Resolve the current registry, then bind its native account root. A
    # changed profile name must never attach an old account's SDK by UUID.
    try:
        profile = machine._claude_profile(metadata["profile_id"])
    except (ValueError, KeyError):
        return
    if Path(profile.config_dir).resolve() != Path(metadata["profile_root"]).resolve():
        return
    ctx = await machine._spawn(
        resume_id=metadata.get("session_id"), cwd=metadata["cwd"],
        claude_profile_id=profile.id, space=metadata["space"],
        work_id=metadata.get("work_id"), _service_recovering=True,
        _service_worker_id=item["id"],
        _service_socket=socket,
    )
    if ctx is None:
        raise RuntimeError("could not reattach a persistent Claude session")


async def activate(machine, ctx) -> None:
    client = getattr(ctx.sdk, "client", None)
    if not hasattr(client, "description"):
        return
    recovery = ctx.sdk.service_recovery
    await client.call("metadata", {"value": {"key": ctx.key}})
    if client.description["head"] > client.description["after"]:
        ctx.claude_service_background_replay = ReplayProjection(client.description["head"])
    if recovery is not None:
        ctx.active_msg_id = recovery["id"]
        ctx.claude_write_active = True
        ctx.needs_reload = False
        await machine._emit(ctx, UserMsg(
            msg_id=recovery["id"], prompt=recovery.get("prompt", ""),
            images=recovery.get("images"), files=recovery.get("files"),
            ts=recovery["started_at"],
        ))
        await machine._set_state(ctx, "running")
        ctx.turn_task = asyncio.create_task(machine._run_turn(
            ctx, recovery.get("prompt", ""), _recover_service=True,
        ))
    ctx.sdk.start_service_events()
    ctx.sdk.service_defer_events = False
