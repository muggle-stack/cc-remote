"""Machine-side reconstruction of service-owned Claude sessions."""

from __future__ import annotations

import asyncio
from pathlib import Path

from cc_remote.claude_service.client import Connection
from cc_remote.protocol import Delta, UserMsg


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
    connection = Connection(machine.cfg.claude_service_socket)
    await connection.connect()
    try:
        sessions = await connection.call("list")
    finally:
        await connection.disconnect()
    for item in sessions:
        metadata = item["metadata"]
        if metadata.get("btw"):
            continue
        # Resolve the current registry, then bind its native account root. A
        # changed profile name must never attach an old account's SDK by UUID.
        try:
            profile = machine._claude_profile(metadata["profile_id"])
        except (ValueError, KeyError):
            continue
        if Path(profile.config_dir).resolve() != Path(metadata["profile_root"]).resolve():
            continue
        ctx = await machine._spawn(
            resume_id=metadata.get("session_id"), cwd=metadata["cwd"],
            claude_profile_id=profile.id, space=metadata["space"],
            work_id=metadata.get("work_id"), _service_recovering=True,
            _service_worker_id=item["id"],
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
