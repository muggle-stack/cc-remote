"""Device maintenance RPC and the short activation admission barrier."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen, getproxies_environment, proxy_bypass_environment

from websockets.asyncio.client import connect

from cc_remote.protocol import Error, ERR_BUSY
from cc_remote.update import UpdateError, host_platform, read_installation, release_repository, version_tuple
from cc_remote.update_jobs import (TERMINAL, UpdateJobs, failure_reason, helper_path, job_id, public_job)


def release_catalog(repository: str | None = None) -> dict:
    request = Request(f"https://api.github.com/repos/{repository or release_repository()}/releases/latest",
                      headers={"Accept": "application/vnd.github+json", "User-Agent": "cc-remote-updater"})
    with urlopen(request, timeout=15) as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise UpdateError("oversized GitHub release response")
    value = json.loads(raw)
    if (not isinstance(value, dict) or value.get("draft") is not False
            or value.get("prerelease") is not False or not str(value.get("tag_name", "")).startswith("v")):
        raise UpdateError("invalid stable release")
    version = value["tag_name"][1:]
    version_tuple(version)
    return {"version": version, "notes": str(value.get("body") or "")[:4000]}


class UIUpdates:
    def __init__(self, machine, root: Path | None = None):
        self.machine = machine
        # Source checkouts and unit tests must never discover/mutate a separate
        # live installation merely because it exists in the user's home.
        release = Path(__file__).resolve().parents[2]
        self.root = root or (release.parent.parent if release.parent.name == "releases"
                             and (release / "release-manifest.json").is_file() else None)
        self.task = None
        self.reserved: str | None = None
        self.catalog = None
        self.checked_at = 0.0
        self.check_lock = asyncio.Lock()
        self.request_lock = asyncio.Lock()
        self.restore_reservation()

    def restore_reservation(self) -> None:
        # Recovery precedes scheduler/message startup on a new Wrapper.
        if self.root is not None:
            try:
                state = self.store().active()
                if state and (state["phase"] in {"updating_relay", "installing", "verifying"}
                              or state["phase"] == "unknown" and state.get("worker_pid")):
                    self.reserved = state["id"]
            except (OSError, ValueError):
                pass

    def store(self) -> UpdateJobs:
        if self.root is None:
            raise UpdateError("not a managed Release installation")
        return UpdateJobs(self.root)

    def busy_count(self) -> int:
        count = 0
        for ctx in self.machine.sessions.values():
            tasks = [getattr(ctx, name, None) for name in
                     ("turn_task", "codex_spontaneous_task", "auto_compact_apply_task", "queued_query_drain_task")]
            backgrounds = (getattr(ctx, "codex_background_processes", None) or {}).values()
            if (ctx.state != "idle" or ctx.queued_queries or ctx.query_lock.locked()
                    or ctx.pending_asks or ctx.queued_query_starting_msg_id
                    or getattr(ctx.sdk, "turn_active", False) is True
                    or (ctx.engine == "dsh" and any(child.get("activity") == "running"
                                                   for child in getattr(ctx.sdk, "children", [])))
                    or any(t is not None and not t.done() for t in tasks)
                    or self.machine._claude_has_background_work(ctx)
                    or any(getattr(item, "status", None) in {"running", "pending", "unknown"}
                           for item in backgrounds)):
                count += 1
        count += sum(not task.done() for task in getattr(self.machine, "_work_schedule_runs", ()))
        count += bool(getattr(self.machine, "_ui_schedule_scanning", False))
        scheduler = getattr(self.machine, "_command_scheduler", None)
        if scheduler is not None:
            count += sum(not p.task.done() for p in scheduler._pending.values())
        runtime = getattr(self.machine, "_dsh", None)
        count += sum(job.status in {"running", "stopping"}
                     for rows in getattr(runtime, "jobs", {}).values() for job in rows)
        count += sum(not task.done() for task in getattr(runtime, "questions", {}).values())
        return count

    def guarded(self) -> bool:
        if self.reserved is None:
            return False
        try:
            state = self.store().state(self.reserved)
            if state["phase"] in TERMINAL:
                self.reserved = None
                return False
        except (OSError, ValueError):
            pass
        return True

    async def reject(self, cmd):
        if (not self.guarded() or cmd.type.startswith(("get_", "list_", "read_", "browse_"))
                or cmd.type in {"hello", "ping", "interrupt", "cancel_queued_query", "answer_question",
                                "acknowledge_completion"}):
            return None
        sid = getattr(cmd, "session_id", None) or getattr(cmd, "sid", None) or self.machine.focused_sid
        error = Error(code=ERR_BUSY, message="设备正在安装更新，请在重新连接后继续。",
                      sid=sid, request_id=getattr(cmd, "cmd_id", None),
                      msg_id=getattr(cmd, "msg_id", None), to=getattr(cmd, "client_id", None))
        await self.machine.transport.send(error)
        return error

    def reserve(self, identity: str) -> dict:
        store = self.store()
        state = store.active()
        if (state is None or state["id"] != job_id(identity) or state["phase"] != "waiting"
                or state.get("cancel_requested") or not store.policy()["enabled"]):
            return {"status": "rejected"}
        # No await between the final local activity check and admission barrier.
        if self.busy_count():
            return {"status": "busy"}
        self.reserved = identity
        return {"status": "ready"}

    async def status(self, *, check: bool = False) -> dict:
        result = {"schema": 1, "managed": False, "enabled": False, "reason": "unsupported",
                  "busy_count": self.busy_count(), "latest": self.catalog, "job": None}
        if self.root is None:
            return result
        try:
            current = read_installation(self.root, *host_platform())
            result.update(managed=True, current=current.manifest["product_version"], reason="setup_required")
            store = self.store()
            try:
                policy = store.policy()
                result.update(enabled=policy["enabled"], allow_relay_update=policy["allow_relay_update"],
                              reason=None if policy["enabled"] else "setup_required")
                if policy["uid"] != os.getuid():
                    result.update(enabled=False, reason="permission_required")
                if (policy["control_socket"] != str(self.machine._local_control.socket)
                        or self.machine._local_control.server is None):
                    result.update(enabled=False, reason="control_unavailable")
                state = store.active()
                result["job"] = public_job(state)
                if state and state["phase"] not in TERMINAL and state.get("worker_pid"):
                    from cc_remote.wrapper.process_scan import process_identity
                    worker = await asyncio.to_thread(process_identity, state["worker_pid"])
                    if worker is None or worker.start_ticks != state.get("worker_start"):
                        result["job"].update(phase="unknown", reason="job_unknown")
            except FileNotFoundError:
                pass
            if check:
                async with self.check_lock:
                    if time.monotonic() - self.checked_at >= 60:
                        try:
                            repository = store.policy()["repository"]
                        except FileNotFoundError:
                            repository = release_repository()
                        self.catalog = await asyncio.to_thread(release_catalog, repository)
                        self.checked_at = time.monotonic()
                result["latest"] = self.catalog
            self.guarded()
        except (OSError, ValueError) as exc:
            result["reason"] = failure_reason(exc)
        return result

    def invoke(self, operation: str, identity: str, version: str | None = None) -> dict:
        store = self.store()
        policy = store.policy()
        if not policy["enabled"]:
            raise UpdateError("UI updates are disabled")
        job_id(identity)
        if version is not None:
            version_tuple(version)
        if host_platform()[0] == "linux":
            command = ["/usr/bin/sudo", "-n", str(helper_path(self.root)), operation, identity]
        else:
            current = read_installation(self.root, *host_platform())
            command = [str(current.release / ".venv/bin/python"), "-I", "-B",
                       str(current.release / "deploy/ui_update_job.py"), "--root", str(self.root), operation, identity]
        if version is not None:
            command.append(version)
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=50, check=False)
        if len(result.stdout) > 32768:
            raise UpdateError("invalid update launcher result")
        try:
            value = json.loads(result.stdout)
        except ValueError:
            return {"ok": False, "reason": "permission_required"}
        if not isinstance(value, dict) or type(value.get("ok")) is not bool:
            raise UpdateError("invalid update launcher result")
        return value

    async def handle(self, request: dict) -> dict:
        operation = request.get("op")
        if not isinstance(operation, str):
            return {"ok": False, "reason": "invalid_request"}
        if operation in {"check", "status"} and set(request) == {"op"}:
            return await self.status(check=operation == "check")
        required = {"op", "job_id", "version"} if operation == "start" else {"op", "job_id"}
        if operation not in {"start", "cancel"} or set(request) != required:
            return {"ok": False, "reason": "invalid_request"}
        try:
            job_id(request["job_id"])
            async with self.request_lock:
                # The root launcher remains authoritative, but avoid accepting
                # an install that cannot reach this Wrapper's idle barrier.
                if operation == "start":
                    status = await self.status()
                    if not status["enabled"]:
                        return {"ok": False, "reason": status.get("reason") or "setup_required"}
                result = await asyncio.to_thread(self.invoke, operation, request["job_id"], request.get("version"))
                if result.get("reason") == "release_changed":
                    self.catalog, self.checked_at = None, 0.0
                return result
        except subprocess.TimeoutExpired:
            return {"ok": False, "reason": "outcome_pending"}
        except (OSError, ValueError) as exc:
            return {"ok": False, "reason": failure_reason(exc)}

    async def start(self):
        if self.root is None:
            return
        self.restore_reservation()
        self.task = asyncio.create_task(self._connect())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def _connect(self):
        target = urlsplit(self.machine.transport.url)
        url = urlunsplit((target.scheme, target.netloc, "/ws/updates", "", ""))
        proxies = getproxies_environment()
        proxy = None if proxy_bypass_environment(target.netloc, proxies) else proxies.get("https") or proxies.get("http")
        backoff = 1
        while True:
            tasks = set()
            try:
                async with connect(url, additional_headers={"Authorization": f"Bearer {self.machine.transport.token}"},
                                   proxy=proxy, max_size=65536, max_queue=4) as socket:
                    await socket.send(json.dumps({"schema": 1, "machine_id": self.machine.cfg.machine_id}))
                    async with asyncio.timeout(10):
                        hello = json.loads(await socket.recv())
                    if hello != {"schema": 1, "ready": True}:
                        raise ValueError("maintenance connection was not acknowledged")
                    backoff = 1

                    async def answer(frame):
                        result = await self.handle({k: v for k, v in frame.items() if k not in {"schema", "id"}})
                        try:
                            await socket.send(json.dumps({"schema": 1, "id": frame["id"], "result": result}))
                        except Exception:
                            pass

                    async for raw in socket:
                        frame = json.loads(raw)
                        if (not isinstance(frame, dict) or type(frame.get("schema")) is not int or frame["schema"] != 1
                                or not isinstance(frame.get("id"), str)
                                or not re.fullmatch(r"[a-f0-9]{32}", frame["id"])):
                            raise ValueError("invalid update request")
                        if len(tasks) >= 8:
                            raise ValueError("too many update requests")
                        task = asyncio.create_task(answer(frame))
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
            except Exception:
                pass
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
