"""FastAPI relay: /api/login (password -> session cookie), /ws (wrapper + web
clients), /healthz, optional static hosting of the web client from the same
origin.

Auth: wrapper authenticates with a Bearer WRAPPER_TOKEN header; web clients
authenticate with a Secure HttpOnly session cookie obtained from /api/login.
Cookie-authenticated WebSockets must also match PUBLIC_ORIGIN when configured.

The relay never imports claude_agent_sdk and never touches the model API.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse

from cc_remote.config import RelayConfig, relay_config, validate_relay_config
from cc_remote.log import logger
from cc_remote.relay.auth import (
    SESSION_COOKIE_NAME, SessionClaims, authenticate, make_session_token,
    session_token_claims, verify_password,
)
from cc_remote.relay.pairing import RelayHub

log = logger("cc_remote.relay.server")

_LOGIN_WINDOW = 60.0
_LOGIN_MAX = 5
_LOGIN_MAX_IPS = 4096
_LOGIN_MAX_TOTAL_ATTEMPTS = 16384
_LOGIN_CLEANUP_INTERVAL = 10.0
SESSION_EXPIRED_CLOSE_CODE = 1008
SESSION_EXPIRED_CLOSE_REASON = "session expired"
SESSION_REVOKED_CLOSE_CODE = 1008
SESSION_REVOKED_CLOSE_REASON = "session revoked"


class LoginRateLimiter:
    """Bounded per-IP login limiter with global stale-entry cleanup."""

    def __init__(
        self,
        *,
        window: float = _LOGIN_WINDOW,
        max_per_ip: int = _LOGIN_MAX,
        max_ips: int = _LOGIN_MAX_IPS,
        max_total_attempts: int = _LOGIN_MAX_TOTAL_ATTEMPTS,
        cleanup_interval: float = _LOGIN_CLEANUP_INTERVAL,
    ) -> None:
        self.window = window
        self.max_per_ip = max_per_ip
        self.max_ips = max_ips
        self.max_total_attempts = max_total_attempts
        self.cleanup_interval = cleanup_interval
        self._attempts: dict[str, list[float]] = defaultdict(list)
        self._total_attempts = 0
        self._last_cleanup = 0.0

    @property
    def key_count(self) -> int:
        return len(self._attempts)

    @property
    def total_attempts(self) -> int:
        return self._total_attempts

    def reset(self) -> None:
        self._attempts.clear()
        self._total_attempts = 0
        self._last_cleanup = 0.0

    def _cleanup(self, now: float) -> None:
        cutoff = now - self.window
        total = 0
        for ip in list(self._attempts):
            fresh = [attempt for attempt in self._attempts[ip] if attempt > cutoff]
            if fresh:
                self._attempts[ip] = fresh
                total += len(fresh)
            else:
                del self._attempts[ip]
        self._total_attempts = total
        self._last_cleanup = now

    def limited(self, ip: str, *, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        if current - self._last_cleanup >= self.cleanup_interval:
            self._cleanup(current)

        attempts = self._attempts.get(ip)
        if attempts is not None:
            cutoff = current - self.window
            fresh = [attempt for attempt in attempts if attempt > cutoff]
            self._total_attempts -= len(attempts) - len(fresh)
            if fresh:
                self._attempts[ip] = fresh
                attempts = fresh
            else:
                del self._attempts[ip]
                attempts = None

        if attempts is not None and len(attempts) >= self.max_per_ip:
            return True
        if attempts is None and len(self._attempts) >= self.max_ips:
            return True
        if self._total_attempts >= self.max_total_attempts:
            return True

        if attempts is None:
            attempts = []
            self._attempts[ip] = attempts
        attempts.append(current)
        self._total_attempts += 1
        return False


_login_limiter = LoginRateLimiter()


@dataclass
class _SessionEntry:
    expires_at: int
    revoked: asyncio.Event = field(default_factory=asyncio.Event)


class SessionRegistry:
    """Process-local revocation registry for signed browser sessions."""

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self._entries: dict[str, _SessionEntry] = {}
        self._lock = asyncio.Lock()

    def _prune_locked(self, now: float) -> None:
        for jti, entry in list(self._entries.items()):
            if entry.expires_at <= now:
                del self._entries[jti]

    async def register(self, claims: SessionClaims) -> bool:
        async with self._lock:
            self._prune_locked(time.time())
            if claims.jti in self._entries or len(self._entries) >= self.cap:
                return False
            self._entries[claims.jti] = _SessionEntry(claims.expires_at)
            return True

    async def active(self, claims: SessionClaims) -> bool:
        async with self._lock:
            self._prune_locked(time.time())
            entry = self._entries.get(claims.jti)
            return entry is not None and entry.expires_at == claims.expires_at

    async def subscribe(self, claims: SessionClaims) -> Optional[asyncio.Event]:
        async with self._lock:
            self._prune_locked(time.time())
            entry = self._entries.get(claims.jti)
            if entry is None or entry.expires_at != claims.expires_at:
                return None
            return entry.revoked

    async def revoke(self, jti: str) -> bool:
        async with self._lock:
            entry = self._entries.pop(jti, None)
            if entry is None:
                return False
            entry.revoked.set()
            return True


class _BodyTooLarge(ValueError):
    pass


def _cookie_secure(cfg: RelayConfig) -> bool:
    """Allow an insecure cookie for the loopback quick-start, or when the
    operator has explicitly opted into ALLOW_INSECURE_HTTP for a plain-http
    public origin."""
    origin = urlsplit(cfg.public_origin.strip().rstrip("/"))
    if origin.scheme != "http":
        return True
    if origin.hostname in {"127.0.0.1", "::1", "localhost"}:
        return False
    return not cfg.allow_insecure_http


def _rate_limited(ip: str) -> bool:
    return _login_limiter.limited(ip)


def _request_origin_allowed(req: Request, cfg: RelayConfig) -> bool:
    """Reject browser cross-origin POSTs while retaining non-browser CLI use."""
    origin = req.headers.get("origin", "").strip()
    return not origin or origin == cfg.public_origin


def _request_ip(req: Request) -> str:
    """Use Caddy's client address only when the direct peer is loopback.

    The bundled relay binds 127.0.0.1 behind Caddy. Without this trusted-proxy
    rule every public user shares the single 127.0.0.1 login bucket, so five bad
    attempts can continuously lock out the legitimate user. A directly exposed
    relay never trusts a caller-supplied forwarding header.
    """
    peer = req.client.host if req.client else "?"
    if peer not in {"127.0.0.1", "::1", "localhost"}:
        return peer[:128]
    forwarded = req.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    if not forwarded:
        return peer
    try:
        return str(ipaddress.ip_address(forwarded))
    except ValueError:
        return peer


async def _read_json_limited(req: Request, max_bytes: int):
    content_length = req.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise ValueError("invalid content-length") from exc
        if declared_length < 0:
            raise ValueError("invalid content-length")
        if declared_length > max_bytes:
            raise _BodyTooLarge

    body = bytearray()
    async for chunk in req.stream():
        if len(body) + len(chunk) > max_bytes:
            raise _BodyTooLarge
        body.extend(chunk)
    return json.loads(body)


async def _serve_client_until_expiry(
    websocket: WebSocket,
    hub: RelayHub,
    expires_at: int,
    revoked: asyncio.Event,
) -> None:
    """Serve until disconnect, signed expiry, or server-side revocation."""
    remaining = max(0.0, expires_at - time.time())
    owner = asyncio.current_task()
    assert owner is not None
    close_signal: asyncio.Future[tuple[int, str]] = (
        asyncio.get_running_loop().create_future()
    )

    async def guard() -> None:
        try:
            await asyncio.wait_for(revoked.wait(), timeout=remaining)
            close = (SESSION_REVOKED_CLOSE_CODE, SESSION_REVOKED_CLOSE_REASON)
        # Python 3.10 raises asyncio.TimeoutError here; builtin TimeoutError is
        # only a reliable alias from Python 3.11 onward.
        except asyncio.TimeoutError:
            close = (SESSION_EXPIRED_CLOSE_CODE, SESSION_EXPIRED_CLOSE_REASON)
        if not close_signal.done():
            close_signal.set_result(close)
            owner.cancel()

    guard_task = asyncio.create_task(guard())
    try:
        await hub.serve_client(websocket)
    except asyncio.CancelledError:
        if not close_signal.done():
            raise
        code, reason = close_signal.result()
        log.info("ws session closed", reason=reason)
        await websocket.close(code=code, reason=reason)
    finally:
        if not guard_task.done():
            guard_task.cancel()
        await asyncio.gather(guard_task, return_exceptions=True)


def create_app(cfg: Optional[RelayConfig] = None) -> FastAPI:
    if cfg is None:
        cfg = relay_config()
    validate_relay_config(cfg)
    app = FastAPI(title="cc-remote relay")
    hub = RelayHub(cfg)
    sessions = SessionRegistry(cfg.session_registry_cap)
    login_slots = asyncio.Semaphore(cfg.login_inflight_cap)
    app.state.hub = hub
    app.state.sessions = sessions
    app.state.login_slots = login_slots

    @app.post("/api/login")
    async def login(req: Request) -> JSONResponse:
        if not _request_origin_allowed(req, cfg):
            return JSONResponse(
                {"error": "origin_rejected"},
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )
        ip = _request_ip(req)
        if _rate_limited(ip):
            return JSONResponse(
                {"error": "rate_limited"},
                status_code=429,
                headers={"Cache-Control": "no-store", "Retry-After": "1"},
            )
        # Do not queue an unbounded number of slow bodies behind the gate.
        # There is no await between locked() and acquire(), so this check and
        # the immediate decrement are atomic with respect to the event loop.
        if login_slots.locked():
            return JSONResponse(
                {"error": "login_capacity"},
                status_code=503,
                headers={"Cache-Control": "no-store", "Retry-After": "1"},
            )
        await login_slots.acquire()
        try:
            try:
                body = await asyncio.wait_for(
                    _read_json_limited(req, cfg.login_body_max_bytes),
                    timeout=cfg.login_read_timeout,
                )
            except asyncio.TimeoutError:
                return JSONResponse(
                    {"error": "request_timeout"},
                    status_code=408,
                    headers={"Cache-Control": "no-store"},
                )
        except _BodyTooLarge:
            return JSONResponse(
                {"error": "too_large"},
                status_code=413,
                headers={"Cache-Control": "no-store"},
            )
        except Exception:
            return JSONResponse(
                {"error": "bad_request"},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        finally:
            login_slots.release()
        candidate = body.get("password", "") if isinstance(body, dict) else ""
        password = candidate if isinstance(candidate, str) else ""
        if not verify_password(password, cfg.login_password):
            log.warning("login failed", ip=ip)
            return JSONResponse({"error": "invalid"}, status_code=401)
        token, exp = make_session_token(cfg.session_secret, cfg.session_ttl_seconds)
        claims = session_token_claims(token, cfg.session_secret)
        assert claims is not None
        if not await sessions.register(claims):
            log.warning("session registry full", ip=ip)
            return JSONResponse({"error": "session_capacity"}, status_code=503)
        log.info("login ok", ip=ip, exp=exp)
        response = JSONResponse(
            {"ok": True, "exp": exp}, headers={"Cache-Control": "no-store"}
        )
        response.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            max_age=cfg.session_ttl_seconds,
            path="/",
            secure=_cookie_secure(cfg),
            httponly=True,
            samesite="strict",
        )
        return response

    @app.get("/api/session")
    async def session_status(req: Request) -> JSONResponse:
        token = req.cookies.get(SESSION_COOKIE_NAME, "")
        claims = session_token_claims(token, cfg.session_secret)
        if (
            claims is None
            or claims.expires_at <= time.time()
            or not await sessions.active(claims)
        ):
            return JSONResponse(
                {"ok": False}, status_code=401, headers={"Cache-Control": "no-store"}
            )
        return JSONResponse(
            {"ok": True, "exp": claims.expires_at},
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/logout")
    async def logout(req: Request) -> JSONResponse:
        if not _request_origin_allowed(req, cfg):
            return JSONResponse(
                {"error": "origin_rejected"},
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )
        token = req.cookies.get(SESSION_COOKIE_NAME, "")
        claims = session_token_claims(token, cfg.session_secret)
        if claims is not None:
            await sessions.revoke(claims.jti)
        response = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
        response.delete_cookie(
            SESSION_COOKIE_NAME,
            path="/",
            secure=_cookie_secure(cfg),
            httponly=True,
            samesite="strict",
        )
        return response

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        role = authenticate(websocket.headers.get("authorization", ""), cfg)  # wrapper Bearer
        claims: Optional[SessionClaims] = None
        if role is None:
            token = websocket.cookies.get(SESSION_COOKIE_NAME, "")
            origin = websocket.headers.get("origin", "")
            expected_origin = cfg.public_origin.strip().rstrip("/")
            origin_ok = not expected_origin or origin == expected_origin
            claims = session_token_claims(token, cfg.session_secret)
            if (
                claims is not None
                and claims.expires_at > time.time()
                and origin_ok
                and await sessions.active(claims)
            ):
                role = "client"
            elif token and not origin_ok:
                log.warning("ws origin rejected", origin=origin or "-")
        if role is None:
            await websocket.close(code=1008, reason="unauthorized")
            return
        await websocket.accept()
        log.info("ws accepted", role=role)
        if role == "wrapper":
            await hub.serve_wrapper(websocket)
        else:
            assert claims is not None
            revoked = await sessions.subscribe(claims)
            if revoked is None:
                await websocket.close(
                    code=SESSION_REVOKED_CLOSE_CODE,
                    reason=SESSION_REVOKED_CLOSE_REASON,
                )
                return
            await _serve_client_until_expiry(
                websocket, hub, claims.expires_at, revoked
            )

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "wrapper_connected": hub.wrapper_connected,
                "clients": hub.client_count}

    # Static web client. Mounted last so /api/login and /ws and /healthz win.
    if cfg.static_dir and os.path.isdir(cfg.static_dir):
        from fastapi.staticfiles import StaticFiles
        app.mount("/", StaticFiles(directory=cfg.static_dir, html=True), name="static")
        log.info("serving static web client", dir=cfg.static_dir)

    return app
