"""Authenticated native video responses over the bounded Viewer binary channel."""
from __future__ import annotations

import asyncio
import re
import time

from fastapi.responses import JSONResponse, Response, StreamingResponse

from cc_remote.config import valid_machine_id
from cc_remote.relay.viewer import ViewerError

MEDIA_ID_RE = re.compile(r"[a-f0-9]{32}\Z")
PRIVATE_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
                   "Cross-Origin-Resource-Policy": "same-origin"}


class MediaResponse(StreamingResponse):
    def __init__(self, peer, request_id, authorize, **kwargs):
        super().__init__(peer.stream(request_id, authorize), **kwargs)
        self.peer = peer
        self.request_id = request_id

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Also release when disconnected before the body iterator starts.
            await self.peer.cancel(self.request_id)


async def media_response(req, viewers, machine_id, media_id, owner_id):
    peer = None
    request_id = None
    try:
        if (not valid_machine_id(machine_id) or not MEDIA_ID_RE.fullmatch(media_id)
                or req.method not in {"GET", "HEAD"}):
            raise ViewerError(404, "video_unavailable")
        if (not viewers.origin_allowed(req)
                or req.headers.get("sec-fetch-site", "same-origin") not in {"same-origin", "none"}):
            raise ViewerError(403, "origin_denied")
        claims = await viewers.active_claims(req)
        if claims is None:
            raise ViewerError(401, "login_required")
        peer = viewers.peers.get(machine_id)

        async def authorize():
            if claims.expires_at <= time.time() or not await viewers.session_active(claims):
                raise ViewerError(401, "login_required")
            if not await viewers.allow_machine(claims, machine_id):
                raise ViewerError(403, "device_denied")
            if peer is None or peer.closed or viewers.peers.get(machine_id) is not peer:
                raise ViewerError(503, "device_offline")

        await authorize()
        headers = {name: req.headers[name] for name in ("range", "if-range", "if-none-match")
                   if name in req.headers}
        if any(len(value) > 256 for value in headers.values()):
            raise ViewerError(400, "invalid_range")
        request_id, status, response_headers = await peer.read_media(
            media_id, owner_id(claims), req.method, headers)
        await authorize()
        if status != 416 and response_headers.get("content-type") != "video/mp4":
            raise ViewerError(502, "invalid_video_type")
        response_headers = {**response_headers, "content-type": "video/mp4", **PRIVATE_HEADERS}
        response_headers["content-disposition"] = (
            'attachment; filename="video.mp4"' if req.query_params.get("download") == "1" else "inline")
        if req.method == "HEAD" or status in {304, 416}:
            await peer.cancel(request_id)
            request_id = None
            return Response(status_code=status, headers=response_headers)
        response = MediaResponse(peer, request_id, authorize,
                                 status_code=status, headers=response_headers)
        request_id = None  # The response owns cancellation from here on.
        return response
    except (ViewerError, ValueError, TimeoutError) as exc:
        status = exc.status if isinstance(exc, ViewerError) else 502
        code = exc.code if isinstance(exc, ViewerError) else "video_unavailable"
        return JSONResponse({"error": code}, status_code=status, headers=PRIVATE_HEADERS)
    finally:
        if request_id is not None and peer is not None:
            await asyncio.shield(peer.cancel(request_id))
