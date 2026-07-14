"""Cookie authentication helper for live client/e2e scripts.

These helpers deliberately require LOGIN_PASSWORD.  The relay no longer
accepts the legacy static CLIENT_TOKEN or credentials in a WebSocket URL.
"""
from __future__ import annotations

import asyncio
import json
from http.cookies import SimpleCookie
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from cc_remote.relay.auth import SESSION_COOKIE_NAME
from websockets.asyncio.client import connect


def _origin(ws_url: str) -> str:
    parsed = urlsplit(ws_url)
    if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
        raise ValueError("RELAY_URL must be an absolute ws:// or wss:// URL")
    scheme = "https" if parsed.scheme == "wss" else "http"
    return urlunsplit((scheme, parsed.netloc, "", "", ""))


def _login_cookie(ws_url: str, password: str) -> str:
    if not password:
        raise RuntimeError("LOGIN_PASSWORD is required for client/e2e authentication")
    origin = _origin(ws_url)
    request = Request(
        f"{origin}/api/login",
        data=json.dumps({"password": password}).encode(),
        headers={"Content-Type": "application/json", "Origin": origin},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        raw_cookie = response.headers.get("Set-Cookie", "")
    cookies = SimpleCookie()
    cookies.load(raw_cookie)
    session = cookies.get(SESSION_COOKIE_NAME)
    if session is None or not session.value:
        raise RuntimeError("relay login response did not include a session cookie")
    return f"{SESSION_COOKIE_NAME}={session.value}"


async def client_connection(ws_url: str, password: str):
    """Return an authenticated websockets connection context manager."""
    cookie = await asyncio.to_thread(_login_cookie, ws_url, password)
    return connect(
        ws_url,
        origin=_origin(ws_url),
        additional_headers={"Cookie": cookie},
    )
