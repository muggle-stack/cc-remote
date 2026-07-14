"""WebSocket client to the relay. Outbound-only (no inbound ports on the
company machine). Auto-reconnects with backoff; on each (re)connect it calls
`on_connected` so the wrapper can re-announce itself (hello).

Live sends are best-effort: while disconnected, `send` drops frames (the ring
buffer + client replay is the source of truth, so nothing is lost). The send
queue is drained on disconnect to avoid double-delivery on reconnect.
"""
from __future__ import annotations

import asyncio
from collections import deque
from typing import AsyncIterator, Awaitable, Callable, Optional
from urllib.parse import urlsplit

from websockets.asyncio.client import connect

from cc_remote.log import logger
from cc_remote.protocol import ProtocolError, deserialize, serialize

log = logger("cc_remote.wrapper.transport")


class _ByteQueue:
    """FIFO with simultaneous item and serialized-byte backpressure."""

    def __init__(self, max_items: int, max_bytes: int):
        self.maxsize = max(1, max_items)
        self.max_bytes = max(1024, max_bytes)
        self._items: deque[tuple[object, int]] = deque()
        self._bytes = 0
        self._closed = False
        self._condition = asyncio.Condition()

    @property
    def byte_size(self) -> int:
        return self._bytes

    def qsize(self) -> int:
        return len(self._items)

    async def put(self, item: object, size: int) -> None:
        if size < 0 or size > self.max_bytes:
            raise ValueError(
                f"queue item size {size} exceeds byte cap {self.max_bytes}"
            )
        async with self._condition:
            await self._condition.wait_for(lambda: (
                self._closed
                or (len(self._items) < self.maxsize
                    and self._bytes + size <= self.max_bytes)
            ))
            if self._closed:
                raise RuntimeError("queue is closed")
            self._items.append((item, size))
            self._bytes += size
            self._condition.notify_all()

    async def get(self):
        async with self._condition:
            await self._condition.wait_for(lambda: self._closed or self._items)
            if not self._items:
                return None
            item, size = self._items.popleft()
            self._bytes = max(0, self._bytes - size)
            self._condition.notify_all()
            return item

    async def drain(self) -> int:
        async with self._condition:
            count = len(self._items)
            self._items.clear()
            self._bytes = 0
            self._condition.notify_all()
            return count

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._items.clear()
            self._bytes = 0
            self._condition.notify_all()


class WrapperTransport:
    def __init__(self, url: str, token: str, *, inbox_cap: int = 1024,
                 send_cap: int = 8192, max_size: int = 16 * 1024 * 1024,
                 inbox_bytes: int = 32 * 1024 * 1024,
                 send_bytes: int = 32 * 1024 * 1024):
        self.url = url
        self.token = token
        self.max_size = max(1024, max_size)
        self.on_connected: Optional[Callable[[], Awaitable[None]]] = None
        self._inbox = _ByteQueue(inbox_cap, max(inbox_bytes, self.max_size))
        self._send_q = _ByteQueue(send_cap, max(send_bytes, self.max_size))
        self._connected = False
        self._generation = 0
        self._stop = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop = True
        # Wake incoming(), sender(), and any producer blocked on byte pressure.
        await self._inbox.close()
        await self._send_q.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def send(self, msg) -> None:
        if not self._connected:
            return  # dropped; ring buffer + client replay covers it
        raw = serialize(msg)
        generation = self._generation
        try:
            await self._send_q.put(
                (generation, raw), len(raw.encode("utf-8")))
        except (RuntimeError, ValueError) as exc:
            if not self._stop:
                log.warning("outbound frame rejected", error=str(exc))

    async def incoming(self) -> AsyncIterator:
        while True:
            item = await self._inbox.get()
            if item is None:
                break
            yield item

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop:
            try:
                headers = {"Authorization": f"Bearer {self.token}"}
                kw: dict = {
                    "additional_headers": headers,
                    "max_size": self.max_size,
                    # websockets 16 defaults proxy=True, including NO_PROXY /
                    # proxy_bypass handling. Do not force a configured proxy for
                    # loopback relay URLs: that can expose the Bearer token.
                    "max_queue": 4,
                }
                if urlsplit(self.url).hostname in {"127.0.0.1", "::1", "localhost"}:
                    kw["proxy"] = None
                async with connect(self.url, **kw) as ws:
                    log.info("connected to relay", url=self.url)
                    backoff = 1.0
                    self._generation += 1
                    self._connected = True
                    if self.on_connected:
                        try:
                            await self.on_connected()
                        except Exception:
                            log.exception("on_connected callback failed")
                    sender = asyncio.create_task(
                        self._sender(ws, self._generation))
                    receiver = asyncio.create_task(self._receiver(ws))
                    try:
                        _, pending = await asyncio.wait(
                            {sender, receiver}, return_when=asyncio.FIRST_COMPLETED,
                        )
                        for task in pending:
                            task.cancel()
                        # Observe a completed task's error so the reconnect log
                        # captures the real failure instead of emitting an
                        # unhandled-task warning later.
                        results = await asyncio.gather(
                            sender, receiver, return_exceptions=True)
                        for result in results:
                            if (isinstance(result, BaseException)
                                    and not isinstance(
                                        result, asyncio.CancelledError)):
                                raise result
                    finally:
                        self._connected = False
                        self._generation += 1
                        # Parent cancellation (WrapperTransport.stop) can land
                        # while wait() is pending. Child tasks are not cancelled
                        # automatically, so reap them explicitly before leaving
                        # this socket generation.
                        for task in (sender, receiver):
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(
                            sender, receiver, return_exceptions=True)
            except Exception as e:
                self._connected = False
                log.warning("relay connection lost, reconnecting", error=str(e), backoff=backoff)
            await self._drain_send_queue()
            if not self._stop:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _drain_send_queue(self) -> None:
        dropped = await self._send_q.drain()
        if dropped:
            log.debug("drained send queue on disconnect", dropped=dropped)

    async def _sender(self, ws, generation: int) -> None:
        while True:
            item = await self._send_q.get()
            if item is None:
                return
            item_generation, raw = item
            if item_generation != generation or generation != self._generation:
                continue
            await ws.send(raw)

    async def _receiver(self, ws) -> None:
        async for raw in ws:
            try:
                cmd = deserialize(raw)
            except ProtocolError as e:
                log.warning("dropping malformed frame from relay", error=str(e))
                continue
            try:
                await self._inbox.put(cmd, len(raw.encode("utf-8")))
            except (RuntimeError, ValueError) as exc:
                if not self._stop:
                    log.warning("inbound frame rejected", error=str(exc))
                return
