"""Native streaming-input steering, shared by the SDK owner and its controller."""

from __future__ import annotations

import hashlib
import json


class ClaudeSteerRejected(RuntimeError):
    """The instruction was definitely not written to the native input stream."""


def steer_message(prompt, native_id: str) -> dict:
    # `next` is consumed at the next safe input boundary. `now` interrupts a
    # running tool; omitting the priority leaves that policy to the CLI default.
    return {"type": "user", "message": {"role": "user", "content": prompt},
            "parent_tool_use_id": None, "uuid": native_id, "priority": "next"}


def _origin_key(origin: dict) -> str:
    kind = origin.get("kind")
    for field in ("taskId", "task_id", "senderTaskId", "fromSession", "verifiedPeerPid", "from", "server", "name"):
        value = origin.get(field)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
            return json.dumps([kind, "task" if field in {"taskId", "task_id"} else field, str(value).strip()])
    return json.dumps(origin, sort_keys=True)


def _main_activity(value: dict) -> bool:
    if value.get("parent_tool_use_id") or value.get("parentToolUseID"):
        return False
    kind = value.get("type")
    if kind == "assistant":
        return True
    if kind == "stream_event":
        return value.get("event", {}).get("type") == "message_start"
    return (kind == "system" and value.get("subtype") == "status"
            and value.get("status") in {"requesting", "compacting"})


class PendingSteers:
    """Fence accepted inputs against their exact replayed human UUIDs.

    A Result already in flight can precede a just-written input. It ends a
    physical response, but cannot retire the controller's accepted work. The
    service journals these annotations so reattachment has the same boundary.
    """

    def __init__(self):
        self.pending: dict[str, dict] = {}
        self.capabilities: set[str] = set()
        self.background_id: str | None = None
        self.background_origin: str | None = None
        self.background_origin_data: dict | None = None

    def handoff_background(self, identity: str | None) -> None:
        if identity and identity == self.background_id:
            self.background_id = None
            self.background_origin = None
            self.background_origin_data = None

    def add(self, native_id: str, metadata: dict) -> None:
        if len(self.pending) >= 32 or native_id in self.pending:
            raise ClaudeSteerRejected("Claude steering capacity reached")
        self.pending[native_id] = metadata

    def annotate(self, value: dict, *, managed_active: bool = False) -> dict:
        origin = value.get("origin")
        kind = origin.get("kind") if isinstance(origin, dict) else None
        child = value.get("parent_tool_use_id") or value.get("parentToolUseID")
        if not child:
            message = value.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            tool_result = isinstance(content, list) and any(
                isinstance(part, dict) and part.get("type") == "tool_result" for part in content)
            injected = (value.get("type") == "user" and not tool_result
                        and kind not in (None, "human"))
            start = value.get("__cc_background_start")
            # Code can omit replay of an internal task-notification user row.
            # An unsolicited top-level request/output still proves a main turn;
            # task edges and forwarded child output do not.
            if start or injected or (not managed_active and not self.background_id
                                     and _main_activity(value)):
                if start:
                    identity = start["id"]
                    origin_key, origin_data = start.get("origin_key"), start.get("origin")
                else:
                    origin_key = _origin_key(origin) if injected else None
                    origin_data = origin if injected else None
                    identity = self.background_id if injected and self.background_origin is None else None
                    identity = identity or "background-" + hashlib.sha256(json.dumps(
                        [value.get("uuid"), origin_key], sort_keys=True).encode()).hexdigest()[:32]
                self.background_id = identity
                self.background_origin = origin_key
                self.background_origin_data = origin_data
                value = {**value, "__cc_background_start": {
                    "id": identity, "origin_key": self.background_origin,
                    "origin": self.background_origin_data,
                }}
            elif (value.get("type") == "result" and self.background_id
                  and kind != "human"
                  and (kind not in (None, "human") or not managed_active)
                  and (self.background_origin is None
                       or isinstance(origin, dict) and _origin_key(origin) == self.background_origin)):
                value = {**value, "__cc_background_end": self.background_id}
                self.handoff_background(self.background_id)
        cancelled = value.get("__cc_steer_cancelled")
        if cancelled:
            self.pending = {uid: data for uid, data in self.pending.items()
                            if data.get("id") != cancelled.get("id")}
        if value.get("type") == "system" and value.get("subtype") == "init":
            self.capabilities = set(value.get("capabilities") or [])
        if value.get("type") == "command_lifecycle" and value.get("state") == "cancelled":
            metadata = self.pending.pop(value.get("command_uuid"), None)
            if metadata is not None:
                return {**value, "type": "system", "subtype": "cc_remote_steer_cancelled",
                        "__cc_steer_cancelled": metadata}
        if kind not in (None, "human") or child:
            return value
        if value.get("type") == "user":
            metadata = self.pending.pop(value.get("uuid"), None)
            if metadata is not None:
                return {**value, "__cc_steer": value.get("__cc_steer", metadata)}
        elif value.get("type") == "result" and self.pending:
            return {**value, "__cc_steer_intermediate": True}
        return value

    async def interrupt(self, client) -> None:
        if self.pending and "interrupt_cancel_queued_v1" in self.capabilities:
            # Ordinary interrupt leaves async inputs queued. Native cancellation
            # emits exact lifecycle frames before Result, so both controller and
            # journal can retire only the cancelled inputs without guessing.
            await client._query._send_control_request(
                {"subtype": "interrupt", "cancel_queued": True})
        else:
            await client.interrupt()
