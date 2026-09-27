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
# Let the WebSocket backend finish its handshake/TCP close deadlines before
# this last-resort guard. Cleanup runs off the forwarding path throughout.
CLIENT_CLOSE_TIMEOUT = 35.0


class SlowClientError(RuntimeError):
    """The client cannot keep up with the relay's bounded send queue."""


class ClientConn:
    def __init__(self, ws, cap: int, client_id: str,
                 byte_cap: int = 16 * 1024 * 1024,
                 owner_id: str | None = None):
        self.ws = ws
        self.cap = max(1, cap)
        self.byte_cap = max(1024, byte_cap)
        self.client_id = client_id
        # A connection id is page-lifetime. BTW ownership is instead bound to
        # the authenticated relay account and can span reloads and tabs.
        self.owner_id = owner_id or client_id
        self.route_id = uuid.uuid4().hex
        self.queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue(maxsize=self.cap)
        self._queued_bytes = 0
        self._sender: Optional[asyncio.Task] = None
        self._stop_task: Optional[asyncio.Task] = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed or bool(self._sender and self._sender.done())

    @property
    def queued_bytes(self) -> int:
        return self._queued_bytes

    def start(self) -> None:
        self._sender = asyncio.create_task(self._run())

    def begin_stop(self, *, code: int | None = None, reason: str = "") -> asyncio.Task:
        """Retire this connection synchronously; finish socket cleanup off-path."""
        if self._stop_task is None:
            self._closed = True
            if self._sender and self._sender is not asyncio.current_task():
                self._sender.cancel()
            self._stop_task = asyncio.create_task(self._finish_stop(code, reason))
        return self._stop_task

    async def stop(self, *, code: int | None = None, reason: str = "") -> None:
        # Cancellation of the reader must not abandon the one close operation.
        await asyncio.shield(self.begin_stop(code=code, reason=reason))

    async def _finish_stop(self, code: int | None, reason: str) -> None:
        if code is not None:
            try:
                async with asyncio.timeout(CLIENT_CLOSE_TIMEOUT):
                    await self.ws.close(code=code, reason=reason)
            except Exception:
                pass
        if self._sender:
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
            log.debug("client sender ended", client_id=self.client_id, error=str(e))
            self.begin_stop(code=1011, reason="client sender failed")
