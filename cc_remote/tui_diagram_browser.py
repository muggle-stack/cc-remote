"""User-triggered Web handoff; no source uploads, credentials or callbacks."""

import asyncio
import json
import re
from urllib.parse import quote, urlsplit, urlunsplit
import webbrowser

WIRE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z")


def session_browser_url(client, sid):
    """Reuse Web's notification route; browser login/device checks still apply."""
    base = urlsplit(client.web_url())
    row = client.workspace.catalog.get(sid, {})
    route = {
        "machine_id": client.machine_id,
        "session_id": sid,
        "engine": row.get("engine", client.engine),
        "space": row.get("space", client.space),
    }
    if (
        base.scheme not in {"http", "https"}
        or not base.hostname
        or base.username is not None
        or base.password is not None
        or any(
            not isinstance(route[key], str) or not WIRE_ID.fullmatch(route[key])
            for key in ("machine_id", "session_id")
        )
        or route["engine"] not in {"claude", "codex"}
        or route["space"] not in {"code", "work"}
    ):
        raise ValueError("Select a valid session and HTTP(S) Web endpoint")
    fragment = "notification=" + quote(
        json.dumps(route, separators=(",", ":")), safe=""
    )
    return urlunsplit(
        (base.scheme, base.netloc, base.path or "/", "", fragment)
    )


async def open_session_browser(client, sid):
    url = session_browser_url(client, sid)
    try:
        opened = await asyncio.to_thread(webbrowser.open, url, new=2)
    except (webbrowser.Error, OSError):
        opened = False
    return (
        "Opened session in browser"
        if opened
        else "No browser available; open manually: " + url
    )
