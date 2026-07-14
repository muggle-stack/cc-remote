"""Bounded one-shot requests to the Codex app-server control plane."""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Optional


_RPC_TIMEOUT = 30.0
_STREAM_LIMIT = 16 * 1024 * 1024


class CodexRpcRejected(RuntimeError):
    """The app-server returned an explicit JSON-RPC rejection."""


class CodexRpcOutcomeUnknown(RuntimeError):
    """The mutating request may have committed before transport failure."""


def _resolve_codex_bin() -> str:
    # Lazy imports avoid a cycle: CodexHandle imports codex_sessions, which uses
    # this module for thread/list.
    from cc_remote.wrapper.codex_handle import _resolve_codex_bin as resolve

    return resolve()


def _codex_env(bin_path: str) -> dict[str, str]:
    from cc_remote.wrapper.codex_handle import _codex_env as build_env

    return build_env(bin_path)


def _rpc_error(error: Any) -> CodexRpcRejected:
    if not isinstance(error, dict):
        return CodexRpcRejected("codex app-server request failed")
    code = error.get("code")
    message = str(error.get("message") or "request failed")[:512]
    if isinstance(code, int):
        return CodexRpcRejected(f"codex app-server error {code}: {message}")
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


async def codex_rpc(
    method: str, params: Optional[dict[str, Any]], cwd: Optional[str] = None,
) -> Any:
    """Initialize one app-server, issue one request, then always reap it.

    This is for thread/account control-plane calls that do not need a resident
    ``CodexHandle``. Notifications emitted before the matching response are
    intentionally ignored; the response identified by its JSON-RPC id is the
    authoritative result.
    """
    if not isinstance(method, str) or not method:
        raise ValueError("codex RPC method must be a non-empty string")
    if params is not None and not isinstance(params, dict):
        raise TypeError("codex RPC params must be a dict or None")

    bin_path = await asyncio.to_thread(_resolve_codex_bin)
    workdir = os.path.realpath(os.path.expanduser(cwd or "~"))
    proc = await asyncio.create_subprocess_exec(
        bin_path,
        "app-server",
        "--stdio",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=workdir,
        env=_codex_env(bin_path),
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
            "params": {"clientInfo": {"name": "cc-remote", "version": "0.1.0"}},
        })
        await asyncio.wait_for(result(1), timeout=_RPC_TIMEOUT)
        await send({"jsonrpc": "2.0", "method": "initialized"})
        request: dict[str, Any] = {
            "jsonrpc": "2.0", "id": 2, "method": method,
        }
        if params is not None:
            request["params"] = params
        try:
            # From the first write onward a broken pipe, EOF, or timeout cannot
            # prove whether a mutating request committed. Preserve that semantic
            # distinction for callers that must not ACK/replay the mutation.
            await send(request)
            return await asyncio.wait_for(result(2), timeout=_RPC_TIMEOUT)
        except CodexRpcRejected:
            raise
        except Exception as exc:
            raise CodexRpcOutcomeUnknown(
                "codex app-server closed before the request outcome was known"
            ) from exc
    finally:
        await _stop_process(proc)
