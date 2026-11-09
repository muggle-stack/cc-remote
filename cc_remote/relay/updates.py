"""Small authenticated maintenance channel, independent of the chat wire version."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
from uuid import uuid4
from starlette.websockets import WebSocketDisconnect

from cc_remote.config import valid_machine_id

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
    def __init__(self):
        self.peers: dict[str, Peer] = {}

    async def request(self, machine: str, command: dict) -> dict:
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
            if machine and self.peers.get(machine) is peer:
                self.peers.pop(machine, None)
            for future in peer.pending.values():
                if not future.done():
                    future.set_exception(UpdateChannelError("outcome_pending"))
            try:
                await socket.close()
            except (OSError, RuntimeError):
                pass
