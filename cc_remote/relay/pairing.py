"""Relay hub: pairs one or more named wrappers with their clients, routes replay, fans
out live events.

- Each ``machine_id`` has one wrapper slot. A duplicate wrapper for the same id
  is rejected while different self-hosted machines may share the relay.
- Clients register by `client_id` (from their hello). A reconnecting phone
  reuses its client_id, replacing any stale connection.
- Wrapper frames with `to=<client_id>` are routed to that client only. Frames
  with `owner_id` are limited to connections for that authenticated account;
  frames with neither are broadcast to every client on the machine.
- wrapper hello announces (re)connection -> broadcast `wrapper_reconnected` so
  clients re-hello with per-session cursors and recover missing live tails.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import json
import re
import uuid
from typing import Awaitable, Callable, Optional

from fastapi import WebSocket, WebSocketDisconnect

from cc_remote import __version__
from cc_remote.config import RelayConfig
from cc_remote.log import logger
from cc_remote.protocol import (
    Error, ProtocolError, WrapperDisconnected, WrapperReconnected,
    PROTOCOL_VERSION, deserialize, is_client_message, serialize,
    ERR_BUSY, ERR_WRAPPER_OFFLINE, ERR_WRAPPER_ALREADY_CONNECTED, ERR_PROTOCOL,
)
from cc_remote.relay.forward import ClientConn, SlowClientError

log = logger("cc_remote.relay")

CLIENT_LIMIT_CLOSE_CODE = 4008
CLIENT_LIMIT_CLOSE_REASON = "client limit reached"
CLIENT_HELLO_TIMEOUT_CLOSE_CODE = 4002
CLIENT_HELLO_TIMEOUT_CLOSE_REASON = "client hello timeout"
PROTOCOL_MISMATCH_CLOSE_CODE = 4406
PROTOCOL_MISMATCH_CLOSE_REASON = "protocol upgrade required"
PROTOCOL_ERROR_CLOSE_CODE = 4400
PROTOCOL_ERROR_CLOSE_REASON = "invalid protocol frame"
MAX_NAMED_WRAPPERS = 256
WRAPPER_LIMIT_CLOSE_CODE = 1013
WRAPPER_LIMIT_CLOSE_REASON = "wrapper capacity reached"


WrapperEventHook = Callable[[str, object], Awaitable[None]]
WrapperAuthorizer = Callable[[str], Awaitable[bool]]


class RelayHub:
    def __init__(
        self,
        cfg: RelayConfig,
        *,
        on_live_turn_end: WrapperEventHook | None = None,
    ):
        self.cfg = cfg
        self._on_live_turn_end = on_live_turn_end
        self._event_tasks: set[asyncio.Task[None]] = set()
        # Preserve the original attributes as the default-machine fast path and
        # test/integration compatibility surface.
        self._wrapper_ws: Optional[WebSocket] = None
        self._clients: dict[str, ClientConn] = {}
        self._wrappers: dict[str, WebSocket] = {}
        self._machine_clients: dict[str, dict[str, ClientConn]] = {}
        self._client_slots: set[int] = set()
        self._lock = asyncio.Lock()
        # Linearizes client generation replacement with client -> wrapper sends.
        # Once a new generation owns client_id, no old generation can enter a
        # wrapper send after that point.
        self._wrapper_send_lock = asyncio.Lock()
        self._wrapper_versions: OrderedDict[str, dict] = OrderedDict()

    def remember_wrapper_version(self, machine_id: str, protocol: int, version: str | None) -> None:
        if not isinstance(version, str) or not re.fullmatch(r"\d{1,6}\.\d{1,6}\.\d{1,6}", version):
            version = None
        self._wrapper_versions[machine_id] = {
            "wrapper_version": version, "relay_version": __version__,
            "wrapper_protocol": protocol, "relay_protocol": PROTOCOL_VERSION,
        }
        self._wrapper_versions.move_to_end(machine_id)
        while len(self._wrapper_versions) > MAX_NAMED_WRAPPERS + 1:
            self._wrapper_versions.popitem(last=False)

    def device_version(self, machine_id: str) -> dict:
        info = self._wrapper_versions.get(machine_id)
        if info and (info["wrapper_protocol"] != PROTOCOL_VERSION
                     or info["wrapper_version"] not in (None, __version__)):
            return {"compatibility": dict(info)}
        return {}

    @property
    def versioned_machine_ids(self) -> tuple[str, ...]:
        return tuple(self._wrapper_versions)

    def forget_wrapper_version(self, machine_id: str) -> None:
        self._wrapper_versions.pop(machine_id, None)

    @property
    def wrapper_connected(self) -> bool:
        return self._wrapper_ws is not None or bool(self._wrappers)

    @property
    def machine_ids(self) -> list[str]:
        ids = set(self._wrappers)
        if self._wrapper_ws is not None:
            ids.add("default")
        return sorted(ids)

    @property
    def client_count(self) -> int:
        return len(self._client_slots)

    def default_machine_id(self, requested: str | None = None) -> str:
        if requested:
            return requested
        ids = self.machine_ids
        return ids[0] if ids else "default"

    def _wrapper_for(self, machine_id: str):
        return (self._wrapper_ws if machine_id == "default"
                else self._wrappers.get(machine_id))

    async def disconnect_wrapper(
        self,
        machine_id: str,
        *,
        reason: str = "device disconnected",
    ) -> bool:
        """Close the current wrapper generation, if any.

        Device revocation uses this to make credential removal effective now
        rather than after the wrapper's next reconnect.
        """
        async with self._lock:
            wrapper = self._wrapper_for(machine_id)
        if wrapper is None:
            return False
        try:
            await wrapper.close(code=1008, reason=reason[:123])
        except Exception:
            pass
        return True

    def _clients_for(self, machine_id: str) -> dict[str, ClientConn]:
        if machine_id == "default":
            return self._clients
        return self._machine_clients.get(machine_id, {})

    def _ensure_clients_for(self, machine_id: str) -> dict[str, ClientConn]:
        if machine_id == "default":
            return self._clients
        return self._machine_clients.setdefault(machine_id, {})

    def _prune_clients_for(
        self,
        machine_id: str,
        clients: dict[str, ClientConn],
    ) -> None:
        if (
            machine_id != "default"
            and not clients
            and self._machine_clients.get(machine_id) is clients
        ):
            del self._machine_clients[machine_id]

    def _event_task_done(self, task: asyncio.Task[None]) -> None:
        self._event_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("relay event hook failed")

    # ---- wrapper side ----

    async def serve_wrapper(
        self,
        ws: WebSocket,
        expected_machine_id: str | None = None,
        authorize_machine: WrapperAuthorizer | None = None,
    ) -> None:
        announced = False
        machine_id: str | None = None
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = deserialize(raw)
                except ProtocolError as e:
                    log.warning("bad frame from wrapper", error=str(e))
                    mismatch = "protocol version mismatch" in str(e)
                    if mismatch and not announced:
                        # Version diagnostics must remain readable even when
                        # strict wire decoding rejects an old peer. Only an
                        # authenticated, correctly scoped hello can publish it.
                        try:
                            hello = json.loads(raw)
                            peer_id = hello.get("machine_id") or "default"
                            peer_version = hello.get("v")
                            valid = (
                                hello.get("type") == "hello" and hello.get("role") == "wrapper"
                                and isinstance(peer_id, str)
                                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}", peer_id)
                                and type(peer_version) is int and 0 < peer_version < 1000000
                                and expected_machine_id in (None, peer_id)
                            )
                            if (valid and (authorize_machine is None or await authorize_machine(peer_id))
                                    and self._wrapper_for(peer_id) is None):
                                self.remember_wrapper_version(peer_id, peer_version,
                                                              getattr(ws, "headers", {}).get("x-cc-remote-version"))
                        except (ValueError, TypeError, AttributeError):
                            pass
                    try:
                        await ws.send_text(serialize(Error(
                            code=ERR_PROTOCOL,
                            message="wrapper protocol upgrade required"
                            if mismatch else "invalid wrapper protocol frame",
                        )))
                        await ws.close(
                            code=(PROTOCOL_MISMATCH_CLOSE_CODE
                                  if mismatch else PROTOCOL_ERROR_CLOSE_CODE),
                            reason=(PROTOCOL_MISMATCH_CLOSE_REASON
                                    if mismatch else PROTOCOL_ERROR_CLOSE_REASON),
                        )
                    except Exception:
                        pass
                    break
                if not announced:
                    if msg.type != "hello" or getattr(msg, "role", None) != "wrapper":
                        try:
                            await ws.send_text(serialize(Error(
                                code=ERR_PROTOCOL,
                                message="first frame must be a wrapper hello",
                            )))
                            await ws.close(
                                code=PROTOCOL_ERROR_CLOSE_CODE,
                                reason=PROTOCOL_ERROR_CLOSE_REASON,
                            )
                        except Exception:
                            pass
                        break
                    machine_id = getattr(msg, "machine_id", None) or "default"
                    if (expected_machine_id is not None
                            and machine_id != expected_machine_id):
                        try:
                            await ws.send_text(serialize(Error(
                                code=ERR_PROTOCOL,
                                message="wrapper credential is not valid for this machine",
                            )))
                            await ws.close(code=1008, reason="machine not authorized")
                        except Exception:
                            pass
                        return
                    async with self._lock:
                        current = self._wrapper_for(machine_id)
                        over_capacity = (
                            current is None
                            and machine_id != "default"
                            and len(self._wrappers) >= MAX_NAMED_WRAPPERS
                        )
                        if current is None and not over_capacity:
                            if machine_id == "default":
                                self._wrapper_ws = ws
                            else:
                                self._wrappers[machine_id] = ws
                    if over_capacity:
                        try:
                            await ws.send_text(serialize(Error(
                                code=ERR_BUSY,
                                message=WRAPPER_LIMIT_CLOSE_REASON,
                            )))
                            await ws.close(
                                code=WRAPPER_LIMIT_CLOSE_CODE,
                                reason=WRAPPER_LIMIT_CLOSE_REASON,
                            )
                        except Exception:
                            pass
                        return
                    if current is not None:
                        try:
                            await ws.send_text(serialize(Error(
                                code=ERR_WRAPPER_ALREADY_CONNECTED,
                                message=f"wrapper {machine_id!r} is already connected",
                            )))
                            await ws.close(code=1008)
                        except Exception:
                            pass
                        return
                    # The WebSocket upgrade and wrapper hello are separate
                    # protocol steps. Reserve the route first so a concurrent
                    # revoke can find and close this socket, then re-check a
                    # dynamic credential before exposing the wrapper as live.
                    announced = True
                    if (authorize_machine is not None
                            and not await authorize_machine(machine_id)):
                        try:
                            await ws.send_text(serialize(Error(
                                code=ERR_PROTOCOL,
                                message="wrapper credential has been revoked",
                            )))
                            await ws.close(code=1008, reason="device revoked")
                        except Exception:
                            pass
                        return
                    log.info("wrapper connected", machine_id=machine_id)
                    self.remember_wrapper_version(machine_id, PROTOCOL_VERSION,
                                                  getattr(ws, "headers", {}).get("x-cc-remote-version"))
                assert machine_id is not None
                await self._on_wrapper_msg(msg, machine_id)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("wrapper loop error")
        finally:
            if announced and machine_id is not None:
                await self._wrapper_gone(machine_id, ws)

    async def _on_wrapper_msg(self, msg, machine_id: str = "default") -> None:
        if msg.type == "hello" and getattr(msg, "role", None) == "wrapper":
            log.info("wrapper announced", cc_session_id=msg.cc_session_id,
                     state=msg.state, head=msg.buffer_head_seq, tail=msg.buffer_tail_seq)
            await self._broadcast(WrapperReconnected(
                cc_session_id=msg.cc_session_id,
                state=msg.state or "idle",
                generation=getattr(msg, "wrapper_generation", None),
            ), machine_id)
            return
        to = getattr(msg, "to", None)
        owner_id = getattr(msg, "owner_id", None)
        if to:
            async with self._lock:
                conn = self._clients_for(machine_id).get(to)
            route_id = getattr(msg, "route_id", None)
            if (conn is not None and owner_id is not None
                    and conn.owner_id != owner_id):
                log.warning(
                    "owner-mismatched routed frame dropped",
                    to=to,
                    type=msg.type,
                )
                return
            if (conn is not None and route_id is not None
                    and conn.route_id != route_id):
                log.debug(
                    "stale connection-routed frame dropped",
                    to=to,
                    type=msg.type,
                )
                return
            if conn is not None:
                try:
                    await conn.send(msg)
                except SlowClientError:
                    await self._drop_client(conn, code=4008, reason="slow client",
                                            machine_id=machine_id)
                except ConnectionError:
                    await self._drop_client(conn, machine_id=machine_id)
            else:
                log.debug("routed frame for unknown client, dropping", to=to, type=msg.type)
        elif owner_id:
            await self._broadcast_owner(msg, owner_id, machine_id)
        else:
            await self._broadcast(msg, machine_id)
        # Replay is routed to one client via ``to`` and must never generate a
        # second notification. Only a new live completion reaches this hook.
        if (
            msg.type == "turn_end"
            and not to
            and not owner_id
            and getattr(msg, "notification_context", None) is not None
            and self._on_live_turn_end is not None
        ):
            task = asyncio.create_task(self._on_live_turn_end(machine_id, msg))
            self._event_tasks.add(task)
            task.add_done_callback(self._event_task_done)

    async def _wrapper_gone(self, machine_id: str = "default", ws=None) -> None:
        async with self._lock:
            current = self._wrapper_for(machine_id)
            if ws is not None and current is not ws:
                return
            if machine_id == "default":
                self._wrapper_ws = None
            else:
                self._wrappers.pop(machine_id, None)
        log.warning("wrapper disconnected", machine_id=machine_id)
        await self._broadcast(WrapperDisconnected(), machine_id)

    # ---- client side ----

    async def serve_client(self, ws: WebSocket,
                           machine_id: str = "default",
                           owner_id: str | None = None) -> None:
        conn: Optional[ClientConn] = None
        client_id: Optional[str] = None
        slot = id(ws)
        max_clients = max(1, getattr(self.cfg, "max_clients", 8))
        # One bounded probe slot lets a reconnect present its client_id and
        # replace a half-open old generation even when active capacity is full.
        async with self._lock:
            if len(self._client_slots) >= max_clients + 1:
                admitted = False
            else:
                self._client_slots.add(slot)
                admitted = True
        if not admitted:
            await ws.close(code=CLIENT_LIMIT_CLOSE_CODE, reason=CLIENT_LIMIT_CLOSE_REASON)
            return

        try:
            try:
                raw = await asyncio.wait_for(
                    ws.receive_text(),
                    timeout=max(0.01, getattr(self.cfg, "client_hello_timeout", 10.0)),
                )
            except asyncio.TimeoutError:
                await ws.close(
                    code=CLIENT_HELLO_TIMEOUT_CLOSE_CODE,
                    reason=CLIENT_HELLO_TIMEOUT_CLOSE_REASON,
                )
                return

            try:
                msg = deserialize(raw)
            except ProtocolError as e:
                log.warning("bad first frame from client", error=str(e))
                await ws.send_text(serialize(Error(
                    code=ERR_PROTOCOL,
                    message="first frame must be a client hello",
                )))
                mismatch = "protocol version mismatch" in str(e)
                await ws.close(
                    code=(PROTOCOL_MISMATCH_CLOSE_CODE
                          if mismatch else PROTOCOL_ERROR_CLOSE_CODE),
                    reason=(PROTOCOL_MISMATCH_CLOSE_REASON
                            if mismatch else PROTOCOL_ERROR_CLOSE_REASON),
                )
                return
            if msg.type != "hello" or getattr(msg, "role", None) != "client":
                await ws.send_text(serialize(Error(
                    code=ERR_PROTOCOL,
                    message="first frame must be a client hello",
                )))
                await ws.close(
                    code=PROTOCOL_ERROR_CLOSE_CODE,
                    reason=PROTOCOL_ERROR_CLOSE_REASON,
                )
                return

            client_id = msg.client_id or uuid.uuid4().hex
            msg.client_id = client_id
            conn = ClientConn(
                ws, self.cfg.client_queue_cap, client_id,
                getattr(self.cfg, "client_queue_bytes", 16 * 1024 * 1024),
                owner_id,
            )
            conn.start()
            # Never trust a client-supplied routing generation. It belongs to
            # this accepted WebSocket and is meaningful only inside the relay.
            msg.route_id = conn.route_id
            msg.owner_id = conn.owner_id
            over_capacity = False
            async with self._wrapper_send_lock:
                async with self._lock:
                    clients = self._clients_for(machine_id)
                    old = clients.get(client_id)
                    if old is None and sum(
                        len(group) for group in [self._clients, *self._machine_clients.values()]
                    ) >= max_clients:
                        over_capacity = True
                    else:
                        self._ensure_clients_for(machine_id)[client_id] = conn
            if over_capacity:
                await conn.stop(
                    code=CLIENT_LIMIT_CLOSE_CODE,
                    reason=CLIENT_LIMIT_CLOSE_REASON,
                )
                return
            if old is not None and old is not conn:
                await old.stop(code=4009, reason="replaced by reconnect")
            log.info("client registered", client_id=client_id,
                     machine_id=machine_id, total=self.client_count)

            if not await self._forward_client_msg(
                    conn, client_id, msg, machine_id):
                return

            while True:
                raw = await ws.receive_text()
                try:
                    msg = deserialize(raw)
                except ProtocolError as e:
                    log.warning("bad frame from client", error=str(e))
                    await conn.stop(
                        code=PROTOCOL_ERROR_CLOSE_CODE,
                        reason=PROTOCOL_ERROR_CLOSE_REASON,
                    )
                    break
                if not is_client_message(msg):
                    log.warning("wrapper-only frame from client rejected",
                                type=msg.type, client_id=client_id)
                    await conn.stop(
                        code=PROTOCOL_ERROR_CLOSE_CODE,
                        reason=PROTOCOL_ERROR_CLOSE_REASON,
                    )
                    break
                if hasattr(msg, "client_id"):
                    msg.client_id = client_id
                # Both routing identities are relay authority. Never allow a
                # browser frame to select another account's private forks.
                msg.owner_id = conn.owner_id
                # route_id is reserved for the first Hello catch-up response.
                msg.route_id = None
                if not await self._forward_client_msg(
                        conn, client_id, msg, machine_id):
                    break
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("client loop error")
        finally:
            if client_id is not None and conn is not None:
                async with self._lock:
                    clients = self._clients_for(machine_id)
                    if clients.get(client_id) is conn:
                        del clients[client_id]
                        self._prune_clients_for(machine_id, clients)
                await conn.stop()
            async with self._lock:
                self._client_slots.discard(slot)
            log.info("client removed", client_id=client_id,
                     machine_id=machine_id, remaining=self.client_count)

    async def _forward_client_msg(
        self, conn: ClientConn, client_id: str, msg,
        machine_id: str = "default",
    ) -> bool:
        """Forward iff ``conn`` still owns client_id at the send linearization point."""
        async with self._wrapper_send_lock:
            async with self._lock:
                current = self._clients_for(machine_id).get(client_id) is conn
                wrapper = self._wrapper_for(machine_id)
            if not current:
                return False
            if wrapper is None:
                try:
                    await conn.send(Error(
                        code=ERR_WRAPPER_OFFLINE,
                        message="wrapper is not connected",
                        request_id=getattr(msg, "request_id", None),
                        msg_id=getattr(msg, "msg_id", None),
                    ))
                except (SlowClientError, ConnectionError):
                    return False
                return True
            try:
                await wrapper.send_text(serialize(msg))
            except Exception as e:
                log.warning("forward to wrapper failed", error=str(e))
                try:
                    await conn.send(Error(
                        code=ERR_WRAPPER_OFFLINE,
                        message="wrapper link broken",
                        request_id=getattr(msg, "request_id", None),
                        msg_id=getattr(msg, "msg_id", None),
                    ))
                except (SlowClientError, ConnectionError):
                    return False
            return True

    # ---- broadcast ----

    async def _broadcast(self, msg, machine_id: str = "default") -> None:
        async with self._lock:
            conns = list(self._clients_for(machine_id).values())
        dead: list[ClientConn] = []
        for c in conns:
            try:
                await c.send(msg)
            except (SlowClientError, ConnectionError):
                dead.append(c)
        for c in dead:
            await self._drop_client(
                c, code=4008, reason="slow client", machine_id=machine_id)

    async def _broadcast_owner(
        self, msg, owner_id: str, machine_id: str = "default",
    ) -> None:
        async with self._lock:
            conns = [
                conn for conn in self._clients_for(machine_id).values()
                if conn.owner_id == owner_id
            ]
        dead: list[ClientConn] = []
        for conn in conns:
            try:
                await conn.send(msg)
            except (SlowClientError, ConnectionError):
                dead.append(conn)
        for conn in dead:
            await self._drop_client(
                conn, code=4008, reason="slow client", machine_id=machine_id)

    async def _drop_client(self, conn: ClientConn, *, code: int | None = None,
                           reason: str = "", machine_id: str = "default") -> None:
        async with self._lock:
            clients = self._clients_for(machine_id)
            if clients.get(conn.client_id) is conn:
                del clients[conn.client_id]
                self._prune_clients_for(machine_id, clients)
        await conn.stop(code=code, reason=reason)
