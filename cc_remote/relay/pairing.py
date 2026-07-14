"""Relay hub: pairs one wrapper with N clients, routes per-client replay, fans
out live events.

- Single wrapper slot. The first wrapper to authenticate occupies it; a second
  is rejected with `wrapper_already_connected`.
- Clients register by `client_id` (from their hello). A reconnecting phone
  reuses its client_id, replacing any stale connection.
- Wrapper frames with `to=<client_id>` are routed to that client only (per-
  client replay); frames without `to` are broadcast to every client.
- wrapper hello announces (re)connection -> broadcast `wrapper_reconnected` so
  clients re-hello with per-session cursors and recover missing live tails.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect

from cc_remote.config import RelayConfig
from cc_remote.log import logger
from cc_remote.protocol import (
    Error, ProtocolError, WrapperDisconnected, WrapperReconnected,
    deserialize, is_client_message, serialize,
    ERR_WRAPPER_OFFLINE, ERR_WRAPPER_ALREADY_CONNECTED, ERR_PROTOCOL,
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


class RelayHub:
    def __init__(self, cfg: RelayConfig):
        self.cfg = cfg
        self._wrapper_ws: Optional[WebSocket] = None
        self._clients: dict[str, ClientConn] = {}
        self._client_slots: set[int] = set()
        self._lock = asyncio.Lock()
        # Linearizes client generation replacement with client -> wrapper sends.
        # Once a new generation owns client_id, no old generation can enter a
        # wrapper send after that point.
        self._wrapper_send_lock = asyncio.Lock()

    @property
    def wrapper_connected(self) -> bool:
        return self._wrapper_ws is not None

    @property
    def client_count(self) -> int:
        return len(self._client_slots)

    # ---- wrapper side ----

    async def serve_wrapper(self, ws: WebSocket) -> None:
        async with self._lock:
            if self._wrapper_ws is not None:
                try:
                    await ws.send_text(serialize(Error(
                        code=ERR_WRAPPER_ALREADY_CONNECTED,
                        message="a wrapper is already connected",
                    )))
                    await ws.close(code=1008)
                except Exception:
                    pass
                return
            self._wrapper_ws = ws
        announced = False
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = deserialize(raw)
                except ProtocolError as e:
                    log.warning("bad frame from wrapper", error=str(e))
                    mismatch = "protocol version mismatch" in str(e)
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
                    announced = True
                    log.info("wrapper connected")
                await self._on_wrapper_msg(msg)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("wrapper loop error")
        finally:
            await self._wrapper_gone()

    async def _on_wrapper_msg(self, msg) -> None:
        if msg.type == "hello" and getattr(msg, "role", None) == "wrapper":
            log.info("wrapper announced", cc_session_id=msg.cc_session_id,
                     state=msg.state, head=msg.buffer_head_seq, tail=msg.buffer_tail_seq)
            await self._broadcast(WrapperReconnected(
                cc_session_id=msg.cc_session_id,
                state=msg.state or "idle",
                generation=getattr(msg, "wrapper_generation", None),
            ))
            return
        to = getattr(msg, "to", None)
        if to:
            async with self._lock:
                conn = self._clients.get(to)
            route_id = getattr(msg, "route_id", None)
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
                    await self._drop_client(conn, code=4008, reason="slow client")
                except ConnectionError:
                    await self._drop_client(conn)
            else:
                log.debug("routed frame for unknown client, dropping", to=to, type=msg.type)
        else:
            await self._broadcast(msg)

    async def _wrapper_gone(self) -> None:
        async with self._lock:
            self._wrapper_ws = None
        log.warning("wrapper disconnected")
        await self._broadcast(WrapperDisconnected())

    # ---- client side ----

    async def serve_client(self, ws: WebSocket) -> None:
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
            )
            conn.start()
            # Never trust a client-supplied routing generation. It belongs to
            # this accepted WebSocket and is meaningful only inside the relay.
            msg.route_id = conn.route_id
            over_capacity = False
            async with self._wrapper_send_lock:
                async with self._lock:
                    old = self._clients.get(client_id)
                    if old is None and len(self._clients) >= max_clients:
                        over_capacity = True
                    else:
                        self._clients[client_id] = conn
            if over_capacity:
                await conn.stop(
                    code=CLIENT_LIMIT_CLOSE_CODE,
                    reason=CLIENT_LIMIT_CLOSE_REASON,
                )
                return
            if old is not None and old is not conn:
                await old.stop(code=4009, reason="replaced by reconnect")
            log.info("client registered", client_id=client_id, total=len(self._clients))

            if not await self._forward_client_msg(conn, client_id, msg):
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
                # route_id is reserved for the first Hello catch-up response.
                msg.route_id = None
                if not await self._forward_client_msg(conn, client_id, msg):
                    break
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("client loop error")
        finally:
            if client_id is not None and conn is not None:
                async with self._lock:
                    if self._clients.get(client_id) is conn:
                        del self._clients[client_id]
                await conn.stop()
            async with self._lock:
                self._client_slots.discard(slot)
            log.info("client removed", client_id=client_id, remaining=len(self._clients))

    async def _forward_client_msg(self, conn: ClientConn, client_id: str, msg) -> bool:
        """Forward iff ``conn`` still owns client_id at the send linearization point."""
        async with self._wrapper_send_lock:
            async with self._lock:
                current = self._clients.get(client_id) is conn
                wrapper = self._wrapper_ws
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

    async def _broadcast(self, msg) -> None:
        async with self._lock:
            conns = list(self._clients.values())
        dead: list[ClientConn] = []
        for c in conns:
            try:
                await c.send(msg)
            except (SlowClientError, ConnectionError):
                dead.append(c)
        for c in dead:
            await self._drop_client(c, code=4008, reason="slow client")

    async def _drop_client(self, conn: ClientConn, *, code: int | None = None,
                           reason: str = "") -> None:
        async with self._lock:
            if self._clients.get(conn.client_id) is conn:
                del self._clients[conn.client_id]
        await conn.stop(code=code, reason=reason)
