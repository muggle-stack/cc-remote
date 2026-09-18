"""Monotonic-seq ring buffer for client reconnect replay.

Every downstream event the wrapper emits is appended with its seq. On a client
`hello(cursors={sid: last_seq})`, `replay_from` returns the events with
seq > last_seq wrapped in replay_start/replay_end. A missing cursor produces a
lightweight snapshot; a cursor older than the retained head marks replay as
truncated.
"""
from __future__ import annotations

from collections import deque
from typing import Optional

from cc_remote.protocol import Delta, ReplayStart, ReplayEnd, Snapshot, StateEvent, TurnUsage


_CURRENT_TURN_DELTA_CHUNK_CHARS = 64 * 1024


class RingBuffer:
    def __init__(self, max_events: int, max_bytes: int):
        self.max_events = max_events
        self.max_bytes = max_bytes
        self._buf: deque[tuple[int, object]] = deque()
        self._bytes = 0
        self._logical_tail_seq = 0
        self._dropped_through_seq = 0
        self._turn_usage: dict[str, TurnUsage] = {}

    @staticmethod
    def _size(msg) -> int:
        return len(msg.model_dump_json().encode())  # type: ignore[attr-defined]

    def append(self, msg) -> None:
        if isinstance(msg, TurnUsage):
            self._turn_usage.pop(msg.turn_id, None)
            self._turn_usage[msg.turn_id] = msg
            while len(self._turn_usage) > 8:
                del self._turn_usage[next(iter(self._turn_usage))]
        size = self._size(msg)
        seq = msg.seq  # type: ignore[attr-defined]
        self._logical_tail_seq = max(self._logical_tail_seq, seq)
        if size > self.max_bytes:
            # Never retain one event larger than the entire ring budget. History
            # remains available through the transcript path; keeping it here
            # would defeat the byte bound and later create an oversized replay.
            self._dropped_through_seq = max(self._dropped_through_seq, seq)
            return
        self._buf.append((seq, msg))
        self._bytes += size
        while (len(self._buf) > self.max_events or self._bytes > self.max_bytes) and len(self._buf) > 1:
            dropped_seq, old = self._buf.popleft()
            self._dropped_through_seq = max(
                self._dropped_through_seq, dropped_seq)
            self._bytes -= self._size(old)

    @property
    def head_seq(self) -> int:
        return self._buf[0][0] if self._buf else 0

    def latest_turn_usage(self) -> list[TurnUsage]:
        return list(self._turn_usage.values())

    @property
    def tail_seq(self) -> int:
        return self._logical_tail_seq

    def _has_gap_after(self, last_seq: int) -> bool:
        return last_seq < self._dropped_through_seq <= self._logical_tail_seq

    def replay_from(self, last_seq: Optional[int], *, cc_session_id, state,
                    tail_text: str = "", cwd: Optional[str] = None,
                    rebuild: bool = False, generation: Optional[str] = None) -> list:
        # rebuild=True: caller knows the client's runtime for this session is
        # stale (e.g. it was evicted from the pool and re-spawned with a fresh
        # ring, seq reset to 0). Emit the WHOLE buffer wrapped in a rebuild
        # envelope so the client discards its old turns and rebuilds — never
        # merges (which would duplicate). No cursor math; unconditional.
        if rebuild:
            head = self.head_seq
            tail = self.tail_seq
            truncated = self._dropped_through_seq > 0
            frames: list = [ReplayStart(from_seq=head, to_seq=tail,
                                        truncated=truncated, rebuild=True,
                                        generation=generation)]
            frames.extend(m for _, m in self._buf)
            frames.append(ReplayEnd(turn_usage=self.latest_turn_usage(), to_seq=tail, truncated=truncated))
            return frames

        if last_seq is None:
            # First hello: send only a snapshot (cc_session_id + state + cwd).
            # Authoritative transcript history is fetched separately via GetHistory.
            return [Snapshot(cc_session_id=cc_session_id, state=state,
                             tail_text=tail_text, cwd=cwd,
                             generation=generation, turn_usage=self.latest_turn_usage())]

        # Future cursor: the client's last_seq is beyond our buffer's tail. This
        # happens because the seq counter resets to 0 on every wrapper restart,
        # but the client's IndexedDB cache keeps the lastSeq from the previous
        # wrapper lifetime. Rebuild the client from the full buffer with
        # rebuild=True (NOT truncated — the buffer has the full history, nothing
        # is lost, so no "history may be missing" banner). No Snapshot here —
        # only hello(null) sends one, else the client re-hellos with the stale
        # cursor and loops.
        if last_seq > self.tail_seq:
            have = list(self._buf)
            from_seq = have[0][0] if have else 0
            to_seq = self.tail_seq
            truncated = self._dropped_through_seq > 0
            frames: list = [ReplayStart(from_seq=from_seq, to_seq=to_seq,
                                        truncated=truncated, rebuild=True,
                                        generation=generation)]
            frames.extend(m for _, m in have)
            frames.append(ReplayEnd(turn_usage=self.latest_turn_usage(), to_seq=to_seq, truncated=truncated))
            return frames

        have = [(s, m) for s, m in self._buf if s > last_seq]
        # truncated if the requested last_seq+1 fell off the front of the buffer
        truncated = ((self._buf and (last_seq + 1) < self.head_seq)
                     or self._has_gap_after(last_seq))

        if not have:
            to_seq = max(last_seq, self.tail_seq)
            return [ReplayStart(from_seq=last_seq + 1, to_seq=to_seq,
                                truncated=truncated, generation=generation),
                    ReplayEnd(turn_usage=self.latest_turn_usage(), to_seq=to_seq, truncated=truncated)]

        from_seq = have[0][0]
        to_seq = self.tail_seq
        frames = [ReplayStart(from_seq=from_seq, to_seq=to_seq,
                              truncated=truncated, generation=generation)]
        for _, m in have:
            frames.append(m)
        frames.append(ReplayEnd(turn_usage=self.latest_turn_usage(), to_seq=to_seq, truncated=truncated))
        return frames

    def replay_from_bounded(
        self,
        last_seq: Optional[int],
        *,
        max_bytes: int,
        max_events: int,
        rebuild: bool = False,
        generation: Optional[str] = None,
    ) -> list:
        """Return a newest contiguous replay suffix under strict queue budgets.

        BTW sessions have no durable History endpoint, but replaying every
        resident side-chat ring during Hello can overflow the relay's bounded
        client queue. The catalog is restored separately and the visible chat
        calls this method on demand. A dropped prefix is explicit via
        ``truncated``; the cursor still advances to the logical tail.
        """
        byte_budget = max(1024, max_bytes)
        event_budget = max(1, max_events)
        cursor = 0 if last_seq is None else last_seq
        effective_rebuild = rebuild or cursor > self.tail_seq
        if effective_rebuild:
            retained = list(self._buf)
            truncated = self._dropped_through_seq > 0
        else:
            retained = [(seq, message) for seq, message in self._buf
                        if seq > cursor]
            truncated = (
                (bool(self._buf) and cursor + 1 < self.head_seq)
                or self._has_gap_after(cursor)
            )
        compacted = self._compact_current_turn_suffix(retained)
        selected_reversed: list[object] = []
        used = 0
        for message in reversed(compacted):
            size = self._size(message)
            if (len(selected_reversed) >= event_budget
                    or used + size > byte_budget):
                truncated = True
                break
            selected_reversed.append(message)
            used += size
        selected = list(reversed(selected_reversed))
        if len(selected) < len(compacted):
            truncated = True
        from_seq = (
            int(getattr(selected[0], "seq", 0) or 0)
            if selected else self.tail_seq + 1
        )
        frames: list = [ReplayStart(
            from_seq=from_seq,
            to_seq=self.tail_seq,
            truncated=truncated,
            rebuild=effective_rebuild,
            generation=generation,
        )]
        frames.extend(selected)
        frames.append(ReplayEnd(
            turn_usage=self.latest_turn_usage(),
            to_seq=self.tail_seq,
            truncated=truncated,
        ))
        return frames

    @staticmethod
    def _delta_replay_key(message: Delta) -> tuple:
        """Fields which must agree before adjacent deltas may be coalesced."""
        return (
            message.message_id,
            message.turn_id,
            message.background,
            message.channel,
            message.sid,
            message.to,
            message.route_id,
        )

    @classmethod
    def _compact_current_turn_suffix(
        cls,
        retained: list[tuple[int, object]],
    ) -> list:
        """Compact adjacent compatible deltas without changing their order.

        A fresh client needs the retained live suffix because canonical History
        can lag partial stream events. Sending thousands of one-token frames is
        unnecessary, though. Each compacted frame keeps the last source seq it
        covers, so the reconnect cursor still advances across the exact source
        range. Individual source deltas are never split (which would create two
        frames with the same seq and make the latter look stale).
        """
        compacted: list = []
        run: list[tuple[int, Delta]] = []
        run_chars = 0
        run_key: tuple | None = None

        def flush() -> None:
            nonlocal run, run_chars, run_key
            if not run:
                return
            first = run[0][1]
            compacted.append(first.model_copy(
                deep=True,
                update={
                    "text": "".join(message.text for _, message in run),
                    "seq": run[-1][0],
                },
            ))
            run = []
            run_chars = 0
            run_key = None

        for seq, message in retained:
            if not isinstance(message, Delta):
                flush()
                compacted.append(message)
                continue
            key = cls._delta_replay_key(message)
            next_chars = len(message.text)
            if run and (
                key != run_key
                or run_chars + next_chars > _CURRENT_TURN_DELTA_CHUNK_CHARS
            ):
                flush()
            run.append((seq, message))
            run_chars += next_chars
            run_key = key
        flush()
        return compacted

    def current_turn_replay(
        self, *, generation: Optional[str] = None,
        message_id: Optional[str] = None,
        boundary_seq: Optional[int] = None,
    ) -> list:
        """Bounded replay of only the latest in-flight turn for a fresh client."""
        start = next(
            (index for index in range(len(self._buf) - 1, -1, -1)
             if getattr(self._buf[index][1], "type", None) in {
                 "user_msg", "turn_steered",
             }
             and (message_id is None
                  or getattr(self._buf[index][1], "msg_id", None) == message_id)),
            None,
        )
        if start is None:
            if message_id is not None:
                # This turn is still in preflight and has not emitted its user
                # marker, unless the resident exact owner proves that the turn
                # already crossed its binding boundary. The user marker can be
                # evicted one frame before that binding, so requiring the
                # binding itself to have fallen out would leave a narrow silent
                # gap. Return an explicit empty truncated envelope whenever the
                # exact binding exists but its marker does not, so clients fetch
                # canonical current-turn history.
                if (
                    boundary_seq is not None
                    and boundary_seq > 0
                ):
                    # The exact resident binding proves every retained frame at
                    # or after its sequence belongs to this active turn. Keep
                    # that suffix: canonical History may not yet contain the
                    # newest partial delta. The truncated envelope still tells
                    # Web to fetch the missing canonical prefix.
                    retained = [
                        (seq, message) for seq, message in self._buf
                        if seq >= boundary_seq
                    ]
                    from_seq = (
                        retained[0][0] if retained else self.head_seq
                        or min(self.tail_seq, boundary_seq + 1)
                    )
                    frames = [
                        ReplayStart(
                            from_seq=from_seq,
                            to_seq=self.tail_seq,
                            truncated=True,
                            generation=generation,
                        ),
                    ]
                    frames.extend(self._compact_current_turn_suffix(retained))
                    frames.append(ReplayEnd(
                        turn_usage=self.latest_turn_usage(),
                        to_seq=self.tail_seq,
                        truncated=True,
                    ))
                    return frames
                # Never replay the previous turn as if it were current.
                return []
            if not self._logical_tail_seq:
                return []
            return [
                ReplayStart(
                    from_seq=self.head_seq, to_seq=self.tail_seq,
                    truncated=True, generation=generation),
                ReplayEnd(turn_usage=self.latest_turn_usage(), to_seq=self.tail_seq, truncated=True),
            ]
        have = list(self._buf)[start:]
        if not have:
            return []
        truncated = self._dropped_through_seq >= have[0][0]
        frames: list = [ReplayStart(
            from_seq=have[0][0], to_seq=self.tail_seq, truncated=truncated,
            generation=generation,
        )]
        frames.extend(message for _, message in have)
        frames.append(ReplayEnd(turn_usage=self.latest_turn_usage(), to_seq=self.tail_seq, truncated=truncated))
        return frames

    def latest_state(self):
        for _, m in reversed(self._buf):
            if isinstance(m, StateEvent):
                return m.state
        return None

    def latest_tail_text(self) -> str:
        parts: list[str] = []
        total = 0
        for _, m in reversed(self._buf):
            if m.type == "delta":  # type: ignore[attr-defined]
                parts.append(m.text)  # type: ignore[attr-defined]
                total += len(m.text)  # type: ignore[attr-defined]
                if total > 500:
                    break
        return "".join(reversed(parts))[-500:]
