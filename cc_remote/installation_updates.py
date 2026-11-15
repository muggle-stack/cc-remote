"""Shared bounded maintenance RPC for managed installations, without engine startup."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import time
from urllib.request import Request, urlopen

from cc_remote.update import UpdateError, host_platform, read_installation, release_repository, version_tuple
from cc_remote.update_errors import read_with_retry
from cc_remote.update_jobs import TERMINAL, UpdateJobs, failure_reason, helper_path, job_id, public_job


def release_catalog(repository: str | None = None) -> dict:
    request = Request(f"https://api.github.com/repos/{repository or release_repository()}/releases/latest",
                      headers={"Accept": "application/vnd.github+json", "User-Agent": "cc-remote-updater"})
    raw = read_with_retry(urlopen, request, timeout=15, limit=1024 * 1024)
    if len(raw) > 1024 * 1024:
        raise UpdateError("oversized GitHub release response")
    value = json.loads(raw)
    if (not isinstance(value, dict) or value.get("draft") is not False
            or value.get("prerelease") is not False or not str(value.get("tag_name", "")).startswith("v")):
        raise UpdateError("invalid stable release")
    version = value["tag_name"][1:]
    version_tuple(version)
    return {"version": version, "notes": str(value.get("body") or "")[:4000]}


class InstallationUpdates:
    def __init__(self, root: Path | None = None):
        release = Path(__file__).resolve().parents[1]
        self.root = root or (release.parent.parent if release.parent.name == "releases"
                             and (release / "release-manifest.json").is_file() else None)
        self.catalog = None
        self.checked_at = 0.0
        self.check_lock = asyncio.Lock()
        self.request_lock = asyncio.Lock()
        self.recovery_task = None

    async def status(self, *, check: bool = False) -> dict:
        raise NotImplementedError

    def store(self) -> UpdateJobs:
        if self.root is None:
            raise UpdateError("not a managed Release installation")
        return UpdateJobs(self.root)

    async def _public_state(self, state: dict | None) -> dict | None:
        result = public_job(state)
        if state and state["phase"] not in TERMINAL and state.get("worker_pid"):
            from cc_remote.wrapper.process_scan import process_identity
            worker = await asyncio.to_thread(process_identity, state["worker_pid"])
            if worker is None or worker.start_ticks != state.get("worker_start"):
                result.update(phase="unknown", reason="job_unknown")
                from cc_remote import activation
                try:
                    running = activation.receipt(self.root)
                    if running and running["id"] == state["id"] and activation.install_locked(self.root):
                        result.update(phase="verifying" if running["phase"] == "verifying" else "installing",
                                      reason="installer_running")
                except (OSError, ValueError):
                    pass
        return result

    async def lookup(self, identity: str) -> dict:
        """Observe an exact receipt without launching, cancelling or replacing it.

        Absence is not a fence against a delayed start: clients must retain and
        reuse this identity when the user retries, even after a missing result.
        """
        job_id(identity)
        store = self.store()
        store.validate_folder()
        try:
            state = store.state(identity)
        except FileNotFoundError:
            return {"ok": True, "job_id": identity, "receipt": "missing", "job": None}
        return {"ok": True, "job_id": identity, "receipt": "found", "job": await self._public_state(state)}

    def invoke(self, operation: str, identity: str, version: str | None = None) -> dict:
        store = self.store()
        policy = store.policy()
        if not policy["enabled"] and operation not in {"cancel", "reconcile", "observe", "prune_cache"}:
            raise UpdateError("UI updates are disabled")
        job_id(identity)
        if version is not None:
            version_tuple(version)
        if host_platform()[0] == "linux":
            broker = self.root / "ui-update.sock"
            if broker.exists():
                import socket
                import stat
                info = broker.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o007:
                    raise UpdateError("unsafe update broker permissions")
                request = {"op": operation, "id": identity}
                if version is not None:
                    request["version"] = version
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                    peer.settimeout(50)
                    peer.connect(str(broker))
                    peer.sendall(json.dumps(request).encode() + b"\n")
                    with peer.makefile("rb") as stream:
                        raw = stream.readline(32769)
                    if len(raw) > 32768:
                        raise UpdateError("invalid update broker response")
                    result = json.loads(raw)
                if not isinstance(result, dict) or type(result.get("ok")) is not bool:
                    raise UpdateError("invalid update broker response")
                return result
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
        if operation not in {"start", "cancel", "lookup", "reconcile"} or set(request) != required:
            return {"ok": False, "reason": "invalid_request"}
        try:
            job_id(request["job_id"])
            if operation == "lookup":
                return await self.lookup(request["job_id"])
            async with self.request_lock:
                # The root launcher remains authoritative. Check this target's
                # policy and (for Wrappers) its local admission channel first.
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

    async def _recover_pending(self):
        from cc_remote import activation
        from cc_remote.update_jobs import worker_alive
        cache_checked_at = time.monotonic() - 3600
        while True:
            if time.monotonic() - cache_checked_at >= 3600:
                cache_checked_at = time.monotonic()
                try:
                    await asyncio.to_thread(self.invoke, "prune_cache", "0" * 32)
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
            try:
                state = self.store().active()
                if state and state["phase"] not in TERMINAL:
                    if not await asyncio.to_thread(worker_alive, state):
                        await asyncio.to_thread(self.invoke, "observe", state["id"])
                else:
                    record = activation.receipt(self.root)
                    if record and record["phase"] in activation.ACTIVE_PHASES and not activation.install_locked(self.root):
                        await asyncio.to_thread(self.invoke, "observe", record["id"])
            except (OSError, ValueError, subprocess.SubprocessError):
                pass  # Keep the receipt and retry observation, never resubmit an installation.
            await asyncio.sleep(5)
