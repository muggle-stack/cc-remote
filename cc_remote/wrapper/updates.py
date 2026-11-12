"""Device maintenance RPC and the short activation admission barrier."""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import getproxies_environment, proxy_bypass_environment

from websockets.asyncio.client import connect

from cc_remote.protocol import Error
from cc_remote.update import host_platform, read_installation, release_repository
from cc_remote.installation_updates import InstallationUpdates, release_catalog
from cc_remote.update_jobs import TERMINAL, failure_reason, job_id


class UIUpdates(InstallationUpdates):
    def __init__(self, machine, root: Path | None = None):
        self.machine = machine
        super().__init__(root)
        self.task = None
        self.reserved: str | None = None
        self.activation_admitted: str | None = None
        self.restore_reservation()

    def restore_reservation(self) -> None:
        # Admission is reconstructed from the live install lock and its exact
        # activation receipt by guarded(). UI task status cannot close chat.
        self.reserved = None

    def busy_count(self) -> int:
        """Count work that would lose ownership/state when this Wrapper exits.

        A live model turn is not itself an activation blocker. Only proven
        independent owners may keep running; launch/control windows, local
        queues and temporary attachments must still drain. This stays
        synchronous with reserve() so new commands cannot enter between the
        final check and the maintenance barrier.
        """
        count = 0
        collaboration = getattr(self.machine, "_collaboration", None)
        if collaboration is not None:
            count += collaboration.busy_count()
        for ctx in self.machine.sessions.values():
            if self._local_session_work(ctx):
                count += 1
                continue
            tasks = [getattr(ctx, name, None) for name in ("turn_task", "codex_spontaneous_task")]
            backgrounds = (getattr(ctx, "codex_background_processes", None) or {}).values()
            if (ctx.state != "idle" or ctx.pending_asks
                    or getattr(ctx.sdk, "turn_active", False) is True
                    or (ctx.engine == "dsh" and any(child.get("activity") == "running"
                                                   for child in getattr(ctx.sdk, "children", [])))
                    or any(t is not None and not t.done() for t in tasks)
                    or self.machine._claude_has_background_work(ctx)
                    or any(getattr(item, "status", None) in {"running", "pending", "unknown"}
                           for item in backgrounds)):
                count += not self._independent_session(ctx)
        count += sum(not task.done() for task in getattr(self.machine, "_work_schedule_runs", ()))
        count += bool(getattr(self.machine, "_ui_schedule_scanning", False))
        for name in ("_interactive_control_tasks", "_codex_fork_tasks", "_claude_fork_tasks", "_codex_archive_tasks"):
            count += sum(not task.done() for task in getattr(self.machine, name, {}).values())
        count += sum(not task.done() for task in getattr(self.machine, "_codex_terminal_persist_tasks", ()))
        scheduler = getattr(self.machine, "_command_scheduler", None)
        if scheduler is not None:
            count += sum(not p.task.done() for p in scheduler._pending.values())
        runtime = getattr(self.machine, "_dsh", None)
        # Native DSH jobs are owned by its separately running server. An
        # unavailable connection cannot establish that boundary for this check.
        if not self._independent_dsh():
            count += sum(job.status in {"running", "stopping"}
                         for rows in getattr(runtime, "jobs", {}).values() for job in rows)
        count += sum(not task.done() for task in getattr(runtime, "questions", {}).values())
        return count

    @staticmethod
    def _local_session_work(ctx) -> bool:
        return bool(
            ctx.queued_queries or ctx.queued_query_starting_msg_id
            or any(getattr(ctx, name).locked() for name in (
                "query_lock", "launch_lock", "steer_lock", "queued_query_lock",
                "permission_profile_lock", "claude_control_persist_lock", "codex_daemon_generation_lock"))
            or any(task is not None and not task.done() for task in (
                getattr(ctx, name, None) for name in (
                    "auto_compact_apply_task", "queued_query_drain_task",
                    "claude_followup_recovery_task", "claude_autonomous_interrupt_task")))
            or ctx.transient_turn_attachment_dir or ctx.codex_steer_attachment_dirs
            or ctx.codex_uncertain_steer or ctx.codex_pending_steer_user_identities
            or ctx.codex_turn_start_reconciling or ctx.codex_account_handoff
            or ctx.state in {"interrupting", "draining"}
        )

    def _independent_dsh(self) -> bool:
        from cc_remote.wrapper.dsh_client import DshClient

        runtime = getattr(self.machine, "_dsh", None)
        client = getattr(runtime, "client", None)
        peer = getattr(client, "_peer", None)
        return (isinstance(client, DshClient) and not client._closed
                and peer is not None and peer.reader is not None and not peer.reader.done()
                and not runtime.closed and runtime.events_ready.is_set())

    def _independent_session(self, ctx) -> bool:
        if ctx.btw:
            return False
        sdk = ctx.sdk
        if ctx.engine == "claude":
            from cc_remote.claude_service.client import RemoteClient
            from cc_remote.wrapper.sdk import SdkHandle

            if not isinstance(sdk, SdkHandle) or not isinstance(sdk.client, RemoteClient):
                return False
            client = sdk.client
            if (not client.id or not sdk.service_metadata
                    or sdk.service_metadata.get("service_id") != client.id
                    or client.connection.task is None or client.connection.task.done()
                    or sdk._control_request_lock.locked()):
                return False
            # An existing service worker is not proof that a newly scheduled
            # browser prompt has reached it. query() owns the launch lock until
            # accepted; its original turn identity also guards preflight.
            if ctx.turn_task is not None and not ctx.turn_task.done():
                return bool(sdk._turn_active and sdk._turn_root_id
                            and sdk._turn_root_id == (sdk.service_turn_metadata or {}).get("id"))
            return True
        if ctx.engine == "codex":
            from cc_remote.wrapper.codex_handle import CodexHandle

            if (ctx.space != "code" or not isinstance(sdk, CodexHandle)
                    or not sdk.using_daemon_proxy or not sdk.daemon_process_generation_current
                    or sdk.thread_id != ctx.session_id or sdk.turn_start_pending
                    or ctx.pending_asks
                    or any(not task.done() for task in sdk._server_request_tasks)):
                return False
            if (ctx.state != "idle" or sdk.turn_active
                    or any(task is not None and not task.done()
                           for task in (ctx.turn_task, ctx.codex_spontaneous_task))):
                # Restart recovery only reattaches a persisted exact owner.
                # A failed lease write must not turn a live response into an
                # unowned/background thread after the controller restarts.
                try:
                    lease = self.machine._codex_turn_leases.get(ctx.key)
                except (OSError, ValueError):
                    return False
                return bool(lease and sdk.turn_active and sdk.turn_id
                            and lease.turn_id == sdk.turn_id == ctx.codex_owned_turn_id
                            and lease.msg_id == ctx.active_msg_id == ctx.codex_owned_msg_id)
            return True
        if ctx.engine == "dsh":
            from cc_remote.wrapper.dsh_runtime import DshHandle

            return (isinstance(sdk, DshHandle) and sdk.connected and self._independent_dsh()
                    and not ctx.pending_asks)
        return False

    def guarded(self) -> bool:
        if self.root is not None:
            from cc_remote.activation import guarded
            try:
                if guarded(self.root, self.activation_admitted):
                    return True
            except (OSError, ValueError):
                # A corrupt receipt is not authority to disable all sessions.
                # The installer must obtain positive admission before stopping.
                pass
        if self.reserved is None:
            return False
        try:
            from cc_remote.activation import install_locked
            if not install_locked(self.root):
                self.reserved = None
                return False
            state = self.store().state(self.reserved)
            if state["phase"] in TERMINAL:
                self.reserved = None
                return False
        except (OSError, ValueError):
            pass
        return True

    def reserve_activation(self, identity: str) -> dict:
        """Common CLI/UI installer admission; no UI job or policy is required."""
        from cc_remote.activation import install_locked, receipt
        if self.root is None:
            return {"status": "rejected"}
        value = receipt(self.root)
        if (not value or value["id"] != job_id(identity)
                or value["phase"] not in {"waiting", "quiesced"}
                or not install_locked(self.root)):
            return {"status": "rejected"}
        if self.busy_count():
            return {"status": "busy"}
        self.activation_admitted = identity
        return {"status": "ready"}

    async def reject(self, cmd):
        if cmd.type == "collaboration" and cmd.operation in {"list", "read", "catalog", "sessions", "reconcile", "stop", "answer"}:
            return None
        if (not self.guarded() or cmd.type.startswith(("get_", "list_", "read_", "browse_"))
                or cmd.type in {"hello", "ping", "interrupt", "cancel_queued_query", "answer_question",
                                "acknowledge_completion"}):
            return None
        sid = getattr(cmd, "session_id", None) or getattr(cmd, "sid", None) or self.machine.focused_sid
        error = Error(code="update_in_progress", message="设备正在安装更新，请在重新连接后继续。",
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
                  "busy_count": self.busy_count(), "latest": self.catalog, "job": None,
                  "maintenance": self.guarded()}
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
                result["job"] = await self._public_state(state)
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
        except (OSError, ValueError) as exc:
            result["reason"] = failure_reason(exc)
        result["maintenance"] = self.guarded()
        return result

    async def start(self):
        if self.root is None:
            return
        self.restore_reservation()
        self.task = asyncio.create_task(self._connect())
        self.recovery_task = asyncio.create_task(self._recover_pending())

    async def close(self):
        if self.recovery_task:
            self.recovery_task.cancel()
            await asyncio.gather(self.recovery_task, return_exceptions=True)
            self.recovery_task = None
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
