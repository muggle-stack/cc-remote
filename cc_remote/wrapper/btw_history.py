"""Bounded, process-local presentation snapshots for ephemeral side chats.

Native Codex ephemeral threads cannot read persisted turns. Keep public wire
items, rather than token frames, until the side chat closes. Replay cuts only
between items and always carries the exact human/native owner boundary.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field

from cc_remote.protocol import (
    AssistantMsgEnd, AssistantMsgStart, Delta, Error, ProcessEvent, ReplayEnd,
    ReplayStart, ToolDelta, ToolResult, ToolUse, TurnBinding, TurnEnd,
    TurnDiff, TurnFileChanges, TurnPlan, TurnSteered, UserMsg,
)
from cc_remote.wrapper.ringbuffer import RingBuffer


def item_key(event) -> str | None:
    if isinstance(event, (AssistantMsgStart, Delta, AssistantMsgEnd)):
        return "message:" + event.message_id
    if isinstance(event, (ToolUse, ToolDelta, ToolResult)):
        return "tool:" + event.tool_use_id
    if isinstance(event, (ProcessEvent, TurnPlan, TurnDiff)):
        return "process:" + event.item_id
    if isinstance(event, TurnFileChanges):
        return "changes:" + event.turn_id
    if isinstance(event, Error) and event.msg_id:
        return "error:" + event.msg_id
    return None


@dataclass
class _Item:
    events: OrderedDict[str, object] = field(default_factory=OrderedDict)
    size: int = 0
    sealed: bool = False

    def append(self, event) -> None:
        if ((isinstance(event, Delta) and "assistant_msg_end" in self.events)
                or (isinstance(event, ToolDelta) and "tool_result" in self.events)):
            return
        if (self.sealed and isinstance(event, ProcessEvent)
                and event.phase in {"start", "update"}
                and event.status in {"unknown", "pending", "running"}):
            return
        key = event.type
        if isinstance(event, ToolDelta):
            key += ":" + event.stream
        if isinstance(event, Delta) and event.replace:
            for stored in list(self.events):
                if stored == key or stored.startswith(key + ":"):
                    self.size -= RingBuffer._size(self.events.pop(stored))
        if isinstance(event, (Delta, ToolDelta)):
            key = next((stored for stored in reversed(self.events)
                        if stored == key or stored.startswith(key + ":")), key)
        previous = self.events.get(key)
        if isinstance(event, Delta) and previous is not None and not event.replace:
            # Coalesce small chunks, not the entire message on every token.
            # This bounds copy/JSON sizing work on the live reader path.
            if len(previous.text) + len(event.text) <= 32 * 1024:
                event = event.model_copy(update={
                    "text": previous.text + event.text,
                    "replace": previous.replace,
                    "channel": previous.channel if event.channel == "unknown" else event.channel,
                })
            else:
                key += ":" + str(len(self.events))
                previous = None
        elif isinstance(event, ToolDelta) and previous is not None:
            if len(previous.delta) + len(event.delta) <= 32 * 1024:
                event = event.model_copy(update={"delta": previous.delta + event.delta})
            else:
                key += ":" + str(len(self.events))
                previous = None
        elif isinstance(event, ProcessEvent):
            if previous is not None:
                event = previous.model_copy(update=event.model_dump(exclude_none=True))
            if event.append_to and event.delta:
                field = event.append_to
                limit = {"summary": 65536, "detail": 262144,
                         "output": 2097152, "diff": 2097152, "progress": 65536}[field]
                value = (getattr(event, field) or "") + event.delta
                event = event.model_copy(update={
                    field: value[-limit:], "append_to": None, "delta": None,
                    "truncated": event.truncated or len(value) > limit,
                })
        self.events[key] = event
        self.size += RingBuffer._size(event) - (
            RingBuffer._size(previous) if previous is not None else 0)


@dataclass
class _Turn:
    user: UserMsg | TurnSteered
    binding: TurnBinding | None = None
    items: OrderedDict[str, _Item] = field(default_factory=OrderedDict)
    end: TurnEnd | None = None

    def envelope(self) -> tuple[list, list]:
        return ([self.user, *([self.binding] if self.binding else [])],
                [self.end] if self.end else [])


class BtwHistory:
    def __init__(self, max_bytes: int, max_items: int):
        self.max_bytes = max_bytes
        self.max_items = max_items
        self.turns: OrderedDict[str, _Turn] = OrderedDict()
        self.truncated = False
        self._bytes = 0
        self._items = 0
        self._owners: dict[str, str] = {}
        self._omitted: OrderedDict[str, None] = OrderedDict()

    def observe(self, event) -> None:
        if isinstance(event, (UserMsg, TurnSteered)):
            if event.msg_id not in self.turns:
                self.turns[event.msg_id] = _Turn(event)
                self._bytes += RingBuffer._size(event)
        elif isinstance(event, TurnBinding):
            turn = self.turns.get(event.msg_id)
            if turn is not None:
                self._bytes -= RingBuffer._size(turn.binding) if turn.binding else 0
                turn.binding = event
                self._bytes += RingBuffer._size(event)
        elif self.turns:
            key = item_key(event)
            if key in self._omitted:
                # A later suffix of an evicted item is not a whole message.
                # Only an exact completed snapshot may restore it below.
                return
            owner = self._owners.get(key) if key else None
            parent = getattr(event, "parent_id", None)
            if owner is None and parent:
                owner = next((self._owners[prefix + parent]
                              for prefix in ("tool:", "process:", "message:")
                              if prefix + parent in self._owners), None)
            turn = self.turns.get(owner) if owner else next(reversed(self.turns.values()))
            explicit = event.msg_id if isinstance(event, Error) else getattr(event, "turn_id", None)
            if explicit and owner is None:
                matches = [row for row in self.turns.values()
                           if explicit in {row.user.msg_id,
                                           getattr(row.user, "turn_id", None),
                                           row.binding.turn_id if row.binding else None}]
                turn = matches[-1] if matches else None
            if turn is not None:
                if isinstance(event, TurnEnd):
                    self._bytes -= RingBuffer._size(turn.end) if turn.end else 0
                    turn.end = event
                    self._bytes += RingBuffer._size(event)
                elif key:
                    group = turn.items.setdefault(key, _Item())
                    if key not in self._owners:
                        self._owners[key] = turn.user.msg_id
                        self._items += 1
                    old_size = group.size
                    group.append(event)
                    self._bytes += group.size - old_size
        self._trim()

    def _trim(self) -> None:
        while self.turns and (self._bytes > self.max_bytes
                              or self._items > self.max_items
                              or len(self.turns) > 64):
            turn_id, turn = next(iter(self.turns.items()))
            self.truncated = True
            if len(self.turns) > 1 or not turn.items:
                self.turns.pop(turn_id)
                for event in sum(turn.envelope(), []):
                    self._bytes -= RingBuffer._size(event)
                groups = list(turn.items.items())
            else:
                groups = [turn.items.popitem(last=False)]
            for key, group in groups:
                self._bytes -= group.size
                self._items -= 1
                self._owners.pop(key, None)
                self._omitted[key] = None
            while len(self._omitted) > 10000:
                self._omitted.popitem(last=False)

    def repair(self, native_turn_id: str, snapshots: list[tuple[str | None, list]],
               *, item_order: list[str] | None = None) -> None:
        """Merge complete native items only into a proven visible segment."""
        rows = [row for row in self.turns.values()
                if native_turn_id in {
                    row.binding.turn_id if row.binding else None,
                    getattr(row.user, "turn_id", None)}]
        ordered: dict[str, list[str]] = {}
        original = {owner: list(row.items) for owner, row in self.turns.items()}
        for client_id, events in snapshots:
            if not events:
                continue
            key = item_key(events[0])
            owner = self._owners.get(key)
            turn = self.turns.get(owner or client_id)
            if turn is None and client_id is None and len(rows) == 1:
                turn = rows[0]
            if turn is None or not any(turn is row for row in rows) or not key:
                continue
            group = _Item()
            for event in events:
                group.append(event)
            group.sealed = True
            previous = turn.items.get(key)
            if previous is None:
                self._items += 1
            self._bytes += group.size - (previous.size if previous else 0)
            turn.items[key] = group
            self._owners[key] = turn.user.msg_id
            self._omitted.pop(key, None)
            ordered.setdefault(turn.user.msg_id, []).append(key)
        for owner, keys in ordered.items():
            turn = self.turns[owner]
            if item_order is not None:
                by_native_id = {key.split(":", 1)[1]: key for key in turn.items}
                keys = [by_native_id[native_id] for native_id in item_order
                        if native_id in by_native_id]
            # Insert missing completed items before the next known native item.
            # A bounded cache can omit an older prefix still in the live row;
            # never move that prefix behind the retained cache suffix.
            order = original[owner]
            pending = []
            for key in dict.fromkeys(keys):
                if key in order:
                    at = order.index(key)
                    order[at:at] = pending
                    pending = []
                else:
                    pending.append(key)
            order.extend(pending)
            # A translator may normalize a malformed native id. Its complete
            # public snapshot still belongs here even without an order anchor.
            order.extend(key for key in turn.items if key not in order)
            turn.items = OrderedDict((key, turn.items[key]) for key in order)
        self._trim()

    def replay(self, *, tail_seq: int, generation: str, max_bytes: int,
               max_events: int, turn_usage: list) -> list:
        selected: list = []
        used = 0
        truncated = self.truncated
        for turn in reversed(self.turns.values()):
            opening, closing = turn.envelope()
            envelope = opening + closing
            size = sum(RingBuffer._size(e) for e in envelope)
            if used + size > max_bytes or len(selected) + len(envelope) > max_events:
                truncated = True
                break
            body: list = []
            used += size
            for group in reversed(turn.items.values()):
                events = list(group.events.values())
                if (used + group.size > max_bytes
                        or len(selected) + len(envelope) + len(body) + len(events) > max_events):
                    truncated = True
                    break
                body[0:0] = events
                used += group.size
            selected[0:0] = opening + body + closing
            if len(body) < sum(len(group.events) for group in turn.items.values()):
                break
        # These are complete item snapshots, not a cursor suffix. Sequence is
        # committed once at ReplayEnd; no reconstructed item changes live seq.
        frames = [ReplayStart(from_seq=0, to_seq=tail_seq, truncated=truncated,
                              rebuild=True, generation=generation)]
        frames.extend(e.model_copy(deep=True, update={"seq": None}) for e in selected)
        frames.append(ReplayEnd(to_seq=tail_seq, truncated=truncated, turn_usage=turn_usage))
        return frames


class CodexBtwCompletions:
    """Capture public completion snapshots before a bounded live queue sheds.

    No raw reasoning, model context or disk transcript is retained. At most two
    turns survive, allowing the previous terminal repair to race the next input.
    """
    def __init__(self, max_bytes: int, max_items: int, tool_result_max: int):
        self.max_bytes, self.max_items = max_bytes, max_items
        self.tool_result_max = tool_result_max
        self.turns: OrderedDict[str, OrderedDict] = OrderedDict()
        self.owners: OrderedDict[str, str | None] = OrderedDict()
        self._last_users: dict[str, str] = {}
        self.initial_owner: str | None = None
        self._bytes = 0
        self.truncated: set[str] = set()

    def observe(self, message: dict) -> None:
        from cc_remote.wrapper.codex_stream import CodexStreamTranslator, codex_live_user_message

        params = message.get("params")
        if not isinstance(params, dict):
            return
        turn_id = params.get("turnId")
        item = params.get("item")
        if (message.get("method") not in {"item/started", "item/completed"}
                or not isinstance(turn_id, str) or not turn_id or len(turn_id) > 256
                or not isinstance(item, dict) or not isinstance(item.get("id"), str)
                or not item["id"] or len(item["id"]) > 256):
            return
        user = codex_live_user_message(message)
        if user is not None:
            # An unattributed steer is deliberately ambiguous, never assumed to
            # belong to the last accepted browser input.
            if self._last_users.get(turn_id) != user.message_id:
                self.owners[turn_id] = user.client_id or (
                    self.initial_owner if turn_id not in self._last_users else None)
                self._last_users[turn_id] = user.message_id
            elif user.client_id:
                self.owners[turn_id] = user.client_id
            while len(self.owners) > 64:
                old_id, _ = self.owners.popitem(last=False)
                self._last_users.pop(old_id, None)
            return
        rows = self.turns.setdefault(turn_id, OrderedDict())
        self.owners.setdefault(turn_id, self.initial_owner)
        key = item["id"]
        if key not in rows:
            rows[key] = (self.owners[turn_id], [], 0)
        if message["method"] == "item/completed":
            try:
                events = CodexStreamTranslator(self.tool_result_max).feed(message)
            except (TypeError, ValueError, KeyError):
                # Optional recovery must not break the native stdout reader.
                return
            events = [e for e in events if item_key(e)]
            size = sum(RingBuffer._size(e) for e in events)
            owner, _, previous = rows[key]
            rows[key] = (owner, events, size)
            self._bytes += size - previous
        while self.turns and (len(self.turns) > 2 or self._bytes > self.max_bytes
                              or sum(len(rows) for rows in self.turns.values()) > self.max_items):
            first_id, first = next(iter(self.turns.items()))
            if len(self.turns) > 1 or not first:
                self.turns.pop(first_id)
                self.owners.pop(first_id, None)
                self._last_users.pop(first_id, None)
                self.truncated.discard(first_id)
                self._bytes -= sum(entry[2] for entry in first.values())
            else:
                self.truncated.add(first_id)
                self._bytes -= first.popitem(last=False)[1][2]

    def snapshots(self, turn_id: str) -> list[tuple[str | None, list]]:
        return [(owner, events) for owner, events, _ in self.turns.get(turn_id, {}).values()]

    def item_order(self, turn_id: str) -> list[str]:
        return list(self.turns.get(turn_id, {}))
