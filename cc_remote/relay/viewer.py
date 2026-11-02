"""Authenticated, isolated static Viewer hosts and a bounded binary channel.

No publication bytes enter the conversation protocol, replay ring or database.
The main origin approves a frame-created challenge; secrets travel exclusively
in HttpOnly cookies / Authorization headers, never in URLs or JSON messages.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import re
import secrets
import time
from urllib.parse import quote, urlsplit

import anyio

from fastapi import Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from cc_remote.protocol import PROTOCOL_VERSION
from cc_remote.relay.viewer_html import preview_html
from cc_remote.viewer import (
    CHUNK_SIZE, ID_RE, MAX_FILE_BYTES, MAX_REQUESTS, MAX_SITES, PULL_WINDOW, REQUEST_ID_RE,
    SCRIPT_ORIGINS, clean_path,
)

COOKIE = "__Host-cc_remote_viewer"
PREFIX = "/__cc_viewer/"
MAX_GRANTS = 256
MAX_PENDING = 512
GRANT_TTL = 3600
LEASE_TTL = 90

# Isolated pages navigate away from the bootstrap document. Install this before
# the page's meta CSP, without removing or relaxing that policy. The parent
# validates the focused frame's WindowProxy and origin before docking its panel.
PREVIEW_ESCAPE_SCRIPT = b"""\n<script>
if(window!==parent)window.addEventListener('keydown',event=>{
  if(event.key==='Escape'&&!event.isComposing&&event.keyCode!==229)
    setTimeout(()=>{if(!event.defaultPrevented)parent.postMessage({type:'cc-remote-preview-escape'},'*');},0);
});
</script>"""


def effective_viewer_mode(cfg) -> str:
    return cfg.viewer_mode or ("isolated" if cfg.viewer_origin_template else "bridge")


def validate_origin_template(template: str, public_origin: str) -> None:
    if not template:
        return
    if template.count("{id}") != 1:
        raise ValueError("VIEWER_ORIGIN_TEMPLATE needs one {id} hostname label")
    parsed = urlsplit(template.replace("{id}", "viewer"))
    main = urlsplit(public_origin)
    local = parsed.scheme == "http" and (parsed.hostname or "").endswith(".localhost")
    if (parsed.scheme != "https" and not local
            or not parsed.hostname or not re.fullmatch(r"[a-z0-9.-]+", parsed.hostname)
            or not template.startswith(f"{parsed.scheme}://{{id}}.")
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
            or (not local and main.scheme != "https")
            or parsed.hostname == main.hostname):
        raise ValueError("Viewer hosts must be isolated HTTPS origins (HTTP *.localhost for development)")
    _ = parsed.port


def main_cookie_name(cfg, req=None) -> str:
    # A sandboxed same-site sibling must not be able to toss a Domain cookie
    # named like the main session cookie. Existing deployments stay unchanged
    # until the independently hosted Viewer feature is enabled.
    secure = req is None or req.url.scheme in {"https", "wss"}
    if effective_viewer_mode(cfg) == "isolated" and cfg.public_origin.startswith("https://") and secure:
        return "__Host-cc_remote_session"
    return "cc_remote_session"


class PublicSite(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1, max_length=48, pattern=r"^[a-z0-9][a-z0-9-]*$")
    label: str = Field(min_length=1, max_length=80)
    entry: str = Field(max_length=4096)
    revision: str = Field(pattern=r"^[a-f0-9]{32}$")
    script_origins: list[str] = Field(default_factory=list, max_length=3)
    urls: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("entry")
    @classmethod
    def entry_path(cls, value):
        path = clean_path(value)
        if not path.lower().endswith((".html", ".htm")) or path.startswith(PREFIX):
            raise ValueError("invalid Viewer entry")
        return path

    @field_validator("script_origins")
    @classmethod
    def origins(cls, value):
        if any(origin not in SCRIPT_ORIGINS for origin in value):
            raise ValueError("unsupported script origin")
        return sorted(set(value))

    @field_validator("urls")
    @classmethod
    def url_aliases(cls, values):
        for value in values:
            parsed = urlsplit(value)
            if (len(value) > 2048 or parsed.scheme not in {"http", "https"}
                    or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or any(ord(c) < 32 for c in value)):
                raise ValueError("invalid URL alias")
        return values


def parse_catalog(values) -> dict[str, PublicSite]:
    if not isinstance(values, list) or len(values) > MAX_SITES:
        raise ValueError("invalid Viewer catalog")
    sites = [PublicSite.model_validate(value) for value in values]
    if len({site.id for site in sites}) != len(sites):
        raise ValueError("duplicate publication")
    return {site.id: site for site in sites}


class ViewerError(Exception):
    def __init__(self, status: int, code: str):
        self.status = status
        self.code = code
        super().__init__(code)


@dataclass
class PendingRead:
    metadata: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    chunks: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=PULL_WINDOW))
    credit: int = 0
    expected: int = 0
    received: int = 0
    head: bool = False


class ViewerPeer:
    def __init__(self, ws: WebSocket, sites: dict[str, PublicSite]):
        self.ws = ws
        self.sites = sites
        self.home_sites: dict[str, PublicSite] = {}
        self.pending: dict[str, PendingRead] = {}
        self.read_slots = asyncio.Semaphore(MAX_REQUESTS)
        self.waiting_reads = 0
        self.lock = asyncio.Lock()
        self.closed = False
        self.session_pages = False
        self.media_previews = False
        self.metadata_pending: dict[str, asyncio.Future] = {}

    def publication(self, site_id: str):
        return self.sites.get(site_id) or self.home_sites.get(site_id)

    def page_publications(self, values):
        """Learn bounded, device-verified descriptors, without a global listing."""
        if not isinstance(values, list) or len(values) > 32:
            raise ViewerError(502, "invalid_page_metadata")
        result = []
        for value in values:
            value = dict(value)
            raw = value.pop("publication", None)
            if raw is not None:
                site = PublicSite.model_validate(raw)
                if site.id != value.get("site_id") or site.revision != value.get("revision"):
                    raise ViewerError(502, "invalid_page_metadata")
                if site.id.startswith("auto-"):
                    if site.id not in self.home_sites and len(self.home_sites) >= 128:
                        # This is a cache; page metadata on the Wrapper remains
                        # durable. Eviction revokes old grants, not associations.
                        self.home_sites.pop(next(iter(self.home_sites)))
                    self.home_sites[site.id] = site
            result.append(value)
        return result

    async def metadata(self, action: str, payload: dict):
        if self.closed or len(self.metadata_pending) >= 4:
            raise ViewerError(503 if self.closed else 429, "viewer_busy")
        request_id = secrets.token_hex(16)
        future = asyncio.get_running_loop().create_future()
        self.metadata_pending[request_id] = future
        try:
            await self.send({"type": "metadata", "request_id": request_id,
                             "action": action, "payload": payload})
            result = await asyncio.wait_for(future, 15)
            return self.page_publications(result) if action in {"locate", "verify"} else result
        finally:
            self.metadata_pending.pop(request_id, None)

    async def send(self, message):
        if self.closed:
            raise ViewerError(503, "device_offline")
        async with self.lock:
            await asyncio.wait_for(self.ws.send_json(message), timeout=10)

    async def read(self, site: PublicSite, path: str, method: str, headers: dict):
        return await self._read({"type": "request", "site_id": site.id,
                                 "revision": site.revision, "path": path}, method, headers)

    async def read_media(self, media_id: str, owner_id: str, method: str, headers: dict):
        if not self.media_previews:
            raise ViewerError(503, "video_unavailable")
        return await self._read({"type": "media_request", "media_id": media_id,
                                 "owner_id": owner_id}, method, headers)

    async def _read(self, payload: dict, method: str, headers: dict):
        if self.closed:
            raise ViewerError(503, "device_offline")
        if self.waiting_reads >= MAX_REQUESTS * 8:
            raise ViewerError(429, "viewer_busy")
        # Three.js commonly starts dozens of mesh fetches together. Queue a
        # bounded burst without failing the ninth file or buffering its body.
        self.waiting_reads += 1
        try:
            await asyncio.wait_for(self.read_slots.acquire(), 60)
        except TimeoutError as exc:
            raise ViewerError(429, "viewer_busy") from exc
        finally:
            self.waiting_reads -= 1
        if self.closed:
            self.read_slots.release()
            raise ViewerError(503, "device_offline")
        request_id = secrets.token_hex(16)
        pending = PendingRead(head=method == "HEAD")
        self.pending[request_id] = pending
        try:
            await self.send({**payload, "request_id": request_id,
                             "method": method, "headers": headers})
            status, response_headers = await asyncio.wait_for(pending.metadata, 15)
            return request_id, status, response_headers
        except BaseException:
            await self.cancel(request_id)
            raise

    async def stream(self, request_id: str, authorize):
        try:
            pending = self.pending[request_id]
            window = 0
            delivered = 0
            while True:
                await authorize()
                if not window:
                    pending.credit += PULL_WINDOW
                    await self.send({"type": "pull", "request_id": request_id, "credits": PULL_WINDOW})
                    window = PULL_WINDOW
                chunk = await asyncio.wait_for(pending.chunks.get(), 30)
                if isinstance(chunk, Exception):
                    raise chunk
                if not chunk:
                    if delivered != pending.expected:
                        raise ViewerError(502, "resource_truncated")
                    break
                await authorize()
                window -= 1
                delivered += len(chunk)
                yield chunk
                if delivered == pending.expected:
                    break
        finally:
            await self.cancel(request_id)

    async def cancel(self, request_id: str):
        pending = self.pending.pop(request_id, None)
        if pending and not pending.metadata.done():
            pending.metadata.cancel()
        try:
            if pending and not self.closed:
                # ASGI disconnect cancellation must still reach the source FD.
                with anyio.CancelScope(shield=True):
                    await self.send({"type": "cancel", "request_id": request_id})
        except Exception:
            pass
        finally:
            if pending:
                self.read_slots.release()

    async def receive(self):
        try:
            while True:
                frame = await self.ws.receive()
                if frame["type"] == "websocket.disconnect":
                    break
                if frame.get("bytes") is not None:
                    raw = frame["bytes"]
                    if not 16 <= len(raw) <= 16 + CHUNK_SIZE:
                        raise ValueError("invalid Viewer chunk")
                    pending = self.pending.get(raw[:16].hex())
                    if pending:
                        if not pending.metadata.done() or pending.credit <= 0:
                            raise ValueError("unsolicited Viewer data")
                        pending.credit -= 1
                        pending.received += len(raw) - 16
                        if pending.received > pending.expected:
                            raise ValueError("resource exceeds declared size")
                        pending.chunks.put_nowait(raw[16:])
                    continue
                raw = frame.get("text", "")
                if len(raw) > 64 * 1024:
                    raise ValueError("Viewer metadata too large")
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise ValueError("invalid Viewer frame")
                if message.get("type") == "catalog":
                    self.sites = parse_catalog(message.get("sites"))
                    continue
                if message.get("type") == "metadata":
                    future = self.metadata_pending.get(message.get("request_id"))
                    if future is not None and not future.done():
                        if message.get("error"):
                            future.set_exception(ViewerError(409, "page_request_failed"))
                        else:
                            future.set_result(message.get("result"))
                    continue
                pending = self.pending.get(message.get("request_id"))
                if pending is None:
                    continue
                if message.get("type") == "failed":
                    status = message.get("status")
                    if status not in {403, 404, 429, 502}:
                        status = 502
                    error = ViewerError(status, "resource_unavailable")
                    if not pending.metadata.done():
                        pending.metadata.set_exception(error)
                    else:
                        pending.chunks.put_nowait(error)
                elif message.get("type") == "response" and not pending.metadata.done():
                    status = message.get("status")
                    headers = message.get("headers")
                    if (status not in {200, 206, 304, 416} or not isinstance(headers, dict)
                            or len(headers) > 5 or any(
                                key not in {"content-type", "content-length", "etag",
                                            "accept-ranges", "content-range"}
                                or not isinstance(value, str) or len(value) > 256
                                or any(ord(c) < 32 for c in value)
                                for key, value in headers.items())):
                        raise ValueError("invalid resource metadata")
                    size = int(headers.get("content-length", "0"))
                    if not 0 <= size <= MAX_FILE_BYTES:
                        raise ValueError("invalid resource size")
                    if status in {200, 206} and not {"content-length", "content-type"} <= headers.keys():
                        raise ValueError("incomplete resource metadata")
                    pending.expected = size if not pending.head and status in {200, 206} else 0
                    pending.metadata.set_result((status, headers))
                else:
                    raise ValueError("invalid Viewer frame")
        finally:
            self.closed = True
            for future in self.metadata_pending.values():
                if not future.done():
                    future.set_exception(ViewerError(503, "device_offline"))
            for pending in self.pending.values():
                error = ViewerError(503, "device_offline")
                if not pending.metadata.done():
                    pending.metadata.set_exception(error)
                elif not pending.chunks.full():
                    pending.chunks.put_nowait(error)


@dataclass
class Grant:
    id: str
    claims: object
    machine_id: str
    parent_machine_id: str
    sid: str
    site: PublicSite
    peer: ViewerPeer
    expires: float
    maximum_expires: float
    # One cookie per grant, authorized only by the main-origin parent handshake.
    cookie_hash: str | None = None
    mode: str = "isolated"
    parent_origin: str = ""
    entry: str = ""


@dataclass
class Challenge:
    grant_id: str
    cookie_hash: str
    expires: float


class ViewerRelay:
    def __init__(self, cfg, active_claims, session_active, allow_machine, origin_allowed):
        self.cfg = cfg
        self.active_claims = active_claims
        self.session_active = session_active
        self.allow_machine = allow_machine
        self.origin_allowed = origin_allowed
        self.bridges: dict[object, object] = {}
        self.peers: dict[str, ViewerPeer] = {}
        self.grants: dict[str, Grant] = {}
        self.challenges: dict[str, Challenge] = {}
        self.api_slots = asyncio.Semaphore(32)

    def prune(self):
        now = time.time()
        self.grants = {key: value for key, value in self.grants.items() if value.expires > now}
        self.challenges = {key: value for key, value in self.challenges.items()
                           if value.expires > now and value.grant_id in self.grants}

    def origin(self, grant_id: str) -> str:
        return self.cfg.viewer_origin_template.replace("{id}", grant_id)

    async def disconnect_machine(self, machine_id: str):
        self.grants = {key: grant for key, grant in self.grants.items()
                       if machine_id not in {grant.machine_id, grant.parent_machine_id}}
        peer = self.peers.get(machine_id)
        if peer:
            peer.closed = True
            try:
                await peer.ws.close(code=1008, reason="device revoked")
            except Exception:
                pass

    def host_grant(self, req: Request) -> str | None:
        if effective_viewer_mode(self.cfg) != "isolated":
            return None
        target = urlsplit(self.origin("placeholder"))
        hostname = (req.url.hostname or "").lower()
        suffix = target.hostname.removeprefix("placeholder")
        if not hostname.endswith(suffix):
            return None
        grant_id = hostname[:-len(suffix)]
        if (not REQUEST_ID_RE.fullmatch(grant_id)
                or req.url.scheme != target.scheme
                or (req.url.port or (443 if req.url.scheme == "https" else 80))
                != (target.port or (443 if target.scheme == "https" else 80))):
            return ""  # A Viewer host must never fall through to the main app.
        return grant_id

    async def authorize(self, grant: Grant):
        if (grant.expires <= time.time() or self.grants.get(grant.id) is not grant
                or not await self.session_active(grant.claims)):
            raise ViewerError(401, "preview_expired")
        if (not await self.allow_machine(grant.claims, grant.machine_id)
                or not await self.allow_machine(grant.claims, grant.parent_machine_id)):
            raise ViewerError(403, "device_not_authorized")
        if self.peers.get(grant.machine_id) is not grant.peer or grant.peer.closed:
            raise ViewerError(503, "device_offline")
        if grant.peer.publication(grant.site.id) != grant.site:
            raise ViewerError(410, "publication_changed")

    async def serve_wrapper(self, ws: WebSocket, scope, authorize):
        machine_id = None
        peer = None
        try:
            first = await asyncio.wait_for(ws.receive_text(), 10)
            if len(first) > 64 * 1024:
                raise ValueError("catalog too large")
            hello = json.loads(first)
            if (not isinstance(hello, dict) or hello.get("type") != "hello"
                    or hello.get("v") != PROTOCOL_VERSION):
                raise ValueError("Viewer protocol mismatch")
            machine_id = hello.get("machine_id")
            from cc_remote.config import valid_machine_id
            if (not isinstance(machine_id, str) or not valid_machine_id(machine_id)
                    or (scope != "*" and scope != machine_id)
                    or not await authorize(machine_id)):
                raise ValueError("device not authorized")
            if machine_id in self.peers or len(self.peers) >= 256:
                raise ValueError("Viewer channel already connected or full")
            peer = ViewerPeer(ws, parse_catalog(hello.get("sites")))
            peer.session_pages = hello.get("session_pages") is True
            peer.media_previews = hello.get("media_previews") is True
            self.peers[machine_id] = peer
            async def check_credential():
                while True:
                    await asyncio.sleep(5)
                    if not await authorize(machine_id):
                        await ws.close(code=1008)
                        return
            guard = asyncio.create_task(check_credential())
            try:
                await peer.receive()
            finally:
                guard.cancel()
                await asyncio.gather(guard, return_exceptions=True)
        except Exception:
            try:
                await ws.close(code=1008, reason="Viewer channel closed")
            except Exception:
                pass
        finally:
            if peer and self.peers.get(machine_id) is peer:
                del self.peers[machine_id]

    async def api(self, req: Request):
        if self.api_slots.locked():
            raise ViewerError(429, "preview_capacity")
        async with self.api_slots:
            return await self._api(req)

    async def _api(self, req: Request):
        claims = await self.active_claims(req)
        if claims is None:
            raise ViewerError(401, "session_expired")
        self.prune()
        path = req.url.path
        if not self.origin_allowed(req):
            raise ViewerError(403, "origin_rejected")
        if path == "/api/viewers" and req.method == "GET":
            sites = []
            for machine_id, peer in list(self.peers.items()):
                if not peer.closed and await self.allow_machine(claims, machine_id):
                    sites.extend({**site.model_dump(), "machine_id": machine_id}
                                 for site in peer.sites.values())
            mode = effective_viewer_mode(self.cfg)
            return JSONResponse({"enabled": mode != "off", "mode": mode,
                                 "sites": sites if mode != "off" else []})
        if req.method != "GET" and not req.headers.get("origin"):
            raise ViewerError(403, "origin_rejected")
        request_origin = req.headers.get("origin") or str(req.base_url).rstrip("/")
        mode = effective_viewer_mode(self.cfg)
        if mode == "off":
            raise ViewerError(503, "viewer_not_configured")
        if mode == "isolated" and request_origin != self.cfg.public_origin:
            raise ViewerError(403, "origin_rejected")
        if req.method == "POST" and path == "/api/viewers/pages":
            from cc_remote.relay.viewer_pages import page_api
            body = bytearray()
            async def read_page_body():
                async for chunk in req.stream():
                    if len(body) + len(chunk) > 48 * 1024:
                        raise ViewerError(413, "request_too_large")
                    body.extend(chunk)
            await asyncio.wait_for(read_page_body(), 5)
            return JSONResponse(await page_api(self, claims, json.loads(body)))
        if req.method == "POST" and path == "/api/viewers/open":
            # Strict finite body read, including slow/chunked uploads.
            body = bytearray()
            async def read_body():
                async for chunk in req.stream():
                    if len(body) + len(chunk) > 4096:
                        raise ViewerError(413, "request_too_large")
                    body.extend(chunk)
            await asyncio.wait_for(read_body(), 5)
            payload = json.loads(body)
            if not isinstance(payload, dict) or set(payload) not in (
                    {"machine_id", "site_id", "parent_machine_id", "sid"},
                    {"machine_id", "site_id", "parent_machine_id", "sid", "entry"}):
                raise ViewerError(400, "invalid_request")
            machine_id, parent = payload["machine_id"], payload["parent_machine_id"]
            sid, site_id = payload["sid"], payload["site_id"]
            if (not all(isinstance(v, str) and 1 <= len(v) <= 128
                        and not any(ord(c) < 32 for c in v)
                        for v in (machine_id, parent, sid, site_id))
                    or not ID_RE.fullmatch(site_id)):
                raise ViewerError(400, "invalid_request")
            if (not await self.allow_machine(claims, machine_id)
                    or not await self.allow_machine(claims, parent)):
                raise ViewerError(403, "device_not_authorized")
            peer = self.peers.get(machine_id)
            if peer is None or peer.closed:
                raise ViewerError(503, "device_offline")
            site = peer.publication(site_id)
            entry = payload.get("entry", site.entry if site else None)
            if site is None or site_id.startswith("auto-") or entry != site.entry:
                if not isinstance(entry, str):
                    raise ViewerError(404, "publication_missing")
                confirmed = await peer.metadata("verify", {"entries": [{"site_id": site_id, "entry": entry}]})
                if not confirmed or confirmed[0].get("site_id") != site_id:
                    raise ViewerError(404, "publication_missing")
                entry = confirmed[0]["entry"]
                site = peer.publication(site_id)
            if site is None:
                raise ViewerError(404, "publication_missing")
            if self.peers.get(machine_id) is not peer or peer.closed:
                raise ViewerError(503, "device_offline")
            if len(self.grants) >= MAX_GRANTS or sum(
                    grant.claims.jti == claims.jti for grant in self.grants.values()) >= 16:
                raise ViewerError(429, "preview_capacity")
            grant_id = secrets.token_hex(16)
            grant = Grant(grant_id, claims, machine_id, parent, sid, site, peer,
                          min(time.time() + LEASE_TTL, claims.expires_at),
                          min(time.time() + GRANT_TTL, claims.expires_at),
                          mode=mode, parent_origin=req.headers["origin"], entry=entry)
            self.grants[grant_id] = grant
            result = {"id": grant_id, "mode": mode, "entry": entry,
                      "expires_at": grant.expires, "script_origins": site.script_origins}
            if mode == "isolated":
                result["origin"] = self.origin(grant_id)
            else:
                result["runner"] = f"/__cc_viewer/bridge/{grant_id}"
            return JSONResponse(result)
        match = re.fullmatch(r"/api/viewers/([a-f0-9]{32})(?:/([a-f0-9]{32}))?", path)
        if match:
            grant = self.grants.get(match[1])
            if (grant is None or grant.claims.jti != claims.jti
                    or grant.parent_origin != request_origin):
                raise ViewerError(404, "preview_missing")
            if req.method == "DELETE" and not match[2]:
                del self.grants[grant.id]
                return JSONResponse({"ok": True})
            await self.authorize(grant)
            if req.method == "POST" and match[2]:
                if grant.mode != "isolated":
                    raise ViewerError(409, "preview_handshake_expired")
                challenge = self.challenges.pop(match[2], None)
                if challenge is None or challenge.grant_id != grant.id:
                    raise ViewerError(409, "preview_handshake_expired")
                if grant.cookie_hash is not None:
                    raise ViewerError(409, "preview_already_open")
                grant.cookie_hash = challenge.cookie_hash
                return JSONResponse({"ok": True})
            if req.method == "GET" and not match[2]:
                grant.expires = min(time.time() + LEASE_TTL, grant.maximum_expires)
                return JSONResponse({"ok": True, "expires_at": grant.expires})
        raise ViewerError(404, "not_found")

    async def host(self, req: Request, grant_id: str):
        self.prune()
        grant = self.grants.get(grant_id)
        if grant is None:
            raise ViewerError(410, "preview_expired")
        await self.authorize(grant)
        if req.method not in {"GET", "HEAD"}:
            raise ViewerError(405, "read_only")
        path = req.url.path
        secure = req.url.scheme == "https"
        cookie_name = COOKIE if secure else "cc_remote_viewer"
        if path == PREFIX + "bootstrap" and req.method == "GET":
            if grant.cookie_hash is not None:
                raise ViewerError(409, "preview_already_open")
            if len(self.challenges) >= MAX_PENDING or sum(
                    item.grant_id == grant.id for item in self.challenges.values()) >= 8:
                raise ViewerError(429, "preview_capacity")
            cookie = secrets.token_urlsafe(32)
            challenge_id = secrets.token_hex(16)
            self.challenges[challenge_id] = Challenge(
                grant.id, hashlib.sha256(cookie.encode()).hexdigest(), time.time() + 30)
            # JSON values are configuration / validated paths, never HTML literals.
            data = json.dumps({"origin": self.cfg.public_origin,
                               "challenge": challenge_id, "id": grant.id,
                               "entry": quote(grant.entry or grant.site.entry, safe="/")}).replace("<", "\\u003c")
            response = HTMLResponse(bootstrap_document(data))
            response.set_cookie(cookie_name, cookie, max_age=GRANT_TTL, path="/",
                                httponly=True, secure=secure, samesite="strict")
            return response
        cookie = req.cookies.get(cookie_name, "")
        if (not cookie or not grant.cookie_hash or not secrets.compare_digest(
                hashlib.sha256(cookie.encode()).hexdigest(), grant.cookie_hash)):
            raise ViewerError(401, "preview_not_authorized")
        if path == PREFIX + "ready":
            return JSONResponse({"ok": True})
        if path.startswith(PREFIX):
            raise ViewerError(404, "not_found")
        # Validate the original encoded path here and at the wrapper: a single
        # decode must never introduce traversal or another escape sequence.
        # Query strings are cache busters, never file paths.
        raw_path = req.scope.get("raw_path", path.encode()).decode("ascii")
        clean_path(raw_path)
        headers = {name: req.headers[name] for name in
                   ("range", "if-none-match", "if-range") if name in req.headers}
        if any(len(value) > 256 for value in headers.values()):
            raise ViewerError(400, "invalid_headers")
        # A navigated document is a separate representation from raw resource
        # fetches. Do not reuse a raw cached/ranged HTML body without the helper.
        destination = req.headers.get("sec-fetch-dest")
        navigation = (path.lower().endswith((".html", ".htm")) and (
            destination == "iframe" or (not destination
                and req.headers.get("upgrade-insecure-requests") == "1"
                and "text/html" in req.headers.get("accept", ""))))
        if navigation:
            headers = {}
        request_id, status, response_headers = await grant.peer.read(
            grant.site, raw_path, req.method, headers)
        try:
            await self.authorize(grant)
        except BaseException:
            await grant.peer.cancel(request_id)
            raise
        helper = b""
        if response_headers.get("content-type", "").split(";", 1)[0] == "text/html":
            response_headers = {**response_headers,
                                "vary": "Sec-Fetch-Dest, Accept, Upgrade-Insecure-Requests"}
        if navigation and status == 200 and "vary" in response_headers:
            helper = PREVIEW_ESCAPE_SCRIPT
            # Injection depends on the streamed BOM. The transformed length is
            # unknown until then; let HTTP frame the stream instead of guessing.
            response_headers.pop("content-length", None)
            response_headers.pop("etag", None)
            response_headers.pop("accept-ranges", None)
        if req.method == "HEAD" or status in {304, 416}:
            await grant.peer.cancel(request_id)
            response = Response(status_code=status, headers=response_headers)
            if helper:
                # Response otherwise inserts zero, which is not the GET length.
                del response.headers["content-length"]
            return response
        async def authorized():
            await self.authorize(grant)
        stream = grant.peer.stream(request_id, authorized)
        async def body():
            try:
                async for chunk in preview_html(stream, helper):
                    yield chunk
            finally:
                await stream.aclose()
        return StreamingResponse(body() if helper else stream,
                                 status_code=status, headers=response_headers)

    def headers(self, grant_id: str):
        grant = self.grants.get(grant_id)
        scripts = " ".join(grant.site.script_origins) if grant else ""
        return {
            "Content-Security-Policy": (
                "default-src 'none'; script-src 'self' 'unsafe-inline' " + scripts + "; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                "font-src 'self' data:; media-src 'self' data: blob:; "
                "connect-src 'self'; worker-src 'self' blob:; "
                "object-src 'none'; frame-src 'none'; base-uri 'self'; form-action 'none'; "
                "sandbox allow-scripts allow-same-origin; frame-ancestors " + self.cfg.public_origin),
            "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-cache",
        }


def bootstrap_document(data: str) -> str:
    return """<!doctype html><html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>连接预览</title><style>body{font:14px system-ui;color:#68758a;display:grid;place-items:center;
height:95vh;margin:0;background:#f8fafc}p{padding:24px;text-align:center}</style>
<p id="status">正在连接远程预览…</p><script>
const config = """ + data + """;
let stopped=false;
const fail = message => { stopped=true; document.getElementById('status').textContent=message;
parent.postMessage({type:'cc-viewer-error',id:config.id,message},config.origin); };
if(parent===window) fail('请从 cc-remote 打开预览');
else {
  parent.postMessage({type:'cc-viewer-ready',id:config.id,challenge:config.challenge},config.origin);
  const started=Date.now();
  const poll=async()=>{
    if(stopped)return;
    try {
      const response=await fetch('/__cc_viewer/ready',{cache:'no-store',credentials:'same-origin'});
      if(response.ok){location.replace(config.entry);return;}
      if(![401].includes(response.status)){fail('预览已失效，请重新打开');return;}
    } catch {}
    if(Date.now()-started>25000){fail('无法连接预览，请检查网络或浏览器 Cookie 设置');return;}
    setTimeout(poll,400);
  };
  poll();
}
</script></html>"""
