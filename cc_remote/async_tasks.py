"""Opt-in generic async command tasks with native Codex completion delivery.

Execution runs through command/exec on the existing official account daemon,
with its configured permissions, never through an unrestricted shell fallback.
The detached worker owns those connections independently of the MCP/browser.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import suppress
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from cc_remote import task_rpc
from cc_remote.task_store import OUTPUT_BYTES, TERMINAL, TaskStore, public_task, request_thread
from cc_remote.wrapper.child_env import sanitized_child_env

POLL_SECONDS = 0.25
RETRY_SECONDS = 30
DELIVERY_TRACK_SECONDS = 86400


class CancelledBeforeExecution(Exception):
    pass


class ExpiredBeforeExecution(Exception):
    pass


def lock_worker(store):
    # Initialize / validate the private state directory before creating a lock.
    with store.db():
        pass
    fd = os.open(store.root / "worker.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def ensure_worker(store):
    fd = lock_worker(store)
    if fd is None:
        return
    # Pass the already-held lock through exec, so concurrent starts don't race
    # an idle worker's exit. The child keeps its immutable runtime cwd in use.
    try:
        subprocess.Popen(
            [sys.executable, "-m", "cc_remote.async_tasks", "worker", "--codex-home",
             str(store.home), "--lock-fd", str(fd)],
            cwd=Path(__file__).resolve().parent.parent, env=sanitized_child_env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, pass_fds=(fd,),
        )
    finally:
        os.close(fd)


async def execute(store, task):
    task_id, spec = task["id"], task["spec"]
    if task["cancel_requested"]:
        store.update(task_id, state="cancelled", delivery="suppressed")
        return
    remaining = spec["timeout_seconds"] - (time.time() - task["created"])
    if remaining <= 0:
        store.update(task_id, state="timed_out", error="Task deadline expired")
        return
    native_deadline = None
    output = bytearray(task["output"])
    truncated = bool(task["truncated"])

    def on_output(params):
        nonlocal truncated
        if params.get("processId") != task_id:
            return
        chunk = base64.b64decode(params.get("deltaBase64", ""), validate=True)
        space = max(0, OUTPUT_BYTES - len(output))
        output.extend(chunk[:space])
        truncated |= len(chunk) > space or bool(params.get("capReached"))
        store.update(task_id, output=bytes(output), truncated=int(truncated))

    def before_send():
        nonlocal native_deadline
        if not store.begin_execution(task_id):
            raise CancelledBeforeExecution()
        # Connection, initialization, thread validation, scheduling and the
        # store write can all consume budget. This hook runs immediately before
        # native.rpc serializes/sends the command, with no intervening await.
        remaining_at_send = spec["timeout_seconds"] - (time.time() - task["created"])
        if remaining_at_send <= 0:
            raise ExpiredBeforeExecution()
        timeout_ms = max(1, int(remaining_at_send * 1000))
        command_params["timeoutMs"] = timeout_ms
        native_deadline = time.monotonic() + timeout_ms / 1000

    try:
        async with task_rpc.connect(store.home, on_output) as native:
            # Check the exact original thread before any command is accepted.
            await native.thread(task["sid"])
            if store.get(task_id)["cancel_requested"]:
                store.update(task_id, state="cancelled", delivery="suppressed")
                return
            command_params = {
                "command": spec["argv"], "cwd": spec["cwd"], "processId": task_id,
                "outputBytesCap": OUTPUT_BYTES // 2, "streamStdoutStderr": True,
            }
            command = asyncio.create_task(native.rpc("command/exec", command_params, timeout=remaining + 10,
                before_send=before_send))
            reason = None
            try:
                while not command.done():
                    await asyncio.wait({command}, timeout=POLL_SECONDS)
                    current = store.get(task_id)
                    if current["cancel_requested"]:
                        reason = "cancelled"
                    elif time.time() >= task["created"] + spec["timeout_seconds"]:
                        reason = "timed_out"
                    if reason and not command.done():
                        with suppress(Exception):
                            await native.rpc("command/exec/terminate", {"processId": task_id}, timeout=5)
                        break
                result = await asyncio.wait_for(asyncio.shield(command), 5)
                code = result["exitCode"]
                if type(code) is not int:
                    raise ValueError("Missing native exit code")
                # A cancellation accepted before completion always suppresses wakeup.
                current = store.get(task_id)
                if current["cancel_requested"]:
                    reason = "cancelled"
                elif code == 124 and native_deadline is not None and time.monotonic() >= native_deadline:
                    # Native expiration uses 124 and can win the poll by the
                    # sub-millisecond rounding in timeoutMs. An early explicit
                    # exit(124) remains a failure, not a deadline expiration.
                    reason = "timed_out"
                store.update(task_id, state=reason or ("completed" if code == 0 else "failed"),
                             exit_code=code, output=bytes(output), truncated=int(truncated))
            finally:
                command.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await command
    except CancelledBeforeExecution:
        store.update(task_id, state="cancelled", delivery="suppressed")
    except ExpiredBeforeExecution:
        if store.get(task_id)["cancel_requested"]:
            store.update(task_id, state="cancelled", delivery="suppressed")
        else:
            store.update(task_id, state="timed_out", error="Task deadline expired before command submission")
    except task_rpc.Rejected:
        store.update(task_id, state="failed", error="Official daemon rejected command or thread access")
    except Exception:
        # Before submission we may retry connecting. Afterwards the command may
        # have had effects: closing its native connection stops it, never rerun.
        if store.get(task_id)["state"] == "queued":
            store.update(task_id, next_attempt=time.time() + RETRY_SECONDS,
                         error="Shared daemon unavailable; waiting before submission")
        else:
            store.update(task_id, state="interrupted", error="Execution connection lost; command was not rerun")


async def deliver(store, task):
    task_id = task["id"]
    if task["state"] not in TERMINAL or task["delivery"] != "pending":
        return
    receipt = public_task(task, output=True)
    receipt.pop("notification")
    receipt.pop("notification_error")
    receipt.pop("notification_turn_id")
    try:
        async with task_rpc.connect(store.home) as native:
            thread = await native.thread(task["sid"])
            if thread.get("status", {}).get("type") == "notLoaded":
                # Resume only this account's still-active persisted thread.
                # Archived/deleted or unverifiable threads are never revived.
                path = thread.get("path")
                active_root = (store.home / "sessions").resolve()
                if (not isinstance(path, str) or not Path(path).is_file()
                        or not Path(path).resolve().is_relative_to(active_root)):
                    store.update(task_id, next_attempt=time.time() + RETRY_SECONDS,
                                 delivery_error="Original thread is unavailable or archived; completion retained")
                    return
                resumed = await native.rpc("thread/resume", {"threadId": task["sid"]})
                if resumed.get("thread", {}).get("id") != task["sid"]:
                    raise task_rpc.Rejected("Original thread resume identity was not confirmed")
            result = await native.rpc("turn/start", {
                "threadId": task["sid"], "input": [],
                "toolOutput": {"namespace": "cc_remote_tasks", "name": "task_result",
                               "output": json.dumps(receipt, ensure_ascii=False)},
            }, before_send=lambda: store.begin_delivery(task_id))
            turn = result.get("turn") if isinstance(result, dict) else None
            if (not isinstance(turn, dict) or not isinstance(turn.get("id"), str)
                    or not turn["id"] or len(turn["id"]) > 512):
                raise ValueError("Missing native acceptance receipt")
            # Acceptance can precede a provider error. Persist the exact turn
            # before reading its outcome; reconnects must never resend it.
            store.update(task_id, delivery="accepted", delivery_error=None,
                         delivery_turn_id=turn["id"], delivery_accepted_at=time.time(),
                         next_attempt=0)
    except task_rpc.Rejected:
        store.update(task_id, delivery="rejected", delivery_error="Official daemon rejected completion delivery")
    except Exception:
        current = store.get(task_id)
        if current["delivery"] == "sending":
            store.update(task_id, delivery="unknown",
                         delivery_error="Completion acknowledgment lost; inspect task_result; not resent")
        elif current["delivery"] == "pending":
            store.update(task_id, next_attempt=time.time() + RETRY_SECONDS,
                         delivery_error="Shared daemon unavailable; completion retained for retry")


async def check_delivery(store, task):
    if task["delivery"] != "accepted":
        return
    task_id = task["id"]
    status = None
    try:
        # Do not hold a command slot for the model's full turn. A short read is
        # retried later, independently of execution and without engine mutation.
        async with asyncio.timeout(5):
            async with task_rpc.connect(store.home) as native:
                status = await native.turn_status(task["sid"], task["delivery_turn_id"])
    except Exception:
        pass
    if status == "completed":
        store.update(task_id, delivery="delivered", delivery_error=None)
    elif status in {"failed", "interrupted"}:
        store.update(task_id, delivery="failed", delivery_error=(
            f"Codex callback turn {status}; result retained in task_result; not resent"))
    elif time.time() >= (task["delivery_accepted_at"] or 0) + DELIVERY_TRACK_SECONDS:
        store.update(task_id, delivery="unknown", delivery_error=(
            "Callback outcome could not be confirmed within 24 hours; result retained; not resent"))
    else:
        store.update(task_id, next_attempt=time.time() + RETRY_SECONDS,
                     delivery_error=None if status == "inProgress" else
                     "Callback accepted; waiting to confirm its outcome; not resent")


async def process_task(store, task_id):
    task = store.get(task_id)
    if task["state"] == "queued":
        await execute(store, task)
    await deliver(store, store.get(task_id))
    await check_delivery(store, store.get(task_id))


async def run_worker(store):
    # Caller owns worker.lock for this entire lifetime.
    store.recover()
    active = {}
    try:
        while True:
            for key, job in list(active.items()):
                if job.done():
                    try:
                        job.result()
                    except Exception:
                        # Don't spin on a corrupt record or replay a mutation.
                        store.update(key, state="interrupted", delivery="unknown",
                                     error="Task worker failed; inspect the retained record")
                    del active[key]
            for key in store.recoverable():
                if len(active) >= 8:
                    break
                if key not in active:
                    active[key] = asyncio.create_task(process_task(store, key))
            if not active and not store.has_pending():
                # main() rechecks pending work after releasing the lifetime lock.
                return
            await asyncio.sleep(POLL_SECONDS)
    finally:
        for job in active.values():
            job.cancel()
        await asyncio.gather(*active.values(), return_exceptions=True)


def make_server(store):
    from mcp.server import Server
    from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

    server = Server("cc-remote-tasks")
    descriptions = {
        "task_start": "Start a generic command in the background using the official account daemon's configured "
                      "permissions (not the current thread's temporary overrides). Return immediately. Completion "
                      "is sent to this Codex thread as tool output; task_status separately tracks callback "
                      "acceptance and its turn outcome. On callback failure, inspect task_result. Reuse request_id only when "
                      "retrying the SAME submission. argv is executed directly; invoke a shell explicitly if needed. "
                      "Exit code zero reports process completion, not semantic task success.",
        "task_status": "Read this thread's task state and notification status. Omit task_id to list recent tasks.",
        "task_result": "Read bounded output for this thread's task, including failed or unfinished tasks.",
        "task_cancel": "Cancel this thread's task and suppress its pending automatic completion notification. "
                       "Already sent notifications cannot be recalled.",
    }

    @server.list_tools()
    async def list_tools():
        tools = []
        for name, description in descriptions.items():
            props = {"task_id": {"type": "string"}}
            required = ["task_id"]
            if name == "task_status":
                required = []
            if name == "task_start":
                props = {"argv": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 128},
                         "cwd": {"type": "string"}, "title": {"type": "string", "maxLength": 120},
                         "request_id": {"type": "string", "maxLength": 128},
                         "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 86400}}
                required = ["argv", "cwd", "title", "request_id"]
            tools.append(Tool(name=name, description=description, inputSchema={
                "type": "object", "properties": props, "required": required, "additionalProperties": False},
                annotations=ToolAnnotations(readOnlyHint=name in {"task_status", "task_result"},
                                            destructiveHint=name in {"task_start", "task_cancel"},
                                            openWorldHint=True)))
        return tools

    @server.call_tool()
    async def call_tool(name, args):
        try:
            sid = request_thread(server.request_context.meta)
            if name == "task_status" and "task_id" not in args:
                return {"tasks": store.list_for(sid)}
            if name == "task_start":
                async with task_rpc.connect(store.home) as native:
                    await native.thread(sid)
                key = store.create(sid, **args)
                ensure_worker(store)
            elif name in {"task_status", "task_result", "task_cancel"}:
                key = args["task_id"]
                store.get(key, sid)
                if name == "task_cancel":
                    store.cancel(key, sid)
                    ensure_worker(store)
            else:
                raise ValueError("Unknown task tool")
            return public_task(store.get(key, sid), output=name == "task_result")
        except (ValueError, OSError, task_rpc.Rejected, ConnectionError, TimeoutError) as exc:
            # Do not serialize arbitrary upstream exceptions (may contain command
            # lines, paths or credentials). Local validation errors are controlled.
            message = str(exc) if isinstance(exc, (ValueError, task_rpc.Rejected)) else "Task service unavailable; retry with the same request_id"
            return CallToolResult(isError=True, content=[TextContent(type="text", text=message)])

    return server


async def serve(store):
    from mcp.server.stdio import stdio_server

    if store.has_pending():
        ensure_worker(store)
    server = make_server(store)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generic async tasks for an existing Codex account daemon")
    parser.add_argument("command", choices=("mcp", "worker", "refresh"))
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument("--thread-id", help="refresh: verify task_status on an existing thread (no model turn)")
    parser.add_argument("--lock-fd", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.thread_id is not None and args.command != "refresh":
        parser.error("--thread-id is only valid for refresh")
    if args.command == "refresh":
        from cc_remote.task_refresh import refresh

        try:
            result = asyncio.run(refresh(args.codex_home, Path(__file__).resolve().parent.parent,
                                         args.thread_id))
        except (ValueError, OSError, task_rpc.Rejected, ConnectionError, TimeoutError) as exc:
            print(str(exc) if isinstance(exc, ValueError)
                  else "Task MCP refresh could not be verified; inspect the existing configuration", file=sys.stderr)
            return 1
        print(json.dumps(result))
        return 0
    store = TaskStore(args.codex_home)
    if args.command == "mcp":
        asyncio.run(serve(store))
    else:
        fd = args.lock_fd if args.lock_fd is not None else lock_worker(store)
        if fd is None:
            return 0
        try:
            asyncio.run(run_worker(store))
        finally:
            os.close(fd)
        # Close the idle-exit race: a submitter that saw our old lock may have
        # inserted after the final pending check. It now gets a new worker.
        if store.has_pending():
            ensure_worker(store)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
