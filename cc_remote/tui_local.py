"""Discover a same-user local relay without weakening its authentication.

Only the active user service's own process environment supplies credentials.
They are never copied to disk, logged, or sent to an unrelated URL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit


_ENV_LIMIT = 1024 * 1024
_KEYS = {
    "RELAY_HOST", "RELAY_PORT", "PUBLIC_ORIGIN", "LOGIN_PASSWORD",
    "LOGIN_USERNAME", "LOGIN_USERS_JSON", "ALLOW_PRIVATE_ORIGINS",
}


@dataclass(frozen=True)
class LocalRelay:
    url: str
    password: str = field(repr=False)
    origin: str = ""
    username: str = ""


def is_loopback_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        return (parsed.scheme in {"ws", "wss"}
                and (host == "localhost" or ipaddress.ip_address(host).is_loopback))
    except ValueError:
        return False


def same_local_endpoint(left: str, right: str) -> bool:
    try:
        a, b = urlsplit(left), urlsplit(right)
        return (
            is_loopback_url(left) and is_loopback_url(right)
            and a.scheme == b.scheme == "ws"
            and (a.port or 80) == (b.port or 80)
            and a.path == b.path == "/ws"
            and not any((a.username, a.password, a.query, a.fragment,
                         b.username, b.password, b.query, b.fragment))
            and (a.hostname == b.hostname
                 or {a.hostname, b.hostname} <= {"localhost", "127.0.0.1"})
        )
    except ValueError:
        return False


def _service_environment() -> dict[str, str]:
    if sys.platform != "linux":
        return {}
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", "cc-remote-relay.service",
             "--property=MainPID", "--property=ActiveState"],
            capture_output=True, text=True, timeout=2, check=True,
        )
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines()
                      if "=" in line)
        pid = fields.get("MainPID", "")
        if fields.get("ActiveState") != "active" or not pid.isdecimal() or int(pid) < 1:
            return {}
        directory = os.open(Path("/proc") / pid, os.O_RDONLY | os.O_DIRECTORY)
        try:
            if os.fstat(directory).st_uid != os.getuid():
                return {}
            descriptor = os.open("environ", os.O_RDONLY | os.O_NOFOLLOW,
                                 dir_fd=directory)
            with os.fdopen(descriptor, "rb") as stream:
                if os.fstat(stream.fileno()).st_uid != os.getuid():
                    return {}
                raw = stream.read(_ENV_LIMIT + 1)
        finally:
            os.close(directory)
        if len(raw) > _ENV_LIMIT:
            return {}
        settings = {}
        for entry in raw.split(b"\0"):
            key, separator, value = entry.partition(b"=")
            name = key.decode("ascii", errors="replace")
            if separator and name in _KEYS:
                settings[name] = value.decode("utf-8")
        return settings
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return {}


def discover_local_relay() -> LocalRelay | None:
    settings = _service_environment()
    password = settings.get("LOGIN_PASSWORD", "")
    # Multi-user deployments require an explicitly selected login; never pick
    # an account or inherit a legacy password that the relay no longer accepts.
    if not password or settings.get("LOGIN_USERS_JSON", "").strip():
        return None
    host = settings.get("RELAY_HOST", "127.0.0.1").strip()
    if host in {"0.0.0.0", "localhost"}:
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    try:
        address = ipaddress.ip_address(host)
        port = int(settings.get("RELAY_PORT", "8765"))
    except ValueError:
        return None
    if not address.is_loopback or not 1 <= port <= 65535:
        return None
    authority = f"[{host}]" if address.version == 6 else host
    target = f"{authority}:{port}"
    origin = f"http://{target}"
    private_origins = settings.get("ALLOW_PRIVATE_ORIGINS", "").strip().lower()
    if (private_origins not in {"1", "true", "yes", "on"}
            and settings.get("PUBLIC_ORIGIN", "").rstrip("/") != origin):
        return None
    return LocalRelay(
        url=f"ws://{target}/ws", password=password,
        origin=origin,
        username=settings.get("LOGIN_USERNAME", ""),
    )
