"""Environment-driven configuration. No hardcoded hosts.

Load order: real environment vars win; a local .env file is loaded if present
(python-dotenv) so the wrapper/relay can be run from a project .env during
local development. Everything is portable so the relay can move to a VPS with
only env changes.
"""
from __future__ import annotations

import math
import json
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

if sys.platform != "win32":
    from cc_remote.claude_broker.paths import default_socket_path


try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _claude_bin() -> str:
    """Use the user's normal Claude Code install unless explicitly overridden."""
    configured = _env("CLAUDE_BIN", "").strip()
    return configured or str(Path.home() / ".local/bin/claude")


def _int(key: str, default: int) -> int:
    v = os.environ.get(key)
    return int(v) if v and v.strip() else default


def _float(key: str, default: float) -> float:
    v = os.environ.get(key)
    return float(v) if v and v.strip() else default


def _bool(key: str, default: bool = False) -> bool:
    value = os.environ.get(key)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def device_config_path() -> Path:
    return Path(_env(
        "CC_REMOTE_DEVICE_CONFIG",
        str(Path.home() / ".cc-remote" / "device.json"),
    )).expanduser()


def _load_device_config() -> dict[str, str]:
    path = device_config_path()
    if not path.exists():
        return {}
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ValueError(
            f"device credential file must not be accessible by group/others: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"invalid device credential file: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"invalid device credential file: {path}")
    allowed = {"relay_url", "wrapper_token", "machine_id", "label"}
    return {
        key: value for key, value in payload.items()
        if key in allowed and isinstance(value, str)
    }


def _wrapper_value(env_key: str, file_key: str, default: str) -> str:
    explicit = os.environ.get(env_key)
    if explicit is not None and explicit.strip():
        return explicit
    return _load_device_config().get(file_key, default)


def _default_device_db_path() -> str:
    push_path = _env("PUSH_DB_PATH", "").strip()
    if push_path:
        return str(Path(push_path).expanduser().parent / "relay-devices.sqlite3")
    return str(Path.home() / ".cc-remote" / "relay-devices.sqlite3")


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
    # Optional multi-user policy. When configured it replaces LOGIN_PASSWORD:
    # {"alice":{"password":"...","machines":["mac","nono"]}}
    login_users_json: str = field(default_factory=lambda: _env("LOGIN_USERS_JSON", "").strip())
    # Optional per-machine wrapper credentials. When configured it replaces
    # the wildcard WRAPPER_TOKEN:
    # {"mac":"long-secret", "nono":"another-long-secret"}
    wrapper_tokens_json: str = field(default_factory=lambda: _env("WRAPPER_TOKENS_JSON", "").strip())
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
    # Optional same-port browser access through literal private/loopback IPs.
    # PUBLIC_ORIGIN remains the canonical external origin; this narrowly adds
    # direct LAN/Tailscale entry points without trusting arbitrary hostnames.
    allow_private_origins: bool = field(
        default_factory=lambda: _bool("ALLOW_PRIVATE_ORIGINS")
    )
    # Opt-in escape hatch: allow a non-loopback PUBLIC_ORIGIN/RELAY_URL to stay
    # on plain http(s)/ws(s) instead of requiring TLS. Off by default; turning
    # it on trades transport confidentiality (password, cookie, tokens, and all
    # traffic travel in cleartext) for being reachable over a bare public IP
    # without a TLS terminator in front.
    allow_insecure_http: bool = field(default_factory=lambda: _bool("ALLOW_INSECURE_HTTP"))
    # Optional Web Push. Configure all three VAPID values to deliver completion
    # notices even when no browser WebSocket is connected.
    push_vapid_public_key: str = field(
        default_factory=lambda: _env("PUSH_VAPID_PUBLIC_KEY", "").strip()
    )
    push_vapid_private_key: str = field(
        default_factory=lambda: _env("PUSH_VAPID_PRIVATE_KEY", "").strip()
    )
    push_vapid_subject: str = field(
        default_factory=lambda: _env("PUSH_VAPID_SUBJECT", "").strip()
    )
    push_db_path: str = field(default_factory=lambda: _env(
        "PUSH_DB_PATH", str(Path.home() / ".cc-remote" / "relay-push.sqlite3")))
    # Persistent enrollment metadata and hashed per-device credentials. This
    # database never contains conversations, artifacts, or plaintext tokens.
    device_db_path: str = field(default_factory=lambda: _env(
        "DEVICE_DB_PATH", _default_device_db_path()))
    device_pairing_ttl_seconds: int = field(
        default_factory=lambda: _int("DEVICE_PAIRING_TTL_SECONDS", 600))


@dataclass
class WrapperConfig:
    relay_url: str = field(default_factory=lambda: _wrapper_value(
        "RELAY_URL", "relay_url", "ws://127.0.0.1:8765/ws"))
    # Token the wrapper presents to the relay at WS upgrade (must match the
    # relay's WRAPPER_TOKEN). Same env name as the relay for convenience.
    wrapper_token: str = field(default_factory=lambda: _wrapper_value(
        "WRAPPER_TOKEN", "wrapper_token", "change-me-wrapper"))
    # Same opt-in escape hatch as the relay's ALLOW_INSECURE_HTTP: lets
    # RELAY_URL stay ws:// against a non-loopback host instead of requiring
    # wss://. Off by default.
    allow_insecure_http: bool = field(
        default_factory=lambda: _bool("ALLOW_INSECURE_HTTP"))
    # Stable relay routing key. Multiple wrapper hosts may share one relay when
    # each uses a distinct id; "default" preserves the single-machine setup.
    machine_id: str = field(default_factory=lambda: _wrapper_value(
        "CC_REMOTE_MACHINE_ID", "machine_id", "default").strip() or "default")
    # Keep Remote on the same rolling Claude Code install used from the user's
    # shell. An explicit absolute CLAUDE_BIN may override this standard path,
    # but an empty value must never silently select the SDK-bundled executable.
    claude_bin: str = field(default_factory=_claude_bin)
    # Optional proxy inherited only by Codex subprocesses launched by this
    # wrapper.  It deliberately does not mutate the wrapper process or the
    # user's shell/CLI environment.
    codex_proxy: str = field(
        default_factory=lambda: _env("CC_REMOTE_CODEX_PROXY", "").strip())
    # Optional local PTY broker used only by the explicit `claude-remote`
    # experiment. It is intentionally disabled in the supported product path:
    # direct native Claude owners are mirrored read-only and explicitly taken
    # over by the SDK instead of sharing a PTY input state machine.
    claude_broker_socket: str = field(
        default_factory=(
            default_socket_path if sys.platform != "win32" else lambda: ""
        )
    )
    experimental_claude_broker: bool = field(
        default_factory=lambda: _bool(
            "CC_REMOTE_EXPERIMENTAL_CLAUDE_BROKER", False))
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
    codex_history_window_max_bytes: int = field(
        default_factory=lambda: _int(
            "CODEX_HISTORY_WINDOW_MAX_BYTES", 32 * 1024 * 1024)
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
    # Consumer-facing per-turn queue. CodexHandle derives a separate bounded
    # burst window from this value so app-server stdout never waits on relay I/O.
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
    # Code sessions prefer Codex's official shared app-server daemon so the
    # native TUI and Remote can attach to the same thread/control plane. Work
    # intentionally keeps its private stdio app-server for isolation.
    codex_daemon_mode: str = field(
        default_factory=lambda: _env("CC_REMOTE_CODEX_DAEMON", "auto").strip().lower()
    )
    # Max cc subprocesses (resident sessions) the wrapper runs concurrently. Each
    # session = one `claude --resume` child (~190MB RAM). Over the cap → evict an
    # idle one (client keeps its cached history; viewing stays instant). Raised
    # from 4 so browsing many sessions doesn't thrash-respawn. Tune via env.
    max_concurrent_sessions: int = field(default_factory=lambda: _int("MAX_CONCURRENT_SESSIONS", 20))
    state_dir: Path = field(default_factory=lambda: Path(_env("CC_REMOTE_STATE_DIR", str(Path.home() / ".cc-remote"))))
    # Work uses native engine sessions, but gives them private provider-scoped
    # working directories and a cc-remote metadata registry.
    claude_work_root: Path = field(default_factory=lambda: Path(_env(
        "CLAUDE_WORK_ROOT", str(Path.home() / ".claude" / "cc-remote" / "work"))))
    codex_work_root: Path = field(default_factory=lambda: Path(_env(
        "CODEX_WORK_ROOT", str(Path.home() / ".codex" / "cc-remote" / "work"))))


def relay_config() -> RelayConfig:
    return RelayConfig()


_PLACEHOLDER_PREFIXES = ("change-me", "changeme", "replace_with", "replace-with")
_MACHINE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}")


def _placeholder(value: str) -> bool:
    normalized = value.strip().lower()
    return not normalized or normalized.startswith(_PLACEHOLDER_PREFIXES)


def valid_machine_id(value: str) -> bool:
    return bool(_MACHINE_ID_RE.fullmatch(value))


def parse_login_users(raw: str) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Parse the optional username/password/machine policy without logging secrets."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception as exc:
        raise ValueError("LOGIN_USERS_JSON must be valid JSON") from exc
    if not isinstance(data, dict) or not data:
        raise ValueError("LOGIN_USERS_JSON must be a non-empty object")
    if len(data) > 256:
        raise ValueError("LOGIN_USERS_JSON supports at most 256 users")
    users: dict[str, tuple[str, tuple[str, ...]]] = {}
    for username, entry in data.items():
        if (not isinstance(username, str) or not username.strip()
                or username != username.strip() or len(username) > 128
                or any(ord(char) < 32 for char in username)):
            raise ValueError("LOGIN_USERS_JSON contains an invalid username")
        if not isinstance(entry, dict):
            raise ValueError("LOGIN_USERS_JSON user entries must be objects")
        password = entry.get("password")
        machines = entry.get("machines")
        if (not isinstance(password, str) or _placeholder(password)
                or len(password) < 12):
            raise ValueError("LOGIN_USERS_JSON passwords must be non-placeholder and at least 12 characters")
        if (not isinstance(machines, list) or not machines
                or len(machines) > 64):
            raise ValueError("LOGIN_USERS_JSON machines must be a non-empty list of at most 64 ids")
        normalized: list[str] = []
        for machine in machines:
            if not isinstance(machine, str) or (
                    machine != "*" and not valid_machine_id(machine)):
                raise ValueError("LOGIN_USERS_JSON contains an invalid machine id")
            if machine not in normalized:
                normalized.append(machine)
        if "*" in normalized and len(normalized) != 1:
            raise ValueError("LOGIN_USERS_JSON wildcard machine must be used alone")
        users[username] = (password, tuple(normalized))
    return users


def parse_wrapper_tokens(raw: str) -> dict[str, str]:
    """Parse optional machine-bound wrapper credentials."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception as exc:
        raise ValueError("WRAPPER_TOKENS_JSON must be valid JSON") from exc
    if not isinstance(data, dict) or not data:
        raise ValueError("WRAPPER_TOKENS_JSON must be a non-empty object")
    if len(data) > 256:
        raise ValueError("WRAPPER_TOKENS_JSON supports at most 256 machines")
    tokens: dict[str, str] = {}
    for machine, token in data.items():
        if not isinstance(machine, str) or not valid_machine_id(machine):
            raise ValueError("WRAPPER_TOKENS_JSON contains an invalid machine id")
        if (not isinstance(token, str) or _placeholder(token)
                or len(token) < 32):
            raise ValueError("WRAPPER_TOKENS_JSON tokens must be non-placeholder and at least 32 characters")
        tokens[machine] = token
    return tokens


def validate_relay_config(cfg: RelayConfig) -> None:
    """Fail closed for a real relay startup.

    Validation is deliberately separate from ``RelayConfig`` construction so
    unit tests and embedded/local app fixtures can build narrow configs. The
    relay entry point and the no-argument app factory call this before serving.
    """
    errors: list[str] = []
    try:
        parse_login_users(cfg.login_users_json)
    except ValueError as exc:
        errors.append(str(exc))
    if not cfg.login_users_json and (
            _placeholder(cfg.login_password) or len(cfg.login_password) < 12):
        errors.append("LOGIN_PASSWORD must be non-placeholder and at least 12 characters")
    if _placeholder(cfg.session_secret) or len(cfg.session_secret) < 32:
        errors.append("SESSION_SECRET must be non-placeholder and at least 32 characters")
    try:
        parse_wrapper_tokens(cfg.wrapper_tokens_json)
    except ValueError as exc:
        errors.append(str(exc))
    if not cfg.wrapper_tokens_json and (
            _placeholder(cfg.wrapper_token) or len(cfg.wrapper_token) < 32):
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

    push_values = (
        cfg.push_vapid_public_key,
        cfg.push_vapid_private_key,
        cfg.push_vapid_subject,
    )
    if any(push_values) and not all(push_values):
        errors.append(
            "PUSH_VAPID_PUBLIC_KEY, PUSH_VAPID_PRIVATE_KEY and "
            "PUSH_VAPID_SUBJECT must be configured together")
    if cfg.push_vapid_public_key and not re.fullmatch(
            r"[A-Za-z0-9_-]{80,128}", cfg.push_vapid_public_key):
        errors.append("PUSH_VAPID_PUBLIC_KEY has an invalid format")
    if cfg.push_vapid_subject and not (
            cfg.push_vapid_subject.startswith("mailto:")
            or cfg.push_vapid_subject.startswith("https://")):
        errors.append("PUSH_VAPID_SUBJECT must use mailto: or https://")
    if (not cfg.push_db_path or "\x00" in cfg.push_db_path
            or len(cfg.push_db_path.encode("utf-8", errors="surrogatepass")) > 4096):
        errors.append("PUSH_DB_PATH must be a non-empty path of at most 4096 UTF-8 bytes")
    if (not cfg.device_db_path or "\x00" in cfg.device_db_path
            or len(cfg.device_db_path.encode(
                "utf-8", errors="surrogatepass")) > 4096):
        errors.append(
            "DEVICE_DB_PATH must be a non-empty path of at most 4096 UTF-8 bytes")
    if not (60 <= cfg.device_pairing_ttl_seconds <= 3600):
        errors.append("DEVICE_PAIRING_TTL_SECONDS must be between 60 and 3600")

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
            if (
                scheme != "https"
                and canonical_host not in {"127.0.0.1", "::1", "localhost"}
                and not cfg.allow_insecure_http
            ):
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
    if not valid_machine_id(cfg.machine_id):
        errors.append("CC_REMOTE_MACHINE_ID has an invalid format")

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
        if (
            parsed.scheme != "wss"
            and not loopback
            and not cfg.allow_insecure_http
        ):
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
    if cfg.codex_proxy:
        proxy = urlsplit(cfg.codex_proxy)
        if (
            proxy.scheme not in {"http", "https", "socks5", "socks5h"}
            or not proxy.netloc
            or proxy.username is not None
            or proxy.password is not None
            or proxy.query
            or proxy.fragment
            or proxy.path not in {"", "/"}
        ):
            errors.append(
                "CC_REMOTE_CODEX_PROXY must be an http(s) or socks5 URL "
                "without credentials, path, query, or fragment")
    if cfg.experimental_claude_broker:
        if sys.platform == "win32":
            errors.append(
                "CC_REMOTE_EXPERIMENTAL_CLAUDE_BROKER is not supported on "
                "Windows")
        elif (not cfg.claude_broker_socket
              or "\x00" in cfg.claude_broker_socket
              or len(cfg.claude_broker_socket.encode(
                  "utf-8", errors="surrogatepass")) > 4096):
            errors.append(
                "CC_REMOTE_CLAUDE_BROKER_SOCKET must be a non-empty path of at "
                "most 4096 UTF-8 bytes")
        elif not os.path.isabs(os.path.expanduser(cfg.claude_broker_socket)):
            errors.append(
                "CC_REMOTE_CLAUDE_BROKER_SOCKET must be an absolute path")

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
    if not (1024 * 1024 <= cfg.codex_history_window_max_bytes <= 256 * 1024 * 1024):
        errors.append(
            "CODEX_HISTORY_WINDOW_MAX_BYTES must be between 1048576 and 268435456")
    if not (0 < cfg.drain_timeout <= 300):
        errors.append("DRAIN_TIMEOUT must be greater than 0 and at most 300")
    if (cfg.codex_turn_idle_warn_seconds != 0
            and not (5 <= cfg.codex_turn_idle_warn_seconds <= 3600)):
        errors.append(
            "CODEX_TURN_IDLE_WARN_SECONDS must be 0 or between 5 and 3600")
    if cfg.codex_daemon_mode not in {"auto", "off"}:
        errors.append("CC_REMOTE_CODEX_DAEMON must be auto or off")

    if errors:
        raise ValueError("invalid wrapper configuration: " + "; ".join(errors))
