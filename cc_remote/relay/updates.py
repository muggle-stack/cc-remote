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
from cc_remote.update import UpdateError
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
    def __init__(self, devices=None):
        self.peers: dict[str, Peer] = {}
        from cc_remote.relay.update_queue import DeviceUpdateQueue
        self.queue = DeviceUpdateQueue(devices) if devices is not None else None
        self.locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    async def request(self, machine: str, command: dict) -> dict:
        if self.queue is None:
            return await self._request(machine, command)
        lock = self.locks.setdefault(machine, asyncio.Lock())
        async with lock:
            try:
                return await self._queued_request(machine, command)
            except UpdateError as exc:
                return {"ok": False, "reason": failure_reason(exc)}

    async def _queued_request(self, machine: str, command: dict) -> dict:
        operation = command["op"]
        if operation == "start":
            if machine in self.peers:
                known = await self._request(machine, {"op": "status"})
                await asyncio.to_thread(self.queue.remember, machine, known)
            state = await asyncio.to_thread(self.queue.create, machine, command["job_id"], command["version"])
            if state["phase"] in TERMINAL or machine not in self.peers:
                return {"ok": True, "job": public_job(state)}
            try:
                return await self._forward_intent(machine, state)
            except UpdateChannelError:
                state = await asyncio.to_thread(self.queue.job, machine, state['id'])
                return {'ok': True, 'job': public_job(state)}
        if operation == "cancel":
            state = await asyncio.to_thread(self.queue.cancel, machine, command["job_id"])
            if state and (state["phase"] in TERMINAL or machine not in self.peers):
                return {"ok": True, "job": public_job(state)}
            if state:
                return await self._forward_intent(machine, state)
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
        if operation in {"status", "check"}:
            await asyncio.to_thread(self.queue.remember, machine, result)
            queued = await asyncio.to_thread(self.queue.active, machine)
            current = result.get('job')
            if queued and queued["phase"] not in TERMINAL and (not current or current.get('id') != queued['id']):
                # A start can time out while the maintenance socket remains
                # connected. Only a previously accepted intent is forwarded,
                # retaining its exact identity and the device's durable fence.
                reply = await self._forward_intent(machine, queued)
                if not current or current.get('phase') in TERMINAL:
                    result = {**result, 'job': reply.get('job')}
        if isinstance(result.get("job"), dict):
            await asyncio.to_thread(self.queue.observe, machine, result["job"])
        return {**result, "online": True}

    async def _forward_intent(self, machine: str, state: dict) -> dict:
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
            await asyncio.to_thread(self.queue.observe, machine, result['job'])
        elif result.get('ok') is False:
            state = {**state, 'phase': 'blocked', 'reason': result.get('reason'), 'updated_at': time.time()}
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
            try:
                await self._forward_intent(machine, state)
            except (UpdateChannelError, OSError, ValueError):
                pass

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
            return await asyncio.wait_for(future, 30)
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
