"""Small authenticated maintenance channel, independent of the chat wire version."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import time
from uuid import uuid4
import weakref
from starlette.websockets import WebSocketDisconnect

from cc_remote.config import valid_machine_id
from cc_remote.update import UpdateError, version_tuple
from cc_remote.update_jobs import TERMINAL, failure_reason, public_job

MAX_FRAME = 65536


class UpdateChannelError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class Peer:
    socket: object
    authorized: object = None
    pending: dict = field(default_factory=dict)


class UpdateRelay:
    def __init__(self, devices=None, server_updates=None):
        self.peers: dict[str, Peer] = {}
        from cc_remote.relay.update_queue import DeviceUpdateQueue
        self.queue = DeviceUpdateQueue(devices) if devices is not None else None
        self.server_updates = server_updates
        self.coordinators = {}
        self.coordination_lock = asyncio.Lock()
        self.locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    async def request(self, machine: str, command: dict) -> dict:
        if self.queue is None:
            return await self._request(machine, command)
        lock = self.locks.setdefault(machine, asyncio.Lock())
        async with lock:
            try:
                if self.server_updates is not None and command["op"] in {"status", "check"}:
                    catalog = await self.server_updates.status(check=command["op"] == "check")
                    result = await self._queued_request(machine, {"op": "status"} if catalog.get("latest") else command)
                    if catalog.get("latest"):
                        result["latest"] = catalog["latest"]
                    if catalog.get("catalog_error") and (catalog.get("latest") or not result.get("latest")):
                        result["reason"] = result.get("reason") or catalog["catalog_error"]
                    elif catalog.get("latest") and result.get("enabled") and result.get("reason") in {
                            "network", "rate_limited", "update_failed"}:
                        result["reason"] = None
                    return result
                return await self._queued_request(machine, command)
            except UpdateError as exc:
                return {"ok": False, "reason": failure_reason(exc)}

    async def _queued_request(self, machine: str, command: dict) -> dict:
        operation = command["op"]
        if operation in {"start", "upgrade"}:
            if machine in self.peers:
                known = await self._request(machine, {"op": "status"})
                await asyncio.to_thread(self.queue.remember, machine, known)
            if operation == "upgrade" and self.server_updates is None:
                return {"ok": False, "reason": "unsupported"}
            state = await asyncio.to_thread(self.queue.create, machine, command["job_id"], command["version"],
                                            coordinated=operation == "upgrade")
            if state.get("coordinated"):
                self.wake_coordinator()
                return {"ok": True, "job": public_job(state)}
            if state["phase"] in TERMINAL or machine not in self.peers:
                return {"ok": True, "job": public_job(state)}
            try:
                return await self._forward_intent(machine, state)
            except UpdateChannelError:
                state = await asyncio.to_thread(self.queue.job, machine, state['id'])
                return {'ok': True, 'job': public_job(state)}
        if operation == "cancel":
            state = await asyncio.to_thread(self.queue.cancel, machine, command["job_id"])
            if state and state.get("coordinated") and not state["delivered"]:
                return {"ok": True, "job": public_job(state)}
            if state and (state["phase"] in TERMINAL or machine not in self.peers):
                return {"ok": True, "job": public_job(state)}
            if state:
                return await self._forward_intent(machine, state)
        if operation == "lookup":
            state = await asyncio.to_thread(self.queue.job, machine, command["job_id"])
            if state and state.get("coordinated") and not state["delivered"]:
                return {"ok": True, "job_id": state["id"], "receipt": "found", "job": public_job(state)}
        if operation == "reconcile":
            state = await asyncio.to_thread(self.queue.job, machine, command["job_id"])
            if (state and state.get("coordinated") and state["phase"] not in TERMINAL
                    and state.get("server_phase") == "unknown"):
                async with self.coordination_lock:
                    blocking_id = state.get("blocking_server_job_id")
                    if blocking_id is None:
                        # Older queue records only retain the requested version's
                        # id. Recover the actual in-flight receipt before acting.
                        active = (await self.server_updates.status()).get("job")
                        if active and active["phase"] not in TERMINAL:
                            blocking_id = active["id"]
                            state = {**state, "blocking_server_job_id": blocking_id}
                            await asyncio.to_thread(self.queue.save, machine, state)
                    if blocking_id is not None:
                        result = await self.server_updates.handle({"op": "reconcile", "job_id": blocking_id})
                        if result.get("ok") is not True:
                            return {"ok": False, "reason": result.get("reason") or "outcome_pending",
                                    "job": public_job(state)}
                        if (result.get("job") or {}).get("id") != blocking_id:
                            return {"ok": False, "reason": "outcome_pending", "job": public_job(state)}
                await self._advance_upgrade(machine, state["id"])
                return {"ok": True, "job": public_job(await asyncio.to_thread(self.queue.job, machine, state["id"]))}
        if machine not in self.peers:
            if operation == "lookup":
                state = await asyncio.to_thread(self.queue.job, machine, command["job_id"])
                return {"ok": True, "job_id": command["job_id"], "receipt": "found" if state else "missing",
                        "job": public_job(state)}
            known = await asyncio.to_thread(self.queue.status, machine)
            if known is None:
                raise UpdateChannelError("device_unavailable")
            state = await asyncio.to_thread(self.queue.active, machine)
            if operation == "check":
                from cc_remote.installation_updates import release_catalog
                known["latest"] = await asyncio.to_thread(release_catalog)
            return {**known, "online": False, "maintenance": False, "job": public_job(state)}
        result = await self._request(machine, command)
        if operation == 'lookup' and result.get('receipt') == 'missing':
            state = await asyncio.to_thread(self.queue.job, machine, command['job_id'])
            if state:
                return {'ok': True, 'job_id': state['id'], 'receipt': 'found', 'job': public_job(state)}
        # Only the device's raw flag acknowledges a forwarded cancellation.
        current = result.get('job')
        if isinstance(current, dict):
            observed = await asyncio.to_thread(self.queue.observe, machine, current)
            if observed:
                result = {**result, 'job': public_job(observed)}
        if operation in {"status", "check"}:
            await asyncio.to_thread(self.queue.remember, machine, result)
            queued = await asyncio.to_thread(self.queue.active, machine)
            if queued and queued.get("coordinated") and (
                    not queued["delivered"] or queued["phase"] == "blocked"):
                return {**result, "job": public_job(queued), "online": True}
            if queued and queued["phase"] not in TERMINAL and (
                not current or current.get('id') != queued['id']
                or (queued.get('cancel_requested') and not current.get('cancel_requested'))
            ):
                # A start or cancellation can time out while the socket remains
                # connected. Only a previously accepted intent is forwarded,
                # retaining its exact identity and the device's durable fence.
                reply = await self._forward_intent(machine, queued)
                if not current or current.get('id') == queued['id'] or current.get('phase') in TERMINAL:
                    result = {**result, 'job': reply.get('job')}
        return {**result, "online": True}

    async def _forward_intent(self, machine: str, state: dict) -> dict:
        if state.get("coordinated") and state.get("server_phase") != "succeeded":
            return {"ok": True, "job": public_job(state)}
        identity = state['id']
        cancel = state.get('cancel_requested')
        command = ({'op': 'cancel', 'job_id': identity} if cancel else
                   {'op': 'start', 'job_id': identity, 'version': state['version']})
        await asyncio.to_thread(self.queue.save, machine, {**state, 'delivered': True})
        try:
            result = await self._request(machine, command)
        except UpdateChannelError:
            await asyncio.to_thread(self.queue.save, machine,
                                    {**state, 'delivered': True, 'phase': 'unknown', 'reason': 'outcome_pending'})
            raise
        if cancel and result.get('ok') is True and result.get('job') is None:
            result = {**result, 'job': {**public_job(state), 'phase': 'cancelled', 'reason': None}}
        if isinstance(result.get('job'), dict):
            observed = await asyncio.to_thread(self.queue.observe, machine, result['job'])
            if observed:
                result = {**result, 'job': public_job(observed)}
        elif result.get('ok') is False:
            state = {**state, 'phase': 'unknown' if cancel else 'blocked', 'delivered': True,
                     'reason': result.get('reason'), 'updated_at': time.time()}
            await asyncio.to_thread(self.queue.save, machine, state)
            result = {**result, 'job': public_job(state)}
        return result

    async def _deliver(self, machine: str, peer: Peer) -> None:
        if self.queue is None:
            return
        lock = self.locks.setdefault(machine, asyncio.Lock())
        async with lock:
            state = await asyncio.to_thread(self.queue.active, machine)
            if not state or state["phase"] in TERMINAL or self.peers.get(machine) is not peer:
                return
            if state.get("coordinated"):
                self.wake_coordinator()
                return
            try:
                await self._forward_intent(machine, state)
            except (UpdateChannelError, OSError, ValueError):
                pass

    def wake_coordinator(self) -> None:
        loop = asyncio.get_running_loop()
        coordinator = self.coordinators.get(loop)
        if self.server_updates is not None and self.queue is not None and (
                coordinator is None or coordinator.done()):
            self.coordinators[loop] = asyncio.create_task(self._coordinate())

    async def start(self) -> None:
        self.wake_coordinator()

    async def close(self) -> None:
        coordinator = self.coordinators.pop(asyncio.get_running_loop(), None)
        if coordinator is not None:
            coordinator.cancel()
            await asyncio.gather(coordinator, return_exceptions=True)

    async def _coordinate(self) -> None:
        async def advance(machine, identity):
            lock = self.locks.setdefault(machine, asyncio.Lock())
            async with lock:
                try:
                    await self._advance_upgrade(machine, identity)
                except (UpdateChannelError, OSError, ValueError):
                    # A transient control failure keeps its original ids.
                    pass

        while True:
            pending = await asyncio.to_thread(self.queue.coordinated_pending)
            await asyncio.gather(*(advance(machine, state["id"]) for machine, state in pending))
            await asyncio.sleep(2)

    async def _advance_upgrade(self, machine: str, identity: str) -> None:
        state = await asyncio.to_thread(self.queue.job, machine, identity)
        if not state or state["phase"] in TERMINAL:
            return
        if state.get("server_phase") != "succeeded":
            # Serialize the shared server handoff across every device. Its own
            # OS receipt remains authoritative if Relay restarts during launch.
            async with self.coordination_lock:
                server = await self.server_updates.status()
                active = server.get("job")
                if active and active["phase"] not in TERMINAL:
                    shared = {"server_job_id": active["id"]} if active["version"] == state["version"] else {}
                    await asyncio.to_thread(self.queue.save, machine, {**state,
                        **shared, "phase": "unknown" if active["phase"] == "unknown" else "updating_relay",
                        "server_phase": active["phase"], "blocking_server_job_id": active["id"],
                        "reason": active.get("reason")})
                    return
                if state.get("blocking_server_job_id") is not None:
                    state = {**state, "blocking_server_job_id": None}
                    await asyncio.to_thread(self.queue.save, machine, state)
                if version_tuple(server.get("current", "0.0.0")) < version_tuple(state["version"]):
                    if not server.get("managed", True):
                        await asyncio.to_thread(self.queue.save, machine, {**state,
                            "phase": "blocked", "reason": server.get("reason") or "unsupported"})
                        return
                    reply = await self.server_updates.handle({"op": "lookup", "job_id": state["server_job_id"]})
                    job = reply.get("job")
                    if job is None:
                        if server.get("enabled"):
                            reply = await self.server_updates.handle({"op": "start",
                                "job_id": state["server_job_id"], "version": state["version"]})
                            job = reply.get("job")
                            if job is None:
                                if reply.get("reason") in {"outcome_pending", "busy"}:
                                    return
                                await asyncio.to_thread(self.queue.save, machine, {**state,
                                    "phase": "blocked", "reason": reply.get("reason") or "server_setup_required"})
                                return
                        else:
                            await asyncio.to_thread(self.queue.save, machine, {**state,
                                "phase": "blocked", "reason": server.get("reason") or "server_setup_required"})
                            return
                    owned = job["id"] == state["server_job_id"]
                    if job["phase"] in TERMINAL and owned:
                        await asyncio.to_thread(self.queue.save, machine, {**state, "phase": "blocked",
                            "server_phase": job["phase"], "reason": job.get("reason") or "relay_unconfirmed"})
                    else:
                        await asyncio.to_thread(self.queue.save, machine, {**state,
                            "phase": "unknown" if job["phase"] == "unknown" else "updating_relay",
                            "server_phase": job["phase"], "blocking_server_job_id": job["id"],
                            "reason": job.get("reason")})
                    return
                state = {**state, "server_phase": "succeeded", "phase": "waiting_device", "reason": None}
                await asyncio.to_thread(self.queue.save, machine, state)
        if machine not in self.peers:
            return
        current = await self._request(machine, {"op": "status"})
        if not state["delivered"]:
            if version_tuple(current.get("current", "0.0.0")) >= version_tuple(state["version"]):
                await asyncio.to_thread(self.queue.save, machine, {**state, "phase": "succeeded", "reason": None})
                return
        device_job = current.get("job")
        if device_job and device_job.get("id") == identity and not state.get("cancel_requested"):
            await asyncio.to_thread(self.queue.observe, machine, device_job)
            return
        await self._forward_intent(machine, state)

    async def _request(self, machine: str, command: dict) -> dict:
        peer = self.peers.get(machine)
        if peer is None:
            raise UpdateChannelError("device_unavailable")
        if peer.authorized is not None and not await peer.authorized(machine):
            raise UpdateChannelError("device_unavailable")
        if len(peer.pending) >= 8:
            raise UpdateChannelError("busy")
        identity = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        peer.pending[identity] = future
        try:
            await peer.socket.send_text(json.dumps({"schema": 1, "id": identity, **command}))
            return await asyncio.wait_for(future, 100 if command["op"] == "authorize" else 30)
        except (asyncio.TimeoutError, OSError, RuntimeError):
            raise UpdateChannelError("outcome_pending") from None
        finally:
            peer.pending.pop(identity, None)

    async def serve(self, socket, scope: str, authorized) -> None:
        await socket.accept()
        peer = Peer(socket, authorized)
        machine = None
        delivery = None
        try:
            async with asyncio.timeout(10):
                raw = await socket.receive_text()
            if len(raw) > 1024:
                raise ValueError("oversized maintenance hello")
            hello = json.loads(raw)
            if (not isinstance(hello, dict) or set(hello) != {"schema", "machine_id"}
                    or type(hello["schema"]) is not int or hello["schema"] != 1
                    or not isinstance(hello["machine_id"], str)
                    or not valid_machine_id(hello["machine_id"])):
                raise ValueError("invalid maintenance hello")
            machine = hello["machine_id"]
            if scope not in {"*", machine} or not await authorized(machine):
                raise ValueError("device not authorized")
            if machine not in self.peers and len(self.peers) >= 128:
                raise ValueError("maintenance connection limit")
            previous = self.peers.get(machine)
            self.peers[machine] = peer
            if previous:
                await previous.socket.close(code=1012)
            await socket.send_text(json.dumps({"schema": 1, "ready": True}))
            delivery = asyncio.create_task(self._deliver(machine, peer))
            while True:
                raw = await socket.receive_text()
                if len(raw) > MAX_FRAME or not await authorized(machine):
                    raise ValueError("maintenance response unavailable")
                frame = json.loads(raw)
                if (not isinstance(frame, dict) or set(frame) != {"schema", "id", "result"}
                        or type(frame["schema"]) is not int or frame["schema"] != 1
                        or not isinstance(frame["id"], str) or not isinstance(frame["result"], dict)):
                    raise ValueError("invalid maintenance response")
                future = peer.pending.get(frame["id"])
                if future and not future.done():
                    future.set_result(frame["result"])
        except (ValueError, KeyError, TypeError, OSError, RuntimeError, asyncio.TimeoutError, WebSocketDisconnect):
            pass
        finally:
            if delivery is not None:
                delivery.cancel()
                await asyncio.gather(delivery, return_exceptions=True)
            if machine and self.peers.get(machine) is peer:
                self.peers.pop(machine, None)
            for future in peer.pending.values():
                if not future.done():
                    future.set_exception(UpdateChannelError("outcome_pending"))
            try:
                await socket.close()
            except (OSError, RuntimeError):
                pass
