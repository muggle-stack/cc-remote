"""Codex session metadata and rollout helpers.

The app-server state DB is authoritative for sidebar metadata such as names and
archive state. Rollout files remain the source for history, cwd fallback, and
per-turn settings.
"""
from __future__ import annotations

import glob
from importlib import import_module
import json
import math
import os
import re
from typing import Any, Optional

from cc_remote.log import logger
from cc_remote.wrapper.codex_rpc import codex_rpc


def _load_tomllib():
    """Load TOML support on every advertised Python version."""
    try:
        return import_module("tomllib")
    except ModuleNotFoundError:  # Python 3.10 has no stdlib tomllib.
        return import_module("tomli")


tomllib = _load_tomllib()

log = logger("cc_remote.wrapper.codex_sessions")

_CONFIG = os.path.expanduser("~/.codex/config.toml")
_CONFIG_MAX_BYTES = 4 * 1024 * 1024

_ROOT = os.path.expanduser("~/.codex/sessions")
_ARCHIVE_ROOT = os.path.expanduser("~/.codex/archived_sessions")
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
MAX_JSONL_RECORD_BYTES = 16 * 1024 * 1024
MAX_META_RECORD_BYTES = 1024 * 1024
_LIST_PAGE_SIZE = 100
_LIST_MAX_PER_ARCHIVE_STATE = 200
_LIST_MAX_PAGES = 20
_THREAD_STATUSES = frozenset({"notLoaded", "idle", "systemError", "active"})


async def list_codex_sessions(limit: int = 60) -> list[dict[str, Any]]:
    """List active and archived app-server threads, newest first.

    ``limit`` is applied independently to active and archived threads so a busy
    active list cannot make the archived group disappear. Both result sets are
    bounded and paginated with opaque app-server cursors.
    """
    per_state_limit = max(1, min(limit, _LIST_MAX_PER_ARCHIVE_STATE))
    provider = codex_current_provider().strip()
    by_id: dict[str, dict[str, Any]] = {}

    for archived in (False, True):
        cursor: Optional[str] = None
        seen_cursors: set[str] = set()
        received = 0
        for _ in range(_LIST_MAX_PAGES):
            remaining = per_state_limit - received
            if remaining <= 0:
                break
            params: dict[str, Any] = {
                "limit": min(_LIST_PAGE_SIZE, remaining),
                "sortKey": "updated_at",
                "sortDirection": "desc",
                "archived": archived,
            }
            if cursor:
                params["cursor"] = cursor
            if provider:
                params["modelProviders"] = [provider]

            response = await codex_rpc("thread/list", params)
            if not isinstance(response, dict) or not isinstance(response.get("data"), list):
                raise RuntimeError("codex thread/list returned an invalid response")
            page = response["data"][:remaining]
            received += len(page)
            for thread in page:
                normalized = _normalize_thread(thread, archived=archived)
                if normalized is not None:
                    by_id[normalized["session_id"]] = normalized

            next_cursor = response.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            if next_cursor in seen_cursors:
                raise RuntimeError("codex thread/list repeated its pagination cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    return sorted(
        by_id.values(), key=lambda item: _updated_sort_key(item.get("last_modified")),
        reverse=True,
    )


def _normalize_thread(thread: Any, *, archived: bool) -> Optional[dict[str, Any]]:
    if not isinstance(thread, dict):
        return None
    session_id = thread.get("id")
    if not isinstance(session_id, str) or not _SAFE_SESSION_ID.fullmatch(session_id):
        return None

    git_info = thread.get("gitInfo")
    branch = git_info.get("branch") if isinstance(git_info, dict) else None
    forked_from = thread.get("forkedFromId")
    if not isinstance(forked_from, str) or not _SAFE_SESSION_ID.fullmatch(forked_from):
        forked_from = None
    raw_status = thread.get("status")
    status = raw_status.get("type") if isinstance(raw_status, dict) else None
    if status not in _THREAD_STATUSES:
        status = None

    updated_at = thread.get("updatedAt")
    if (isinstance(updated_at, bool) or not isinstance(updated_at, (int, float))
            or not math.isfinite(updated_at) or updated_at < 0):
        last_modified = None
    else:
        last_modified = str(updated_at)

    return {
        "session_id": session_id,
        "summary": _bounded_text(thread.get("name"), 500),
        "first_prompt": _bounded_text(thread.get("preview"), 2000),
        "cwd": _bounded_text(thread.get("cwd"), 4096),
        "last_modified": last_modified,
        "git_branch": _bounded_text(branch, 500),
        "forked_from_id": forked_from,
        "status": status,
        "tag": "archived" if archived else None,
    }


def _bounded_text(value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    return value[:limit] or None


def _updated_sort_key(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return -1.0
    return parsed if math.isfinite(parsed) else -1.0


def codex_session_cwd(session_id: str) -> Optional[str]:
    """The cwd a Codex thread was started in (for resume). None if not found."""
    path = _rollout_path(session_id)
    if not path:
        return None
    meta = _read_meta(path)
    return meta.get("cwd") if meta else None


def codex_rollout_path(session_id: str) -> Optional[str]:
    """Public: the rollout .jsonl for a Codex thread (for history replay)."""
    return _rollout_path(session_id)


def codex_model(default: str = "gpt-5-codex") -> str:
    """The model Codex is configured to use (from ~/.codex/config.toml). Used to
    show the right model readout for live Codex sessions (not a Claude model)."""
    return _config_value("model", default)[:256]


def codex_effort(default: str = "high") -> str:
    """The default reasoning effort from ~/.codex/config.toml (model_reasoning_effort)."""
    return _config_value("model_reasoning_effort", default)[:64]


def codex_current_provider() -> str:
    """The provider Codex is configured for right now (config.toml model_provider).
    A codex rollout carries provider-encrypted reasoning, so a session from a
    DIFFERENT provider can't be resumed here — the list is filtered to this one."""
    return _config_value("model_provider", "")[:256]


def codex_context_window(default: int = 256000) -> int:
    """Fallback context window (tokens) for a fresh session before any turn has
    reported one. The AUTHORITATIVE value comes from the live server's
    thread/tokenUsage/updated (tokenUsage.modelContextWindow) and overrides this;
    ~/.codex/config.toml's model_context_window is only a user-declared estimate
    (it can disagree with the server, e.g. 400000 in config vs 258400 live)."""
    try:
        return int(_config_value("model_context_window", str(default)))
    except (ValueError, TypeError):
        return default


def codex_fast_enabled() -> bool:
    """True for either accepted/reported top-level Codex Fast tier name."""
    return (_config_value("service_tier", "") or "").lower() in {
        "fast", "priority",
    }


def codex_approval(default: str = "never") -> str:
    """The top-level Codex approval policy used for a new thread."""
    value = _config_value("approval_policy", default)
    return value if value in {"untrusted", "on-request", "never"} else default


def codex_session_settings(
    session_id: str, max_bytes: int = 64 * 1024 * 1024,
) -> dict:
    """The per-thread settings carried by the latest bounded rollout tail.

    Codex appends a `turn_context` record per turn carrying `model`, `effort`,
    and the nested `collaboration_mode` selected for that turn.
    The official thread/resume response is authoritative for settings it exposes;
    this bounded tail is the fallback and remains necessary for collaboration mode,
    which 0.144.1 does not include in that response. Config.toml is never a valid
    resume source because it holds only fresh-thread global defaults.

    Returns {} when the rollout is missing/unreadable; the caller falls back to the
    config defaults (correct for a brand-new session).
    """
    path = _rollout_path(session_id)
    if not path:
        return {}
    try:
        size = os.path.getsize(path)
    except OSError:
        return {}
    out: dict = {}
    try:
        # A long-running thread can easily exceed 64 MiB. Only its newest
        # turn_context matters, so seek to a bounded tail and discard the first
        # partial JSONL record instead of rejecting the entire rollout.
        tail_bytes = max(1, int(max_bytes))
        start = max(0, size - tail_bytes)
        with open(path, "rb") as f:
            if start:
                f.seek(start - 1)
                starts_at_record = f.read(1) == b"\n"
                f.seek(start)
                if not starts_at_record:
                    discarded = f.readline(MAX_JSONL_RECORD_BYTES + 1)
                    if not discarded.endswith(b"\n"):
                        return {}
            while True:
                raw = f.readline(MAX_JSONL_RECORD_BYTES + 1)
                if not raw:
                    break
                if len(raw) > MAX_JSONL_RECORD_BYTES:
                    if not raw.endswith(b"\n"):
                        # The remainder is still the same oversized record. Stop:
                        # a boundary cannot be recovered without exceeding our cap.
                        break
                    continue
                try:
                    line = raw.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                # cheap prefilter: most lines are messages, not turn contexts
                if '"turn_context"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("type") != "turn_context":
                    continue
                payload = rec.get("payload")
                if not isinstance(payload, dict):
                    continue
                for key in ("model", "effort"):
                    val = payload.get(key)
                    if isinstance(val, str) and val:
                        out[key] = val[:256 if key == "model" else 64]
                        # last one wins = the session's current setting
                approval = payload.get("approval_policy")
                if approval in {"untrusted", "on-request", "never"}:
                    out["approval_policy"] = approval
                if "service_tier" in payload:
                    tier = payload.get("service_tier")
                    if tier is None:
                        out["service_tier"] = None
                    elif isinstance(tier, str) and tier:
                        out["service_tier"] = tier[:64]
                collaboration = payload.get("collaboration_mode")
                if isinstance(collaboration, dict):
                    mode = collaboration.get("mode")
                    if mode in ("default", "plan"):
                        out["collaboration_mode"] = mode
    except Exception as e:
        log.warning("read codex session settings failed", session_id=session_id, error=str(e))
    return out


def _config_value(key: str, default: str) -> str:
    try:
        target = os.path.realpath(_CONFIG)
        if os.path.getsize(target) > _CONFIG_MAX_BYTES:
            return default
        with open(target, "rb") as f:
            config = tomllib.load(f)
        # tomllib preserves table boundaries. Looking only at the root prevents
        # a profile/provider's nested `model`, effort or service tier from being
        # mistaken for the user's default.
        value = config.get(key)
        if isinstance(value, str):
            return value[:4096] or default
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)[:4096]
    except Exception:
        pass
    return default


# ---- internals ----
def _rollout_path(session_id: str) -> Optional[str]:
    try:
        if not _SAFE_SESSION_ID.fullmatch(session_id):
            return None
        safe_id = glob.escape(session_id)
        scanned = 0
        # thread/archive moves the rollout out of ``sessions``. Archived rows
        # still need history, cwd lookup, and engine detection so unarchive never
        # falls through to the Claude SDK.
        for source_root in (_ROOT, _ARCHIVE_ROOT):
            matches = glob.iglob(
                os.path.join(source_root, "**", f"*{safe_id}*.jsonl"),
                recursive=True,
            )
            root = os.path.realpath(source_root)
            for match in matches:
                if scanned >= 1000:
                    return None
                scanned += 1
                resolved = os.path.realpath(match)
                if os.path.commonpath((root, resolved)) == root:
                    return match
        return None
    except Exception:
        return None


def _read_meta(path: str) -> Optional[dict]:
    try:
        with open(path) as f:
            line = f.readline(MAX_META_RECORD_BYTES + 1)
            if len(line.encode("utf-8", "surrogatepass")) > MAX_META_RECORD_BYTES:
                return None
            d = json.loads(line)
        if d.get("type") == "session_meta" and isinstance(d.get("payload"), dict):
            return d["payload"]
    except Exception:
        pass
    return None


def _bounded_lines(file, max_record_bytes: int):
    """Yield complete JSONL records without ever allocating one unbounded line."""
    while True:
        line = file.readline(max_record_bytes + 1)
        if not line:
            return
        if len(line.encode("utf-8", "surrogatepass")) <= max_record_bytes \
                and (line.endswith("\n") or len(line) < max_record_bytes + 1):
            yield line
            continue
        # Oversized record: consume bounded chunks through its newline and skip it.
        while line and not line.endswith("\n"):
            line = file.readline(max_record_bytes + 1)
