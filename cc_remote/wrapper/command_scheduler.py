"""Bounded command intake with ordered mutations and independent sessions.

Handlers still own session locks, reliable receipts and native turn lifetimes.
This scheduler only orders command *handlers*, never the turns they launch.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

from cc_remote.log import logger
from cc_remote.protocol import serialize

log = logger("cc_remote.wrapper.command_scheduler")


@dataclass
class _Pending:
    task: asyncio.Task
    kind: str
    target: str | None
    serial: bool
    barrier: bool
    size: int


class CommandScheduler:
    def __init__(
        self,
        process: Callable[[object], Awaitable[None]],
        resolve_target: Callable[[str], str],
        *,
        max_items: int = 128,
        max_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        self._process = process
        self._resolve_target = resolve_target
        self._max_items = max_items
        self._max_bytes = max_bytes
        self._pending: dict[tuple[str, str], _Pending] = {}
        self._bytes = 0
        self._changed = asyncio.Event()

    def owns_target(self, target: str) -> bool:
        """Pin a resident context even before its queued handler takes a lock."""
        target = self._resolve_target(target)
        return any(
            not pending.task.done() and pending.target is not None
            and self._resolve_target(pending.target) == target
            for pending in self._pending.values()
        )

    async def submit(
        self, command, *, target: str | None = None,
        serial: bool = True, barrier: bool = False, urgent: bool = False,
    ) -> None:
        received = asyncio.get_running_loop().time()
        fields = {
            "type": command.type,
            "sid": target,
            "client_id": getattr(command, "client_id", None),
            "cmd_id": getattr(command, "cmd_id", None),
            "msg_id": getattr(command, "msg_id", None),
        }
        # Deliberately omit prompts, attachments, SDK stderr and credentials.
        log.info("command received", **fields)
        key = (
            getattr(command, "client_id", None) or "",
            getattr(command, "cmd_id", None) or f"untracked-{id(command)}",
        )
        # Include decoded attachments in the budget instead of counting tasks
        # alone. A blocked SDK must not turn the transport's bounded inbox into
        # an unbounded collection of tasks retaining multi-megabyte prompts.
        size = len(serialize(command).encode("utf-8"))
        while True:
            current = self._pending.get(key)
            if current is not None and not current.task.done():
                return
            # One oversize item can run alone (the transport already bounds a
            # single frame). Keep ordinary overload as backpressure, not drops.
            if (len(self._pending) < self._max_items
                    and (self._bytes + size <= self._max_bytes
                         or not self._pending)):
                break
            self._changed.clear()
            await self._changed.wait()

        dependencies = []
        for pending in self._pending.values():
            if pending.task.done() or urgent:
                continue
            same_target = (
                target is not None and pending.target is not None
                and self._resolve_target(target)
                == self._resolve_target(pending.target)
            )
            # Stop may pass an explicitly addressed metadata read, but not the
            # preceding resume/query/mutation that makes its target runnable.
            # Keep the read alive so it still returns its report and receipt.
            if (same_target and command.type == "interrupt"
                    and pending.kind == "get_context"):
                continue
            if ((serial and pending.serial) or barrier or pending.barrier
                    or same_target):
                dependencies.append(pending.task)

        async def run() -> None:
            for dependency in dependencies:
                # Cancelling this command must not cancel an earlier handler
                # which another client/session is still waiting for.
                await asyncio.shield(dependency)
            started = asyncio.get_running_loop().time()
            log.info("command dispatch", **fields,
                     queued_ms=round((started - received) * 1000))
            try:
                await self._process(command)
            finally:
                log.info("command handler finished", **fields,
                         elapsed_ms=round((asyncio.get_running_loop().time()
                                           - started) * 1000))

        task = asyncio.create_task(run())
        pending = _Pending(task, command.type, target, serial, barrier, size)
        # A completed task's callback may not have run yet. Retire its budget
        # before replacing it with a reliable replay of the same command id.
        if current is not None:
            self._bytes -= current.size
        self._pending[key] = pending
        self._bytes += size

        def finished(_task: asyncio.Task) -> None:
            if self._pending.get(key) is pending:
                self._pending.pop(key)
                self._bytes -= size
            self._changed.set()

        task.add_done_callback(finished)

    async def drain(self) -> None:
        """A finite input source ends after its admitted commands complete."""
        while self._pending:
            # gather() of already-finished tasks can return synchronously on
            # Python 3.13, starving the callbacks that retire their entries.
            self._changed.clear()
            await self._changed.wait()

    async def close(self) -> None:
        tasks = [pending.task for pending in self._pending.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._pending.clear()
        self._bytes = 0
        self._changed.set()
