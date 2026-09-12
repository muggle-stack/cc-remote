"""Bounded, snapshot-exact read-ahead for official older summary pages.

No synthetic cursors, translated events, control state or native full turns are
cached here. A cache miss is the original read; speculative failures are silent
and are retried normally only when the user actually requests that page.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any, Awaitable, Callable

from cc_remote.wrapper.history_store import HistorySourceFingerprint

_MAX_PAGES = 16
_MAX_BYTES = 8 * 1024 * 1024
_MIN_PREFETCH_SOURCE_BYTES = 32 * 1024 * 1024
_Key = tuple[str, str, int]


@dataclass(frozen=True)
class _Page:
    source: HistorySourceFingerprint
    response: Any
    size: int


class CodexHistoryPrefetch:
    def __init__(
        self,
        fetch: Callable[[str, str | None, int], Awaitable[Any]],
        cacheable: Callable[[Any, int], bool],
    ) -> None:
        self._fetch = fetch
        self._cacheable = cacheable
        self._pages: OrderedDict[_Key, _Page] = OrderedDict()
        self._bytes = 0
        self._tasks: dict[_Key, tuple[HistorySourceFingerprint, asyncio.Task]] = {}
        self._epoch = 0
        self._closed = False

    def _cached(self, key: _Key, source: HistorySourceFingerprint) -> _Page | None:
        page = self._pages.get(key)
        if page is None:
            return None
        if page.source != source:
            self._drop(key)
            return None
        self._pages.move_to_end(key)
        return page

    def _drop(self, key: _Key) -> None:
        page = self._pages.pop(key, None)
        if page is not None:
            self._bytes -= page.size

    async def _read(
        self, key: _Key, source: HistorySourceFingerprint | None,
    ) -> Any:
        epoch = self._epoch
        thread_id, cursor, limit = key
        response = await self._fetch(thread_id, cursor, limit)
        if source is None or self._closed or not self._cacheable(response, limit):
            return response
        try:
            after = await asyncio.to_thread(HistorySourceFingerprint.capture, source.path)
        except OSError:
            return response
        # Even an append invalidates this optimization. Lifecycle/late native
        # items may change despite older turns looking terminal in a summary.
        if source != after or epoch != self._epoch or self._closed:
            return response
        def freeze() -> _Page | None:
            size = len(json.dumps(response, ensure_ascii=False).encode("utf-8"))
            return _Page(source, deepcopy(response), size) if size <= _MAX_BYTES else None

        page = await asyncio.to_thread(freeze)
        if page is None or epoch != self._epoch or self._closed:
            return response
        self._drop(key)
        self._pages[key] = page
        self._bytes += page.size
        while len(self._pages) > _MAX_PAGES or self._bytes > _MAX_BYTES:
            self._drop(next(iter(self._pages)))
        return response

    async def read(
        self, thread_id: str, cursor: str | None, limit: int,
        source: HistorySourceFingerprint | None,
    ) -> Any:
        # Never reuse the moving head or a page without an exact source proof.
        if cursor is None or source is None:
            return await self._fetch(thread_id, cursor, limit)
        key = (thread_id, cursor, limit)
        cached = self._cached(key, source)
        if cached is not None:
            # Projection adds aliases/images. Copy off-loop so even a large
            # cached answer cannot stall live chat while its page is restored.
            return await asyncio.to_thread(deepcopy, cached.response)
        pending = self._tasks.get(key)
        if pending is not None and pending[0] == source:
            # Browser disconnects do not cancel another requester's read-ahead.
            try:
                await asyncio.shield(pending[1])
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                # Invalidation can cancel only the speculative task. The
                # foreground request still gets its ordinary fresh read.
            cached = self._cached(key, source)
            if cached is not None:
                return await asyncio.to_thread(deepcopy, cached.response)
        return await self._read(key, source)

    def prefetch(
        self, thread_id: str, cursor: str | None, limit: int,
        source: HistorySourceFingerprint | None,
    ) -> None:
        if (self._closed or cursor is None or source is None
                or source.size < _MIN_PREFETCH_SOURCE_BYTES):
            return
        key = (thread_id, cursor, limit)
        if self._tasks or self._cached(key, source) is not None:
            return

        async def run() -> None:
            try:
                # Do not start an expensive speculative read for an already
                # obsolete snapshot (common while the current turn streams).
                current = await asyncio.to_thread(HistorySourceFingerprint.capture, source.path)
                if current == source:
                    await self._read(key, source)
            except Exception:
                # No error is displayed/cached by speculation. Foreground reads
                # retain the normal official error and compatibility behavior.
                pass

        task = asyncio.create_task(run())
        self._tasks[key] = (source, task)

        def forget(done: asyncio.Task) -> None:
            if self._tasks.get(key) == (source, done):
                self._tasks.pop(key, None)

        task.add_done_callback(forget)

    def invalidate(self, thread_id: str) -> None:
        self._epoch += 1
        for key in tuple(self._pages):
            if key[0] == thread_id:
                self._drop(key)
        for key, (_source, task) in tuple(self._tasks.items()):
            if key[0] == thread_id:
                task.cancel()

    async def close(self) -> None:
        self._closed = True
        tasks = [task for _source, task in self._tasks.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._pages.clear()
        self._bytes = 0
