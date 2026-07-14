"""Environment-driven configuration. No hardcoded hosts.

Load order: real environment vars win; a local .env file is loaded if present
(python-dotenv) so the wrapper/relay can be run from a project .env during
local development. Everything is portable so the relay can move to a VPS with
only env changes.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _int(key: str, default: int) -> int:
    v = os.environ.get(key)
    return int(v) if v and v.strip() else default


def _float(key: str, default: float) -> float:
    v = os.environ.get(key)
    return float(v) if v and v.strip() else default


@dataclass
class RelayConfig:
    host: str = field(default_factory=lambda: _env("RELAY_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _int("RELAY_PORT", 8765))
    wrapper_token: str = field(default_factory=lambda: _env("WRAPPER_TOKEN", "change-me-wrapper"))
    # Optional path to a built web client (web/dist) to serve from the same origin.
    static_dir: str = field(default_factory=lambda: _env("WEB_STATIC_DIR", ""))
    # Hard per-client queue limits. A client that exceeds either limit is
    # disconnected; individual deltas are never shed because that would make a
    # partial answer look complete.
    client_queue_cap: int = field(default_factory=lambda: _int("CLIENT_QUEUE_CAP", 4096))
    client_queue_bytes: int = field(
        default_factory=lambda: _int("CLIENT_QUEUE_BYTES", 16 * 1024 * 1024)
    )
    # Maximum decoded WebSocket text frame accepted by the relay. Attachment
    # commands are validated more tightly by the wrapper after transport.
    ws_max_size_bytes: int = field(
        default_factory=lambda: _int("WS_MAX_SIZE_BYTES", 16 * 1024 * 1024)
    )
    # Login gate: web clients POST /api/login with this password and receive a
    # short-lived HMAC session in an HttpOnly cookie.
    login_password: str = field(default_factory=lambda: _env("LOGIN_PASSWORD", ""))
    session_secret: str = field(default_factory=lambda: _env("SESSION_SECRET", ""))
    session_ttl_seconds: int = field(default_factory=lambda: _int("SESSION_TTL_SECONDS", 7 * 24 * 3600))
    login_body_max_bytes: int = field(default_factory=lambda: _int("LOGIN_BODY_MAX_BYTES", 4096))
    # A byte limit alone does not stop slow chunked bodies from retaining an
    # ASGI task forever.  Bound both the wall-clock read and the number of
    # bodies that may be read concurrently.
    login_read_timeout: float = field(default_factory=lambda: _float("LOGIN_READ_TIMEOUT", 10.0))
    login_inflight_cap: int = field(default_factory=lambda: _int("LOGIN_INFLIGHT_CAP", 32))
    session_registry_cap: int = field(default_factory=lambda: _int("SESSION_REGISTRY_CAP", 1024))
    # Total accepted browser/TUI sockets, including clients still waiting for
    # their mandatory first Hello frame.
    max_clients: int = field(default_factory=lambda: _int("MAX_CLIENTS", 8))
    client_hello_timeout: float = field(default_factory=lambda: _float("CLIENT_HELLO_TIMEOUT", 10.0))
    # Exact browser Origin accepted for cookie-authenticated WebSockets, for
    # example https://remote.example.com (no path or trailing slash).
    public_origin: str = field(default_factory=lambda: _env("PUBLIC_ORIGIN", ""))


@dataclass
class WrapperConfig:
    relay_url: str = field(default_factory=lambda: _env("RELAY_URL", "ws://127.0.0.1:8765/ws"))
    # Token the wrapper presents to the relay at WS upgrade (must match the
    # relay's WRAPPER_TOKEN). Same env name as the relay for convenience.
    wrapper_token: str = field(default_factory=lambda: _env("WRAPPER_TOKEN", "change-me-wrapper"))
    # Optional explicit Claude Code executable. Blank preserves the existing
    # SDK/PATH discovery behavior.
    claude_bin: str = field(default_factory=lambda: _env("CLAUDE_BIN", "").strip())
    # cwd for the cc session. MUST match the resumed session's cwd, otherwise
    # --resume cannot locate the session jsonl under ~/.claude/projects/.
    cc_cwd: str = field(default_factory=lambda: _env("CC_CWD", os.getcwd()))
    resume_session_id: str = field(default_factory=lambda: _env("CC_RESUME_SESSION_ID", ""))
    ring_max_events: int = field(default_factory=lambda: _int("RING_MAX_EVENTS", 10000))
    ring_max_bytes: int = field(default_factory=lambda: _int("RING_MAX_BYTES", 24 * 1024 * 1024))
    tool_result_max: int = field(default_factory=lambda: _int("TOOL_RESULT_MAX", 65536))
    history_source_max_bytes: int = field(
        default_factory=lambda: _int("HISTORY_SOURCE_MAX_BYTES", 64 * 1024 * 1024)
    )
    # Bound relay-facing queues and inbound frames. The frame cap must be large
    # enough for an encoded attachment command, but remains finite so one
    # authenticated client cannot grow the wrapper without limit.
    transport_inbox_cap: int = field(default_factory=lambda: _int("WRAPPER_INBOX_CAP", 1024))
    transport_send_cap: int = field(default_factory=lambda: _int("WRAPPER_SEND_QUEUE_CAP", 8192))
    transport_inbox_bytes: int = field(
        default_factory=lambda: _int("WRAPPER_INBOX_BYTES", 32 * 1024 * 1024)
    )
    transport_send_bytes: int = field(
        default_factory=lambda: _int("WRAPPER_SEND_QUEUE_BYTES", 32 * 1024 * 1024)
    )
    ws_max_size_bytes: int = field(default_factory=lambda: _int("WS_MAX_SIZE_BYTES", 16 * 1024 * 1024))
    turn_reader_queue_cap: int = field(
        default_factory=lambda: _int("TURN_READER_QUEUE_CAP", 4)
    )
    # Seconds to wait for the terminal ResultMessage after interrupt() before
    # forcing an SDK reconnect (drain safety net).
    drain_timeout: float = field(default_factory=lambda: _float("DRAIN_TIMEOUT", 15.0))
    # A Codex turn may legitimately run for a long time, so this is a warning,
    # not an automatic interrupt. Any raw app-server event resets the idle clock.
    # Set 0 to disable.
    codex_turn_idle_warn_seconds: float = field(
        default_factory=lambda: _float("CODEX_TURN_IDLE_WARN_SECONDS", 90.0)
    )
    # Max cc subprocesses (resident sessions) the wrapper runs concurrently. Each
    # session = one `claude --resume` child (~190MB RAM). Over the cap → evict an
    # idle one (client keeps its cached history; viewing stays instant). Raised
    # from 4 so browsing many sessions doesn't thrash-respawn. Tune via env.
    max_concurrent_sessions: int = field(default_factory=lambda: _int("MAX_CONCURRENT_SESSIONS", 20))
    state_dir: Path = field(default_factory=lambda: Path(_env("CC_REMOTE_STATE_DIR", str(Path.home() / ".cc-remote"))))


def relay_config() -> RelayConfig:
    return RelayConfig()


_PLACEHOLDER_PREFIXES = ("change-me", "changeme", "replace_with", "replace-with")


def _placeholder(value: str) -> bool:
    normalized = value.strip().lower()
    return not normalized or normalized.startswith(_PLACEHOLDER_PREFIXES)


def validate_relay_config(cfg: RelayConfig) -> None:
    """Fail closed for a real relay startup.

    Validation is deliberately separate from ``RelayConfig`` construction so
    unit tests and embedded/local app fixtures can build narrow configs. The
    relay entry point and the no-argument app factory call this before serving.
    """
    errors: list[str] = []
    if _placeholder(cfg.login_password) or len(cfg.login_password) < 12:
        errors.append("LOGIN_PASSWORD must be non-placeholder and at least 12 characters")
    if _placeholder(cfg.session_secret) or len(cfg.session_secret) < 32:
        errors.append("SESSION_SECRET must be non-placeholder and at least 32 characters")
    if _placeholder(cfg.wrapper_token) or len(cfg.wrapper_token) < 32:
        errors.append("WRAPPER_TOKEN must be non-placeholder and at least 32 characters")
    if cfg.session_ttl_seconds <= 0:
        errors.append("SESSION_TTL_SECONDS must be positive")
    if not (256 <= cfg.login_body_max_bytes <= 64 * 1024):
        errors.append("LOGIN_BODY_MAX_BYTES must be between 256 and 65536")
    if not math.isfinite(cfg.login_read_timeout) or not (1 <= cfg.login_read_timeout <= 60):
        errors.append("LOGIN_READ_TIMEOUT must be between 1 and 60 seconds")
    if not (1 <= cfg.login_inflight_cap <= 1024):
        errors.append("LOGIN_INFLIGHT_CAP must be between 1 and 1024")
    if not (1 <= cfg.session_registry_cap <= 1_000_000):
        errors.append("SESSION_REGISTRY_CAP must be between 1 and 1000000")
    if not (1 <= cfg.max_clients <= 64):
        errors.append("MAX_CLIENTS must be between 1 and 64")
    if cfg.client_hello_timeout <= 0:
        errors.append("CLIENT_HELLO_TIMEOUT must be positive")
    if not (1 <= cfg.client_queue_cap <= 65536):
        errors.append("CLIENT_QUEUE_CAP must be between 1 and 65536")
    if not (1024 <= cfg.client_queue_bytes <= 1024 * 1024 * 1024):
        errors.append("CLIENT_QUEUE_BYTES must be between 1024 and 1073741824")
    if not (12 * 1024 * 1024 <= cfg.ws_max_size_bytes <= 64 * 1024 * 1024):
        errors.append("WS_MAX_SIZE_BYTES must be between 12582912 and 67108864")
    if cfg.client_queue_bytes < cfg.ws_max_size_bytes:
        errors.append("CLIENT_QUEUE_BYTES must be at least WS_MAX_SIZE_BYTES")

    origin = cfg.public_origin.strip()
    try:
        parsed = urlsplit(origin)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        parsed = urlsplit("")
        hostname = None
        port = None
    if (
        not origin
        or parsed.scheme.lower() not in ("http", "https")
        or not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        errors.append("PUBLIC_ORIGIN must be an http(s) origin without a path")
    else:
        scheme = parsed.scheme.lower()
        try:
            canonical_host = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError:
            errors.append("PUBLIC_ORIGIN contains an invalid hostname")
        else:
            host_for_url = (
                f"[{canonical_host}]" if ":" in canonical_host else canonical_host
            )
            default_port = 443 if scheme == "https" else 80
            canonical_origin = f"{scheme}://{host_for_url}"
            if port is not None and port != default_port:
                canonical_origin += f":{port}"
            # Store exactly the serialization browsers use for Origin headers,
            # including lower-case host and omitted default port.
            cfg.public_origin = canonical_origin
            if scheme != "https" and canonical_host not in {
                "127.0.0.1", "::1", "localhost"
            }:
                errors.append("PUBLIC_ORIGIN must use https except on loopback")

    if errors:
        raise ValueError("invalid relay configuration: " + "; ".join(errors))


def wrapper_config() -> WrapperConfig:
    return WrapperConfig()


def validate_wrapper_config(cfg: WrapperConfig) -> None:
    """Reject credentials or relay URLs that could expose wrapper authority."""
    errors: list[str] = []
    if _placeholder(cfg.wrapper_token) or len(cfg.wrapper_token) < 32:
        errors.append("WRAPPER_TOKEN must be non-placeholder and at least 32 characters")

    parsed = urlsplit(cfg.relay_url)
    if (
        parsed.scheme not in ("ws", "wss")
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        errors.append("RELAY_URL must be a ws(s) URL without credentials, query, or fragment")
    else:
        loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        if parsed.scheme != "wss" and not loopback:
            errors.append("RELAY_URL must use wss except on loopback")
    if parsed.path != "/ws":
        errors.append("RELAY_URL path must be /ws")
    if (not cfg.cc_cwd or "\x00" in cfg.cc_cwd
            or len(cfg.cc_cwd.encode("utf-8", errors="surrogatepass")) > 4096):
        errors.append("CC_CWD must be a non-empty path of at most 4096 UTF-8 bytes")
    if (cfg.resume_session_id
            and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}",
                                 cfg.resume_session_id)):
        errors.append("CC_RESUME_SESSION_ID has an invalid format")
    if ("\x00" in cfg.claude_bin
            or len(cfg.claude_bin.encode("utf-8", errors="surrogatepass")) > 4096):
        errors.append("CLAUDE_BIN must be at most 4096 UTF-8 bytes")
    elif (cfg.claude_bin
          and not os.path.isabs(os.path.expanduser(cfg.claude_bin))):
        errors.append("CLAUDE_BIN must be an absolute path")

    if not (12 * 1024 * 1024 <= cfg.ws_max_size_bytes <= 64 * 1024 * 1024):
        errors.append("WS_MAX_SIZE_BYTES must be between 12582912 and 67108864")
    for name, value, upper in (
        ("WRAPPER_INBOX_CAP", cfg.transport_inbox_cap, 65536),
        ("WRAPPER_SEND_QUEUE_CAP", cfg.transport_send_cap, 65536),
        ("WRAPPER_INBOX_BYTES", cfg.transport_inbox_bytes, 1024 * 1024 * 1024),
        ("WRAPPER_SEND_QUEUE_BYTES", cfg.transport_send_bytes, 1024 * 1024 * 1024),
        ("TURN_READER_QUEUE_CAP", cfg.turn_reader_queue_cap, 1024),
    ):
        if not (1 <= value <= upper):
            errors.append(f"{name} must be between 1 and {upper}")
    if cfg.transport_inbox_bytes < cfg.ws_max_size_bytes:
        errors.append("WRAPPER_INBOX_BYTES must be at least WS_MAX_SIZE_BYTES")
    if cfg.transport_send_bytes < cfg.ws_max_size_bytes:
        errors.append("WRAPPER_SEND_QUEUE_BYTES must be at least WS_MAX_SIZE_BYTES")
    if not (1 <= cfg.max_concurrent_sessions <= 64):
        errors.append("MAX_CONCURRENT_SESSIONS must be between 1 and 64")
    if not (4 <= cfg.ring_max_events <= 1_000_000):
        errors.append("RING_MAX_EVENTS must be between 4 and 1000000")
    if not (cfg.ws_max_size_bytes <= cfg.ring_max_bytes <= 1024 * 1024 * 1024):
        errors.append(
            "RING_MAX_BYTES must be at least WS_MAX_SIZE_BYTES and at most 1073741824")
    if not (1024 <= cfg.tool_result_max <= 16 * 1024 * 1024):
        errors.append("TOOL_RESULT_MAX must be between 1024 and 16777216")
    elif cfg.tool_result_max > max(1024, (cfg.ws_max_size_bytes - 64 * 1024) // 4):
        errors.append(
            "TOOL_RESULT_MAX is too large for WS_MAX_SIZE_BYTES after UTF-8 encoding")
    if not (1024 * 1024 <= cfg.history_source_max_bytes <= 1024 * 1024 * 1024):
        errors.append("HISTORY_SOURCE_MAX_BYTES must be between 1048576 and 1073741824")
    if not (0 < cfg.drain_timeout <= 300):
        errors.append("DRAIN_TIMEOUT must be greater than 0 and at most 300")
    if (cfg.codex_turn_idle_warn_seconds != 0
            and not (5 <= cfg.codex_turn_idle_warn_seconds <= 3600)):
        errors.append(
            "CODEX_TURN_IDLE_WARN_SECONDS must be 0 or between 5 and 3600")

    if errors:
        raise ValueError("invalid wrapper configuration: " + "; ".join(errors))
