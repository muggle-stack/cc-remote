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

from cc_remote.protocol import ReplayStart, ReplayEnd, Snapshot, StateEvent


class RingBuffer:
    def __init__(self, max_events: int, max_bytes: int):
        self.max_events = max_events
        self.max_bytes = max_bytes
        self._buf: deque[tuple[int, object]] = deque()
        self._bytes = 0
        self._logical_tail_seq = 0
        self._dropped_through_seq = 0

    @staticmethod
    def _size(msg) -> int:
        return len(msg.model_dump_json().encode())  # type: ignore[attr-defined]

    def append(self, msg) -> None:
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
            frames.append(ReplayEnd(to_seq=tail, truncated=truncated))
            return frames

        if last_seq is None:
            # First hello: send only a snapshot (cc_session_id + state + cwd).
            # Authoritative transcript history is fetched separately via GetHistory.
            return [Snapshot(cc_session_id=cc_session_id, state=state,
                             tail_text=tail_text, cwd=cwd,
                             generation=generation)]

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
            frames.append(ReplayEnd(to_seq=to_seq, truncated=truncated))
            return frames

        have = [(s, m) for s, m in self._buf if s > last_seq]
        # truncated if the requested last_seq+1 fell off the front of the buffer
        truncated = ((self._buf and (last_seq + 1) < self.head_seq)
                     or self._has_gap_after(last_seq))

        if not have:
            to_seq = max(last_seq, self.tail_seq)
            return [ReplayStart(from_seq=last_seq + 1, to_seq=to_seq,
                                truncated=truncated, generation=generation),
                    ReplayEnd(to_seq=to_seq, truncated=truncated)]

        from_seq = have[0][0]
        to_seq = self.tail_seq
        frames = [ReplayStart(from_seq=from_seq, to_seq=to_seq,
                              truncated=truncated, generation=generation)]
        for _, m in have:
            frames.append(m)
        frames.append(ReplayEnd(to_seq=to_seq, truncated=truncated))
        return frames

    def current_turn_replay(
        self, *, generation: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> list:
        """Bounded replay of only the latest in-flight turn for a fresh client."""
        start = next(
            (index for index in range(len(self._buf) - 1, -1, -1)
             if getattr(self._buf[index][1], "type", None) == "user_msg"
             and (message_id is None
                  or getattr(self._buf[index][1], "msg_id", None) == message_id)),
            None,
        )
        if start is None:
            if message_id is not None:
                # This turn is still in preflight and has not emitted its user
                # marker. Never replay the previous turn as if it were current.
                return []
            if not self._logical_tail_seq:
                return []
            return [
                ReplayStart(
                    from_seq=self.head_seq, to_seq=self.tail_seq,
                    truncated=True, generation=generation),
                ReplayEnd(to_seq=self.tail_seq, truncated=True),
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
        frames.append(ReplayEnd(to_seq=self.tail_seq, truncated=truncated))
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
