"""Bounded one-shot requests to the Codex app-server control plane."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import sys
from typing import Any, Optional

from cc_remote import __version__
from cc_remote.wrapper.history_metrics import history_timed
from cc_remote.wrapper.codex_runtime import (
    codex_env as _codex_env,
    resolve_codex_bin as _resolve_codex_bin,
)


_RPC_TIMEOUT = 30.0
_STREAM_LIMIT = 16 * 1024 * 1024
_NOFILE_SOFT_MAX = 65_536
_RLIMIT_EXEC = str(Path(__file__).with_name("rlimit_exec.py"))


def _child_env(bin_path: str, codex_home: Optional[str]) -> dict[str, str]:
    # Preserve the one-argument compatibility seam used by existing tests and
    # embedders when the legacy/default profile is selected.
    if codex_home is None:
        return _codex_env(bin_path)
    return _codex_env(bin_path, codex_home)


class CodexRpcRejected(RuntimeError):
    """The app-server returned an explicit JSON-RPC rejection."""

    def __init__(self, message: str, *, code: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message


class CodexRpcOutcomeUnknown(RuntimeError):
    """The mutating request may have committed before transport failure."""


class CodexRpcResponseTooLarge(CodexRpcOutcomeUnknown):
    """One JSON-RPC response exceeded the bounded stdio frame.

    The subtype lets bounded read-only consumers choose another source. It must
    remain an outcome-unknown failure for mutating callers because the app-server
    may have committed before producing the oversized response.
    """


def _rpc_error(error: Any) -> CodexRpcRejected:
    if not isinstance(error, dict):
        return CodexRpcRejected("codex app-server request failed")
    code = error.get("code")
    message = str(error.get("message") or "request failed")[:512]
    if isinstance(code, int):
        return CodexRpcRejected(
            f"codex app-server error {code}: {message}",
            code=code,
        )
    return CodexRpcRejected(f"codex app-server error: {message}")


async def _stop_process(proc: asyncio.subprocess.Process) -> None:
    if proc.stdin is not None:
        proc.stdin.close()
    if proc.returncode is not None:
        await proc.wait()
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=3.0)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


@history_timed("codex_rpc")
async def codex_rpc(
    method: str, params: Optional[dict[str, Any]], cwd: Optional[str] = None,
    codex_home: Optional[str] = None,
    *,
    nofile_soft_limit: Optional[int] = None,
) -> Any:
    """Issue a control request, optionally borrowing a scoped history reader.

    This is for thread/account control-plane calls that do not need a resident
    ``CodexHandle``. Notifications emitted before the matching response are
    intentionally ignored; the response identified by its JSON-RPC id is the
    authoritative result.
    """
    if not isinstance(method, str) or not method:
        raise ValueError("codex RPC method must be a non-empty string")
    if params is not None and not isinstance(params, dict):
        raise TypeError("codex RPC params must be a dict or None")
    if nofile_soft_limit is not None and (
        not isinstance(nofile_soft_limit, int)
        or isinstance(nofile_soft_limit, bool)
        or nofile_soft_limit < 1
        or nofile_soft_limit > _NOFILE_SOFT_MAX
        or os.name != "posix"
    ):
        raise ValueError("invalid POSIX Codex RPC nofile limit")

    scope = _history_scope.get()
    # ContextVars propagate to child tasks. A speculative/background task must
    # never read another task's stdio or outlive its parent's connection.
    if (scope is not None and scope[0] is asyncio.current_task()
            and method in {"thread/turns/list", "thread/items/list", "thread/list"}
            and nofile_soft_limit is None):
        _owner, stack, readers = scope
        key = (codex_home, cwd)
        if key not in readers:
            connection = await stack.enter_async_context(AsyncExitStack())
            request = await connection.enter_async_context(_rpc_connection(cwd, codex_home))
            readers[key] = (request, connection)
        request, connection = readers[key]
        try:
            return await request(method, params)
        except CodexRpcRejected:
            raise
        except BaseException:
            # A truncated frame/EOF cannot be reused by a narrower fallback.
            # Never retry this request; the next explicit read gets a new pipe.
            readers.pop(key, None)
            await connection.aclose()
            raise
    async with _rpc_connection(cwd, codex_home, nofile_soft_limit) as request:
        return await request(method, params)


_history_scope: ContextVar[tuple | None] = ContextVar("codex_history_rpc", default=None)


@asynccontextmanager
async def codex_history_rpc_scope():
    """Reuse only a page's read-only RPC pipe, isolated by task and account.

    Native sessions stay on their existing daemon. This merely groups the
    existing persisted-history reads; it never resumes a thread or sends input.
    Every pipe is reaped when the page finishes, fails or is cancelled.
    """
    current = _history_scope.get()
    if current is not None and current[0] is asyncio.current_task():
        yield
        return
    async with AsyncExitStack() as stack:
        token = _history_scope.set((asyncio.current_task(), stack, {}))
        try:
            yield
        finally:
            _history_scope.reset(token)


@asynccontextmanager
async def _rpc_connection(cwd, codex_home, nofile_soft_limit=None):
    bin_path = await asyncio.to_thread(_resolve_codex_bin)
    workdir = os.path.realpath(os.path.expanduser(cwd or "~"))
    app_server_argv = (bin_path, "app-server", "--stdio")
    if nofile_soft_limit is not None:
        app_server_argv = (
            sys.executable,
            _RLIMIT_EXEC,
            str(nofile_soft_limit),
            *app_server_argv,
        )
    proc = await asyncio.create_subprocess_exec(
        *app_server_argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=workdir,
        env=_child_env(bin_path, codex_home),
        limit=_STREAM_LIMIT,
    )

    async def send(message: dict[str, Any]) -> None:
        if proc.stdin is None:
            raise RuntimeError("codex app-server stdin unavailable")
        proc.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
        await proc.stdin.drain()

    async def result(request_id: int) -> Any:
        if proc.stdout is None:
            raise RuntimeError("codex app-server stdout unavailable")
        while True:
            try:
                line = await proc.stdout.readline()
            except ValueError as exc:
                # asyncio StreamReader.readline() raises ValueError when one
                # newline-delimited JSON-RPC frame exceeds its configured
                # limit. Preserve this distinction so bounded read-only
                # consumers can choose a narrower compatibility source.
                raise CodexRpcResponseTooLarge(
                    "codex app-server response exceeded the stdio limit"
                ) from exc
            if not line:
                raise RuntimeError("codex app-server closed before responding")
            try:
                message = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(message, dict):
                continue
            if message.get("id") != request_id or "method" in message:
                continue
            if "error" in message:
                raise _rpc_error(message["error"])
            return message.get("result")

    try:
        await send({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "cc-remote", "version": __version__},
                "capabilities": {"experimentalApi": True},
            },
        })
        await asyncio.wait_for(result(1), timeout=_RPC_TIMEOUT)
        await send({"jsonrpc": "2.0", "method": "initialized"})
        request_id = 1

        async def call(method, params):
            nonlocal request_id
            request_id += 1
            request: dict[str, Any] = {
                "jsonrpc": "2.0", "id": request_id, "method": method,
            }
            if params is not None:
                request["params"] = params
            try:
                # Mutations retain the one-shot outcome-unknown contract.
                await send(request)
                return await asyncio.wait_for(result(request_id), timeout=_RPC_TIMEOUT)
            except (CodexRpcRejected, CodexRpcResponseTooLarge):
                raise
            except Exception as exc:
                raise CodexRpcOutcomeUnknown(
                    "codex app-server closed before the request outcome was known"
                ) from exc

        yield call
    finally:
        await _stop_process(proc)


async def codex_rpc_batch(
    requests: list[tuple[str, Optional[dict[str, Any]]]],
    cwd: Optional[str] = None,
    *,
    timeout: float = _RPC_TIMEOUT,
    codex_home: Optional[str] = None,
) -> list[Any | Exception]:
    """Issue a read-only request batch through one initialized app-server.

    Each response occupies the same position as its request. An individual
    JSON-RPC rejection is returned as ``CodexRpcRejected`` so inventory callers
    can preserve successful components. Process/initialization failures before
    submission still raise; transport failures after submission become
    per-request ``CodexRpcOutcomeUnknown`` values. The shared deadline preserves
    responses received before a slower component times out.
    """
    if not isinstance(requests, list):
        raise TypeError("codex RPC batch must be a list")
    for method, params in requests:
        if not isinstance(method, str) or not method:
            raise ValueError("codex RPC method must be a non-empty string")
        if params is not None and not isinstance(params, dict):
            raise TypeError("codex RPC params must be a dict or None")
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("codex RPC timeout must be positive")
    if not requests:
        return []

    deadline = asyncio.get_running_loop().time() + timeout

    def remaining() -> float:
        return max(0.0, deadline - asyncio.get_running_loop().time())

    bin_path = await asyncio.wait_for(
        asyncio.to_thread(_resolve_codex_bin), timeout=remaining(),
    )
    workdir = os.path.realpath(os.path.expanduser(cwd or "~"))
    proc = await asyncio.wait_for(
        asyncio.create_subprocess_exec(
            bin_path,
            "app-server",
            "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=workdir,
            env=_child_env(bin_path, codex_home),
            limit=_STREAM_LIMIT,
        ),
        timeout=remaining(),
    )

    async def send(message: dict[str, Any]) -> None:
        if proc.stdin is None:
            raise RuntimeError("codex app-server stdin unavailable")
        proc.stdin.write(
            (json.dumps(message, separators=(",", ":")) + "\n").encode()
        )
        await asyncio.wait_for(proc.stdin.drain(), timeout=remaining())

    async def response_for(request_id: int) -> Any:
        if proc.stdout is None:
            raise RuntimeError("codex app-server stdout unavailable")
        while True:
            line = await proc.stdout.readline()
            if not line:
                raise RuntimeError("codex app-server closed before responding")
            try:
                message = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(message, dict):
                continue
            if message.get("id") != request_id or "method" in message:
                continue
            if "error" in message:
                raise _rpc_error(message["error"])
            return message.get("result")

    try:
        await send({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "cc-remote", "version": __version__},
            },
        })
        await asyncio.wait_for(response_for(1), timeout=remaining())
        await send({"jsonrpc": "2.0", "method": "initialized"})

        pending: dict[int, int] = {}
        values: list[Any | Exception] = [None] * len(requests)
        completed: set[int] = set()
        try:
            for index, (method, params) in enumerate(requests):
                request_id = index + 2
                request: dict[str, Any] = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                }
                if params is not None:
                    request["params"] = params
                await send(request)
                pending[request_id] = index

            async def collect() -> None:
                if proc.stdout is None:
                    raise RuntimeError("codex app-server stdout unavailable")
                while pending:
                    line = await proc.stdout.readline()
                    if not line:
                        raise RuntimeError(
                            "codex app-server closed before responding"
                        )
                    try:
                        message = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(message, dict) or "method" in message:
                        continue
                    request_id = message.get("id")
                    if request_id not in pending:
                        continue
                    index = pending.pop(request_id)
                    values[index] = (
                        _rpc_error(message["error"])
                        if "error" in message
                        else message.get("result")
                    )
                    completed.add(index)

            await asyncio.wait_for(collect(), timeout=remaining())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            for index in range(len(values)):
                if index in completed:
                    continue
                unknown = CodexRpcOutcomeUnknown(
                    "codex app-server closed before the request outcome was known"
                )
                unknown.__cause__ = exc
                values[index] = unknown
        return values
    finally:
        await _stop_process(proc)
