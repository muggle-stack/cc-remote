"""Bounded per-client forwarding.

A slow or half-dead browser must never block the wrapper or grow relay memory
without bound.  Frames are serialized before enqueueing so both the item count
and the queued byte count are hard limits.  When either limit is exceeded the
whole connection is dropped: selectively discarding deltas would silently
corrupt the answer and leave the client believing it has a complete turn.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Optional

from cc_remote.log import logger
from cc_remote.protocol import serialize

log = logger("cc_remote.relay.forward")


class SlowClientError(RuntimeError):
    """The client cannot keep up with the relay's bounded send queue."""


class ClientConn:
    def __init__(self, ws, cap: int, client_id: str, byte_cap: int = 16 * 1024 * 1024):
        self.ws = ws
        self.cap = max(1, cap)
        self.byte_cap = max(1024, byte_cap)
        self.client_id = client_id
        self.route_id = uuid.uuid4().hex
        self.queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue(maxsize=self.cap)
        self._queued_bytes = 0
        self._sender: Optional[asyncio.Task] = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed or bool(self._sender and self._sender.done())

    @property
    def queued_bytes(self) -> int:
        return self._queued_bytes

    def start(self) -> None:
        self._sender = asyncio.create_task(self._run())

    async def stop(self, *, code: int | None = None, reason: str = "") -> None:
        already_closed = self._closed
        self._closed = True
        if code is not None and not already_closed:
            try:
                await self.ws.close(code=code, reason=reason)
            except Exception:
                pass
        if self._sender and self._sender is not asyncio.current_task():
            self._sender.cancel()
            try:
                await self._sender
            except (asyncio.CancelledError, Exception):
                pass

    async def send(self, msg) -> None:
        """Queue one complete frame or fail the whole slow connection."""
        if self.closed:
            raise ConnectionError("client sender is closed")
        raw = serialize(msg)
        size = len(raw.encode("utf-8"))
        if self.queue.full() or self._queued_bytes + size > self.byte_cap:
            raise SlowClientError(
                f"client queue limit exceeded: items={self.queue.qsize()}/{self.cap} "
                f"bytes={self._queued_bytes + size}/{self.byte_cap}"
            )
        self.queue.put_nowait((raw, size))
        self._queued_bytes += size

    async def _run(self) -> None:
        try:
            while True:
                raw, size = await self.queue.get()
                self._queued_bytes = max(0, self._queued_bytes - size)
                await self.ws.send_text(raw)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._closed = True
            log.debug("client sender ended", client_id=self.client_id, error=str(e))
            try:
                await self.ws.close(code=1011, reason="client sender failed")
            except Exception:
                pass
