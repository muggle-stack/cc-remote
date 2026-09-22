"""FastAPI relay: /api/login (password -> session cookie), /ws (wrapper + web
clients), /healthz, optional static hosting of the web client from the same
origin.

Auth: wrapper authenticates with a Bearer WRAPPER_TOKEN header; web clients
authenticate with a Secure HttpOnly session cookie obtained from /api/login.
Cookie-authenticated WebSockets must also match PUBLIC_ORIGIN, or an explicitly
enabled same-port private-IP origin.

The relay never imports claude_agent_sdk and never touches the model API.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse

from cc_remote.config import (
    RelayConfig, relay_config, valid_machine_id, validate_relay_config,
)
from cc_remote.log import logger
from cc_remote.protocol import PROTOCOL_VERSION
from cc_remote.relay.auth import (
    SessionClaims, authenticate_login,
    make_session_token, session_token_claims, wrapper_machine_scope,
)
from cc_remote.relay.devices import DeviceStore
from cc_remote.relay.viewer import ViewerError, ViewerRelay, main_cookie_name
from cc_remote.relay.pairing import RelayHub
from cc_remote.relay.push import (
    PushDispatcher, PushOutcome, PushSubscription, PushSubscriptionStore,
)

log = logger("cc_remote.relay.server")

_LOGIN_WINDOW = 60.0
_LOGIN_MAX = 5
_LOGIN_MAX_IPS = 4096
_LOGIN_MAX_TOTAL_ATTEMPTS = 16384


def _turn_push_outcome(result: object | None) -> PushOutcome:
    subtype = str(getattr(result, "subtype", "") or "").lower()
    if subtype in {
        "error_during_execution", "interrupted", "cancelled", "canceled",
    }:
        return "interrupted"
    if bool(getattr(result, "is_error", False)):
        return "failed"
    return "success"


_LOGIN_CLEANUP_INTERVAL = 10.0
SESSION_EXPIRED_CLOSE_CODE = 1008
SESSION_EXPIRED_CLOSE_REASON = "session expired"
SESSION_REVOKED_CLOSE_CODE = 1008
SESSION_REVOKED_CLOSE_REASON = "session revoked"
_PUSH_BODY_MAX_BYTES = 16 * 1024
_DEVICE_BODY_MAX_BYTES = 8 * 1024
_PUSH_KEY_RE = re.compile(r"[A-Za-z0-9_-]{16,1024}")
_PRIVATE_ORIGIN_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "::1/128",
        "fc00::/7",
    )
)


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
_pair_limiter = LoginRateLimiter(max_per_ip=30)


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
        return await self.active_id(claims.jti, claims.expires_at)

    async def active_id(self, jti: str, expires_at: float) -> bool:
        async with self._lock:
            self._prune_locked(time.time())
            entry = self._entries.get(jti)
            return entry is not None and entry.expires_at == expires_at

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


def _private_origin_allowed(origin: str, cfg: RelayConfig) -> bool:
    """Allow only a literal private IP on the relay's direct listening port."""
    if not cfg.allow_private_origins:
        return False
    try:
        parsed = urlsplit(origin)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return False
    effective_port = port or (443 if parsed.scheme == "https" else 80)
    if effective_port != cfg.port:
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return any(address in network for network in _PRIVATE_ORIGIN_NETWORKS)


def _origin_allowed(origin: str, cfg: RelayConfig) -> bool:
    candidate = origin.strip()
    return candidate == cfg.public_origin or _private_origin_allowed(candidate, cfg)


def _canonical_origin_host(hostname: str) -> str | None:
    try:
        return str(ipaddress.ip_address(hostname))
    except ValueError:
        try:
            return hostname.encode("idna").decode("ascii").lower()
        except UnicodeError:
            return None


def _origin_parts(origin: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(origin)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or hostname is None:
        return None
    host = _canonical_origin_host(hostname)
    if host is None:
        return None
    return (
        parsed.scheme,
        host,
        port or (443 if parsed.scheme == "https" else 80),
    )


def _request_target_parts(
    req: Request | WebSocket,
) -> tuple[str, str, int] | None:
    scheme = req.url.scheme.lower()
    if scheme == "ws":
        scheme = "http"
    elif scheme == "wss":
        scheme = "https"
    if scheme not in {"http", "https"} or req.url.hostname is None:
        return None
    host = _canonical_origin_host(req.url.hostname)
    if host is None:
        return None
    return (
        scheme,
        host,
        req.url.port or (443 if scheme == "https" else 80),
    )


def _request_cookie_secure(req: Request) -> bool:
    """Select Secure from the trusted effective request transport."""
    return req.url.scheme.lower() == "https"


def _rate_limited(ip: str) -> bool:
    return _login_limiter.limited(ip)


def _request_origin_allowed(
    req: Request | WebSocket,
    cfg: RelayConfig,
    *,
    allow_missing: bool = True,
) -> bool:
    """Bind an accepted browser Origin to the effective request target."""
    origin = req.headers.get("origin", "").strip()
    if not origin:
        return allow_missing
    return (
        _origin_allowed(origin, cfg)
        and _origin_parts(origin) == _request_target_parts(req)
    )


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


async def _active_claims(
    req: Request | WebSocket,
    cfg: RelayConfig,
    sessions: SessionRegistry,
) -> SessionClaims | None:
    token = req.cookies.get(main_cookie_name(cfg, req), "")
    claims = session_token_claims(token, cfg.session_secret)
    if (
        claims is None
        or claims.expires_at <= time.time()
        or not await sessions.active(claims)
    ):
        return None
    return claims


def _push_subject(claims: SessionClaims) -> str:
    # Legacy password mode intentionally represents one shared user.
    return claims.subject or "legacy"


def _device_subject(claims: SessionClaims) -> str:
    # Legacy single-password mode is one owner, just like Push subscriptions.
    return claims.subject or "legacy"


def _btw_owner_id(claims: SessionClaims, secret: str) -> str:
    """Derive an opaque, reload-stable BTW owner from authenticated claims."""
    digest = hmac.new(
        secret.encode("utf-8"),
        f"cc-remote:btw-owner:{_device_subject(claims)}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"owner-{digest[:32]}"


async def _claims_allow_machine(
    claims: SessionClaims,
    machine_id: str,
    devices: DeviceStore,
) -> bool:
    return claims.allows_machine(machine_id) or await devices.owned_by(
        machine_id, _device_subject(claims))


def _clean_device_text(value: object, *, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if (not normalized or len(normalized) > maximum
            or any(ord(char) < 32 for char in normalized)):
        return None
    return normalized


def _parse_push_subscription(
    body: object,
    claims: SessionClaims,
    *,
    enrolled_machine: bool = False,
) -> PushSubscription | None:
    if not isinstance(body, dict):
        return None
    machine_id = body.get("machine_id")
    endpoint = body.get("endpoint")
    keys = body.get("keys")
    if (
        not isinstance(machine_id, str)
        or not valid_machine_id(machine_id)
        or (not claims.allows_machine(machine_id) and not enrolled_machine)
        or not isinstance(endpoint, str)
        or len(endpoint) > 4096
        or not isinstance(keys, dict)
    ):
        return None
    try:
        parsed = urlsplit(endpoint)
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return None
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    notification_mode = body.get("notification_mode", "generic")
    if (
        not isinstance(p256dh, str)
        or not _PUSH_KEY_RE.fullmatch(p256dh)
        or not isinstance(auth, str)
        or not _PUSH_KEY_RE.fullmatch(auth)
        or not isinstance(notification_mode, str)
        or notification_mode not in {"generic", "session"}
    ):
        return None
    return PushSubscription(
        subject=_push_subject(claims),
        machine_id=machine_id,
        endpoint=endpoint,
        p256dh=p256dh,
        auth=auth,
        session_jti=claims.jti,
        expires_at=claims.expires_at,
        notification_mode=notification_mode,
    )


async def _serve_client_until_expiry(
    websocket: WebSocket,
    hub: RelayHub,
    expires_at: int,
    revoked: asyncio.Event,
    machine_id: str = "default",
    owner_id: str | None = None,
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
        bound_owner = owner_id or getattr(
            getattr(websocket, "state", None), "btw_owner_id", None)
        if bound_owner is not None:
            await hub.serve_client(websocket, machine_id, bound_owner)
        elif machine_id == "default":
            # Preserve the small embedded/test hub surface when no account
            # identity was attached by the authenticated endpoint.
            await hub.serve_client(websocket)
        else:
            await hub.serve_client(websocket, machine_id)
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


def create_app(
    cfg: Optional[RelayConfig] = None,
    *,
    device_store: DeviceStore | None = None,
) -> FastAPI:
    if cfg is None:
        cfg = relay_config()
    validate_relay_config(cfg)
    app = FastAPI(title="cc-remote relay")
    sessions = SessionRegistry(cfg.session_registry_cap)
    devices = device_store or DeviceStore(cfg.device_db_path)
    push_store: PushSubscriptionStore | None = None
    push_dispatcher: PushDispatcher | None = None
    if cfg.push_vapid_public_key:
        push_store = PushSubscriptionStore(cfg.push_db_path)
        push_dispatcher = PushDispatcher(
            push_store,
            vapid_private_key=cfg.push_vapid_private_key,
            vapid_subject=cfg.push_vapid_subject,
            session_active=lambda subscription: sessions.active_id(
                subscription.session_jti, subscription.expires_at),
        )

    async def on_live_turn_end(machine_id: str, msg: object) -> None:
        if push_dispatcher is None:
            return
        result = getattr(msg, "result", None)
        raw_context = getattr(msg, "notification_context", None)
        context = (
            raw_context.model_dump()
            if hasattr(raw_context, "model_dump")
            else None
        )
        if context is not None:
            context["sid"] = getattr(msg, "sid", None)
        await push_dispatcher.notify_turn_end(
            machine_id,
            outcome=_turn_push_outcome(result),
            context=context,
        )

    hub = RelayHub(cfg, on_live_turn_end=on_live_turn_end)
    login_slots = asyncio.Semaphore(cfg.login_inflight_cap)
    app.state.hub = hub
    app.state.sessions = sessions
    app.state.login_slots = login_slots
    app.state.push_store = push_store
    app.state.push_dispatcher = push_dispatcher
    app.state.device_store = devices
    viewers = ViewerRelay(
        cfg, lambda req: _active_claims(req, cfg, sessions), sessions.active,
        lambda claims, machine: _claims_allow_machine(claims, machine, devices),
        lambda req: _request_origin_allowed(req, cfg),
    )
    app.state.viewers = viewers

    @app.middleware("http")
    async def static_shell_cache_policy(req: Request, call_next):
        """Never let a protocol upgrade reload into a stale application shell."""
        grant_id = viewers.host_grant(req)
        if grant_id is None and req.url.path.startswith("/__cc_viewer/bridge/"):
            from cc_remote.relay.viewer_bridge import bridge_runner
            return await bridge_runner(req, viewers)
        viewer_api = req.url.path == "/api/viewers" or req.url.path.startswith("/api/viewers/")
        if grant_id is not None or viewer_api:
            try:
                response = (await viewers.host(req, grant_id) if grant_id is not None
                            else await viewers.api(req))
            except (ViewerError, ValueError, asyncio.TimeoutError) as exc:
                status = exc.status if isinstance(exc, ViewerError) else 400
                code = exc.code if isinstance(exc, ViewerError) else "invalid_request"
                if grant_id is not None:
                    messages = {
                        "device_offline": "设备已离线，请连接后重新打开预览。",
                        "resource_unavailable": "资源不存在、已更改或未包含在预览范围内。",
                        "publication_changed": "预览配置已更新，请重新打开。",
                        "preview_expired": "预览已过期，请从 cc-remote 重新打开。",
                    }
                    response = HTMLResponse(
                        '<meta charset="utf-8"><p style="font:15px system-ui;padding:24px">'
                        + messages.get(code, "当前无法打开预览，请返回 cc-remote 重试。") + "</p>",
                        status_code=status,
                    )
                else:
                    response = JSONResponse({"error": code}, status_code=status)
            if grant_id is not None:
                response.headers.update(viewers.headers(grant_id))
            if viewer_api or req.url.path.startswith("/__cc_viewer/") or response.status_code >= 400:
                response.headers["Cache-Control"] = "no-store"
            return response
        response = await call_next(req)
        path = req.url.path
        if path in {"/", "/index.html", "/sw.js", "/cc-remote-build.json", "/cc-remote-viewer-runner.js"}:
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        elif (
            path.startswith("/assets/")
            and response.status_code in {200, 206}
        ):
            # Vite asset names are content-addressed; old pages can keep using
            # their exact files while a new navigation picks up the new index.
            response.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable"
            )
        return response

    @app.get("/api/auth-config")
    async def auth_config() -> JSONResponse:
        return JSONResponse(
            {"multi_user": bool(cfg.login_users_json)},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/push-config")
    async def push_config(req: Request) -> JSONResponse:
        claims = await _active_claims(req, cfg, sessions)
        if claims is None:
            return JSONResponse(
                {"ok": False}, status_code=401,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            {
                "enabled": push_dispatcher is not None,
                "public_key": cfg.push_vapid_public_key
                if push_dispatcher is not None else "",
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/push/subscribe")
    async def push_subscribe(req: Request) -> JSONResponse:
        if not _request_origin_allowed(req, cfg):
            return JSONResponse({"error": "origin_rejected"}, status_code=403)
        claims = await _active_claims(req, cfg, sessions)
        if claims is None:
            return JSONResponse({"ok": False}, status_code=401)
        if push_store is None:
            return JSONResponse({"error": "push_disabled"}, status_code=503)
        try:
            body = await _read_json_limited(req, _PUSH_BODY_MAX_BYTES)
        except _BodyTooLarge:
            return JSONResponse({"error": "too_large"}, status_code=413)
        except Exception:
            return JSONResponse({"error": "bad_request"}, status_code=400)
        requested_machine = body.get("machine_id") if isinstance(body, dict) else None
        enrolled_machine = (
            isinstance(requested_machine, str)
            and await devices.owned_by(
                requested_machine, _device_subject(claims))
        )
        subscription = _parse_push_subscription(
            body, claims, enrolled_machine=enrolled_machine)
        if subscription is None:
            return JSONResponse({"error": "invalid_subscription"}, status_code=400)
        await push_store.upsert(subscription)
        return JSONResponse(
            {"ok": True}, headers={"Cache-Control": "no-store"})

    @app.post("/api/push/unsubscribe")
    async def push_unsubscribe(req: Request) -> JSONResponse:
        if not _request_origin_allowed(req, cfg):
            return JSONResponse({"error": "origin_rejected"}, status_code=403)
        claims = await _active_claims(req, cfg, sessions)
        if claims is None:
            return JSONResponse({"ok": False}, status_code=401)
        if push_store is None:
            return JSONResponse({"ok": True})
        try:
            body = await _read_json_limited(req, _PUSH_BODY_MAX_BYTES)
        except _BodyTooLarge:
            return JSONResponse({"error": "too_large"}, status_code=413)
        except Exception:
            return JSONResponse({"error": "bad_request"}, status_code=400)
        endpoint = body.get("endpoint") if isinstance(body, dict) else None
        if not isinstance(endpoint, str) or not (1 <= len(endpoint) <= 4096):
            return JSONResponse({"error": "invalid_subscription"}, status_code=400)
        await push_store.remove_endpoint(_push_subject(claims), endpoint)
        return JSONResponse(
            {"ok": True}, headers={"Cache-Control": "no-store"})

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
        username_value = body.get("username", "") if isinstance(body, dict) else ""
        username = username_value if isinstance(username_value, str) else ""
        access = authenticate_login(username, password, cfg)
        if access is None:
            log.warning("login failed", ip=ip)
            return JSONResponse({"error": "invalid"}, status_code=401)
        subject, machines = access
        token, exp = make_session_token(
            cfg.session_secret,
            cfg.session_ttl_seconds,
            subject=subject,
            machines=machines,
        )
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
            main_cookie_name(cfg, req),
            token,
            max_age=cfg.session_ttl_seconds,
            path="/",
            secure=_request_cookie_secure(req),
            httponly=True,
            samesite="strict",
        )
        return response

    @app.get("/api/session")
    async def session_status(req: Request) -> JSONResponse:
        token = req.cookies.get(main_cookie_name(cfg, req), "")
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
            {"ok": True, "exp": claims.expires_at,
             "username": claims.subject},
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
        token = req.cookies.get(main_cookie_name(cfg, req), "")
        claims = session_token_claims(token, cfg.session_secret)
        if claims is not None:
            if push_store is not None:
                await push_store.remove_session(claims.jti)
            await sessions.revoke(claims.jti)
        response = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
        response.delete_cookie(
            main_cookie_name(cfg, req),
            path="/",
            secure=_request_cookie_secure(req),
            httponly=True,
            samesite="strict",
        )
        return response

    @app.get("/api/devices")
    async def device_list(req: Request) -> JSONResponse:
        claims = await _active_claims(req, cfg, sessions)
        if claims is None:
            return JSONResponse({"ok": False}, status_code=401)
        subject = _device_subject(claims)
        records = await devices.list_for_subject(subject)
        connected = set(hub.machine_ids)
        result = [
            {
                "machine_id": record.machine_id,
                "label": record.label,
                "platform": record.platform,
                "hostname": record.hostname,
                "created_at": record.created_at,
                "last_seen": record.last_seen,
                "online": record.machine_id in connected,
                "managed": True,
                **hub.device_version(record.machine_id),
            }
            for record in records
        ]
        known = {record.machine_id for record in records}
        visible_legacy = {
            machine_id for machine_id in connected | set(hub.versioned_machine_ids)
            if claims.allows_machine(machine_id)
        }
        if "*" not in claims.machines:
            visible_legacy.update(claims.machines)
        for machine_id in sorted(visible_legacy - known):
            result.append({
                "machine_id": machine_id,
                "label": machine_id,
                "platform": "",
                "hostname": "",
                "created_at": None,
                "last_seen": None,
                "online": machine_id in connected,
                "managed": False,
                **hub.device_version(machine_id),
            })
        pairing_expires_at = await devices.pairing_expires_at(subject)
        return JSONResponse(
            {
                "ok": True,
                "devices": result,
                "pairing": {
                    "enabled": pairing_expires_at is not None,
                    "expires_at": pairing_expires_at,
                },
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/devices/pairing")
    async def device_pairing_start(req: Request) -> JSONResponse:
        if not _request_origin_allowed(req, cfg):
            return JSONResponse({"error": "origin_rejected"}, status_code=403)
        claims = await _active_claims(req, cfg, sessions)
        if claims is None:
            return JSONResponse({"ok": False}, status_code=401)
        grant = await devices.create_pairing(
            _device_subject(claims), ttl=cfg.device_pairing_ttl_seconds)
        return JSONResponse(
            {"ok": True, "code": grant.code, "expires_at": grant.expires_at},
            headers={"Cache-Control": "no-store"},
        )

    @app.delete("/api/devices/pairing")
    async def device_pairing_close(req: Request) -> JSONResponse:
        if not _request_origin_allowed(req, cfg):
            return JSONResponse({"error": "origin_rejected"}, status_code=403)
        claims = await _active_claims(req, cfg, sessions)
        if claims is None:
            return JSONResponse({"ok": False}, status_code=401)
        await devices.close_pairing(_device_subject(claims))
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})

    @app.post("/api/devices/pair")
    async def device_pair(req: Request) -> JSONResponse:
        if _pair_limiter.limited(_request_ip(req)):
            return JSONResponse(
                {"error": "rate_limited"}, status_code=429,
                headers={"Retry-After": "1", "Cache-Control": "no-store"},
            )
        try:
            body = await _read_json_limited(req, _DEVICE_BODY_MAX_BYTES)
        except _BodyTooLarge:
            return JSONResponse({"error": "too_large"}, status_code=413)
        except Exception:
            return JSONResponse({"error": "bad_request"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "bad_request"}, status_code=400)
        code = _clean_device_text(body.get("code"), maximum=64)
        label = _clean_device_text(body.get("label"), maximum=64)
        platform = _clean_device_text(body.get("platform"), maximum=32)
        hostname = _clean_device_text(body.get("hostname"), maximum=255)
        if None in (code, label, platform, hostname):
            return JSONResponse({"error": "invalid_device"}, status_code=400)
        enrolled = await devices.redeem(
            code,
            label=label,
            platform=platform,
            hostname=hostname,
        )
        if enrolled is None:
            return JSONResponse(
                {"error": "invalid_or_expired_code"}, status_code=401,
                headers={"Cache-Control": "no-store"},
            )
        origin = urlsplit(cfg.public_origin)
        relay_url = (
            f"{'wss' if origin.scheme == 'https' else 'ws'}://{origin.netloc}/ws"
        )
        return JSONResponse(
            {
                "ok": True,
                "machine_id": enrolled.machine_id,
                "label": enrolled.label,
                "token": enrolled.token,
                "relay_url": relay_url,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.patch("/api/devices/{machine_id}")
    async def device_rename(machine_id: str, req: Request) -> JSONResponse:
        if not valid_machine_id(machine_id):
            return JSONResponse({"error": "invalid_device"}, status_code=400)
        if not _request_origin_allowed(req, cfg):
            return JSONResponse({"error": "origin_rejected"}, status_code=403)
        claims = await _active_claims(req, cfg, sessions)
        if claims is None:
            return JSONResponse({"ok": False}, status_code=401)
        try:
            body = await _read_json_limited(req, _DEVICE_BODY_MAX_BYTES)
        except _BodyTooLarge:
            return JSONResponse({"error": "too_large"}, status_code=413)
        except Exception:
            return JSONResponse({"error": "bad_request"}, status_code=400)
        label = _clean_device_text(
            body.get("label") if isinstance(body, dict) else None, maximum=64)
        if label is None:
            return JSONResponse({"error": "invalid_label"}, status_code=400)
        changed = await devices.rename(
            machine_id, _device_subject(claims), label)
        if not changed:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})

    @app.delete("/api/devices/{machine_id}")
    async def device_revoke(machine_id: str, req: Request) -> JSONResponse:
        if not valid_machine_id(machine_id):
            return JSONResponse({"error": "invalid_device"}, status_code=400)
        if not _request_origin_allowed(req, cfg):
            return JSONResponse({"error": "origin_rejected"}, status_code=403)
        claims = await _active_claims(req, cfg, sessions)
        if claims is None:
            return JSONResponse({"ok": False}, status_code=401)
        revoked = await devices.revoke(machine_id, _device_subject(claims))
        if not revoked:
            return JSONResponse({"error": "not_found"}, status_code=404)
        await hub.disconnect_wrapper(machine_id, reason="device revoked")
        hub.forget_wrapper_version(machine_id)
        await viewers.disconnect_machine(machine_id)
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})

    @app.get("/api/machines")
    async def machine_list(req: Request) -> JSONResponse:
        claims = await _active_claims(req, cfg, sessions)
        if claims is None:
            return JSONResponse(
                {"ok": False}, status_code=401,
                headers={"Cache-Control": "no-store"},
            )
        records = await devices.list_for_subject(_device_subject(claims))
        machines = {record.machine_id for record in records}
        machines.update(
            machine_id for machine_id in hub.machine_ids
            if claims.allows_machine(machine_id)
        )
        if "*" not in claims.machines:
            machines.update(claims.machines)
        return JSONResponse(
            {"ok": True, "machines": sorted(machines)},
            headers={"Cache-Control": "no-store"},
        )

    @app.websocket("/ws/viewer")
    async def viewer_ws(websocket: WebSocket) -> None:
        authorization = websocket.headers.get("authorization", "")
        token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        scope = wrapper_machine_scope(token, cfg)
        dynamic = scope is None
        if dynamic:
            scope = await devices.machine_for_token(token)
        if scope is None or websocket.headers.get("origin"):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        async def authorized(machine_id: str) -> bool:
            return (await devices.machine_for_token(token) == machine_id) if dynamic else True
        await viewers.serve_wrapper(websocket, scope, authorized)

    @app.websocket("/ws/viewer-client")
    async def viewer_client_ws(websocket: WebSocket) -> None:
        # This is a browser-only resource connection, never a wrapper role or
        # chat connection. A public grant ID is not authentication.
        from cc_remote.relay.viewer_bridge import serve_bridge
        claims = await _active_claims(websocket, cfg, sessions)
        if (claims is None or websocket.headers.get("authorization")
                or not _request_origin_allowed(websocket, cfg, allow_missing=False)):
            await websocket.close(code=1008, reason="unauthorized")
            return
        await serve_bridge(websocket, viewers, claims)

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        authorization = websocket.headers.get("authorization", "")
        role: str | None = None
        wrapper_scope: str | None = None
        dynamic_wrapper_token: str | None = None
        if authorization.lower().startswith("bearer "):
            wrapper_token = authorization[7:].strip()
            wrapper_scope = wrapper_machine_scope(wrapper_token, cfg)
            if wrapper_scope is None:
                wrapper_scope = await devices.machine_for_token(wrapper_token)
                if wrapper_scope is not None:
                    dynamic_wrapper_token = wrapper_token
            if wrapper_scope is not None:
                role = "wrapper"
        claims: Optional[SessionClaims] = None
        if role is None:
            token = websocket.cookies.get(main_cookie_name(cfg, websocket), "")
            origin = websocket.headers.get("origin", "")
            origin_ok = _request_origin_allowed(
                websocket, cfg, allow_missing=False,
            )
            claims = session_token_claims(token, cfg.session_secret)
            if (
                claims is not None
                and claims.expires_at > time.time()
                and origin_ok
                and await sessions.active(claims)
            ):
                role = "client"
            elif token and not origin_ok:
                log.warning("ws origin rejected", origin=(origin or "-")[:512])
        if role is None:
            await websocket.close(code=1008, reason="unauthorized")
            return
        await websocket.accept()
        log.info("ws accepted", role=role)
        if role == "wrapper":
            if wrapper_scope not in (None, "*"):
                await devices.touch_seen(wrapper_scope)
            async def dynamic_wrapper_authorized(machine_id: str) -> bool:
                if dynamic_wrapper_token is None:
                    return True
                return await devices.machine_for_token(
                    dynamic_wrapper_token) == machine_id
            await hub.serve_wrapper(
                websocket,
                None if wrapper_scope == "*" else wrapper_scope,
                dynamic_wrapper_authorized,
            )
        else:
            assert claims is not None
            revoked = await sessions.subscribe(claims)
            if revoked is None:
                await websocket.close(
                    code=SESSION_REVOKED_CLOSE_CODE,
                    reason=SESSION_REVOKED_CLOSE_REASON,
                )
                return
            requested_machine = websocket.query_params.get("machine", "").strip()
            if requested_machine and not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}", requested_machine
            ):
                await websocket.close(code=1008, reason="invalid machine")
                return
            if requested_machine and not await _claims_allow_machine(
                    claims, requested_machine, devices):
                await websocket.close(code=1008, reason="machine not authorized")
                return
            if requested_machine:
                machine_id = requested_machine
            else:
                connected = [
                    candidate for candidate in hub.machine_ids
                    if await _claims_allow_machine(claims, candidate, devices)
                ]
                if connected:
                    machine_id = connected[0]
                elif "*" not in claims.machines:
                    machine_id = claims.machines[0]
                else:
                    enrolled = await devices.list_for_subject(
                        _device_subject(claims))
                    machine_id = (
                        enrolled[0].machine_id if enrolled
                        else hub.default_machine_id()
                    )
            if machine_id == "default":
                # Keep the legacy call shape for embedded relays and tests that
                # replace the expiry guard. Named machines use the extended
                # route-aware form below.
                websocket.state.btw_owner_id = _btw_owner_id(
                    claims, cfg.session_secret)
                await _serve_client_until_expiry(
                    websocket, hub, claims.expires_at, revoked)
            else:
                websocket.state.btw_owner_id = _btw_owner_id(
                    claims, cfg.session_secret)
                await _serve_client_until_expiry(
                    websocket, hub, claims.expires_at, revoked, machine_id)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        from cc_remote import __version__

        return JSONResponse(
            {
                "ok": True,
                "protocol": PROTOCOL_VERSION,
                "version": __version__,
                "wrapper_connected": hub.wrapper_connected,
                "machines": hub.machine_ids,
                "clients": hub.client_count,
            },
            headers={"Cache-Control": "no-store"},
        )

    # Static web client. Mounted last so /api/login and /ws and /healthz win.
    if cfg.static_dir and os.path.isdir(cfg.static_dir):
        from fastapi.staticfiles import StaticFiles
        app.mount("/", StaticFiles(directory=cfg.static_dir, html=True), name="static")
        log.info("serving static web client", dir=cfg.static_dir)

    return app
