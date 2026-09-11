"""Private Remote controls and one-time native Claude handoff parsing."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import threading
from typing import Any, Callable
from uuid import UUID, uuid4

from claude_agent_sdk import get_session_messages

from cc_remote.wrapper.stream import (
    recover_claude_delayed_retry_tail,
    transcript_path,
)
from cc_remote.wrapper import claude_catalog
from cc_remote.protocol import (
    MAX_AUTO_COMPACT_TOKENS,
    MIN_AUTO_COMPACT_TOKENS,
)


CLAUDE_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
CLAUDE_PERMISSION_MODES = frozenset({
    "default", "acceptEdits", "plan", "auto", "bypassPermissions",
})
CLAUDE_AUTO_COMPACT_MODES = frozenset({"inherit", "auto", "custom"})
CLAUDE_DEFAULT_AUTO_COMPACT_MODE = "inherit"
_V3_FORCED_AUTO_COMPACT_TOKENS = 500_000
_NATIVE_AUTO_COMPACT_POLICY = "native"

_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\[\]/-]{0,254}$")  # local patch: allow provider-native ids, not only claude-*
_MAX_ENTRIES = 4096
_MAX_FILE_BYTES = 1024 * 1024
_MAX_RECORD_BYTES = 16 * 1024 * 1024
_COMPLETED_STOP_REASONS = frozenset({
    "end_turn", "stop_sequence", "max_tokens", "refusal",
})
_SYNTHETIC_NO_RESPONSE_TEXT = "No response requested."


class ClaudeControlStoreError(RuntimeError):
    """The Remote-owned Claude control store is unsafe or malformed."""


@dataclass(frozen=True)
class ClaudeControls:
    model: str | None = None
    effort: str | None = None
    permission_mode: str | None = None
    auto_compact_mode: str = CLAUDE_DEFAULT_AUTO_COMPACT_MODE
    auto_compact_threshold_tokens: int | None = None
    # Version 3 stores the launch value separately from the user's desired
    # value. A lowering transaction may survive a wrapper restart without
    # silently launching under the lower threshold before /compact succeeds.
    # Whether this process has already compacted is deliberately not durable:
    # the transcript may grow while the wrapper is down, so a fresh boundary
    # must be proven before a pending lower threshold is applied.
    applied_auto_compact_mode: str | None = "inherit"
    applied_auto_compact_threshold_tokens: int | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            key: value for key, value in (
                ("model", self.model),
                ("effort", self.effort),
                ("permission_mode", self.permission_mode),
            ) if value is not None
        }
        # Always serialize the mode so desired/applied launch state remains
        # unambiguous across a pending reconnect transaction.
        payload["auto_compact_mode"] = self.auto_compact_mode
        if self.auto_compact_mode == "custom":
            payload["auto_compact_threshold_tokens"] = (
                self.auto_compact_threshold_tokens)
        if self.applied_auto_compact_mode is not None:
            payload["applied_auto_compact_mode"] = (
                self.applied_auto_compact_mode)
            if self.applied_auto_compact_mode == "custom":
                payload["applied_auto_compact_threshold_tokens"] = (
                    self.applied_auto_compact_threshold_tokens)
        return payload


def _stored_auto_compact(
    raw: dict[str, Any],
    *,
    legacy_desired_was_applied: bool = False,
) -> tuple[str, int | None, str, int | None]:
    """Decode one durable desired/applied pair.

    v1/v2 had no separate applied field, so a valid desired value was also the
    value that old cc-remote launched. A missing/malformed v3 applied field has
    no such proof and therefore fails closed to ``inherit``. Missing desired
    state delegates to Claude Code, which is the authority for model- and
    provider-specific defaults.
    """
    raw_mode = raw.get("auto_compact_mode")
    raw_threshold = raw.get("auto_compact_threshold_tokens")
    desired_valid = bool(
        raw_mode in CLAUDE_AUTO_COMPACT_MODES
        and (
            (
                raw_mode == "custom"
                and isinstance(raw_threshold, int)
                and not isinstance(raw_threshold, bool)
                and MIN_AUTO_COMPACT_TOKENS <= raw_threshold
                <= MAX_AUTO_COMPACT_TOKENS
            )
            or (raw_mode != "custom" and raw_threshold is None)
        )
    )
    if desired_valid:
        desired_mode, desired_threshold = valid_claude_auto_compact(
            raw_mode, raw_threshold)
    else:
        desired_mode, desired_threshold = "inherit", None

    raw_applied_mode = raw.get("applied_auto_compact_mode")
    raw_applied_threshold = raw.get(
        "applied_auto_compact_threshold_tokens")
    applied_valid = bool(
        raw_applied_mode in CLAUDE_AUTO_COMPACT_MODES
        and (
            (
                raw_applied_mode == "custom"
                and isinstance(raw_applied_threshold, int)
                and not isinstance(raw_applied_threshold, bool)
                and MIN_AUTO_COMPACT_TOKENS <= raw_applied_threshold
                <= MAX_AUTO_COMPACT_TOKENS
            )
            or (
                raw_applied_mode != "custom"
                and raw_applied_threshold is None
            )
        )
    )
    if applied_valid:
        applied_mode, applied_threshold = valid_claude_auto_compact(
            raw_applied_mode, raw_applied_threshold)
    elif desired_valid and legacy_desired_was_applied:
        applied_mode, applied_threshold = desired_mode, desired_threshold
    else:
        # Neither absent desired state nor a malformed v3 applied field proves
        # a launch flag. Resume under the historical implicit setting so a
        # lower desired threshold cannot skip compact-before-reconnect.
        applied_mode, applied_threshold = "inherit", None
    return (
        desired_mode,
        desired_threshold,
        applied_mode,
        applied_threshold,
    )


def _migrate_v3_forced_auto_compact_default(
    desired_mode: str,
    desired_threshold: int | None,
    applied_mode: str,
    applied_threshold: int | None,
) -> tuple[str, int | None, str, int | None]:
    """Remove the short-lived cc-remote 500K default from v3 records.

    Early version-3 writers silently made 500K the desired value for every
    session. New writers add a backward-compatible top-level policy marker, so
    a later explicit user-selected 500K value is not treated specially.
    """
    if (
        desired_mode == "custom"
        and desired_threshold == _V3_FORCED_AUTO_COMPACT_TOKENS
    ):
        return "inherit", None, "inherit", None
    return (
        desired_mode,
        desired_threshold,
        applied_mode,
        applied_threshold,
    )


def _canonical_session_id(value: object) -> str:
    if not isinstance(value, str):
        raise ClaudeControlStoreError("Claude session id must be a UUID")
    profile_id = None
    native = value
    if "@" in value:
        profile_id, native = value.split("@", 1)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", profile_id):
            raise ClaudeControlStoreError("invalid Claude profile id")
    try:
        canonical = str(UUID(native))
    except (ValueError, AttributeError) as exc:
        raise ClaudeControlStoreError("Claude session id must be a UUID") from exc
    if native.lower() != canonical:
        raise ClaudeControlStoreError("Claude session id must be canonical")
    return f"{profile_id}@{canonical}" if profile_id is not None else canonical


def valid_claude_model(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _MODEL_ID.fullmatch(value) else None


def valid_claude_effort(value: object) -> str | None:
    return value if isinstance(value, str) and value in CLAUDE_EFFORTS else None


def valid_claude_permission(value: object) -> str | None:
    return (
        value if isinstance(value, str) and value in CLAUDE_PERMISSION_MODES
        else None
    )


def valid_claude_auto_compact(
    mode: object,
    threshold_tokens: object = None,
) -> tuple[str, int | None]:
    """Return one canonical safe config; malformed persisted state inherits."""
    if mode is None:
        mode = "inherit"
    if not isinstance(mode, str) or mode not in CLAUDE_AUTO_COMPACT_MODES:
        return "inherit", None
    if mode == "custom":
        if (not isinstance(threshold_tokens, int)
                or isinstance(threshold_tokens, bool)
                or not MIN_AUTO_COMPACT_TOKENS <= threshold_tokens
                <= MAX_AUTO_COMPACT_TOKENS):
            return "inherit", None
        return mode, threshold_tokens
    if threshold_tokens is not None:
        return "inherit", None
    return mode, None


def claude_auto_compact_cli_value(
    mode: object,
    threshold_tokens: object = None,
) -> str | None:
    """Map the canonical session config onto Claude's ``--autocompact`` value."""
    checked_mode, checked_threshold = valid_claude_auto_compact(
        mode, threshold_tokens)
    if checked_mode == "inherit":
        return None
    if checked_mode == "auto":
        return "auto"
    assert checked_threshold is not None
    return str(checked_threshold)


def claude_auto_compact_from_cli(value: object) -> tuple[str, int | None]:
    """Parse only canonical broker launch values, never user settings text."""
    if value is None:
        return "inherit", None
    if value == "auto":
        return "auto", None
    # Broker metadata is a local trust boundary nevertheless: bound the string
    # before ``int`` so a corrupt/compromised response cannot trigger Python's
    # huge-integer conversion path while Remote is adopting a terminal session.
    if (isinstance(value, str)
            and 6 <= len(value) <= 7
            and value.isascii()
            and value.isdecimal()):
        return valid_claude_auto_compact("custom", int(value))
    return "inherit", None


class ClaudeControlStore:
    """Atomic, bounded per-session preferences owned only by cc-remote."""

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "claude-session-controls.json"
        self._lock = threading.RLock()
        self._profile_revision = 0
        self._sessions = self._load()

    def migrate_profile_sessions(
        self,
        transform: Callable[[str], str],
        *,
        profile_revision: int,
    ) -> int:
        """Atomically translate keys once per Claude topology revision."""
        if (
            isinstance(profile_revision, bool)
            or not isinstance(profile_revision, int)
            or profile_revision < 1
        ):
            raise ClaudeControlStoreError("invalid Claude profile revision")
        with self._lock:
            if self._profile_revision >= profile_revision:
                return 0
            updated: dict[str, dict[str, Any]] = {}
            migrated = 0
            for session_id, controls in self._sessions.items():
                target = _canonical_session_id(transform(session_id))
                if target in updated and updated[target] != controls:
                    raise ClaudeControlStoreError(
                        "Claude control profile migration collides")
                updated[target] = controls
                migrated += target != session_id
            self._persist(updated, profile_revision=profile_revision)
            self._sessions = updated
            self._profile_revision = profile_revision
            return migrated

    def get(self, session_id: str) -> ClaudeControls:
        session_id = _canonical_session_id(session_id)
        with self._lock:
            stored = self._sessions.get(session_id)
            raw = dict(stored) if stored is not None else None
        if raw is None:
            return ClaudeControls()
        auto_mode, auto_threshold, applied_mode, applied_threshold = (
            _stored_auto_compact(raw)
        )
        return ClaudeControls(
            model=valid_claude_model(raw.get("model")),
            effort=valid_claude_effort(raw.get("effort")),
            permission_mode=valid_claude_permission(raw.get("permission_mode")),
            auto_compact_mode=auto_mode,
            auto_compact_threshold_tokens=auto_threshold,
            applied_auto_compact_mode=applied_mode,
            applied_auto_compact_threshold_tokens=applied_threshold,
        )

    def update(
        self,
        session_id: str,
        *,
        model: str | None,
        effort: str | None,
        permission_mode: str | None,
        auto_compact_mode: str = CLAUDE_DEFAULT_AUTO_COMPACT_MODE,
        auto_compact_threshold_tokens: int | None = None,
        applied_auto_compact_mode: str | None = None,
        applied_auto_compact_threshold_tokens: int | None = None,
    ) -> ClaudeControls:
        session_id = _canonical_session_id(session_id)
        checked_auto_mode, checked_auto_threshold = valid_claude_auto_compact(
            auto_compact_mode, auto_compact_threshold_tokens)
        if applied_auto_compact_mode is None:
            applied_mode, applied_threshold = (
                checked_auto_mode, checked_auto_threshold)
        else:
            applied_mode, applied_threshold = valid_claude_auto_compact(
                applied_auto_compact_mode,
                applied_auto_compact_threshold_tokens,
            )
        controls = ClaudeControls(
            model=valid_claude_model(model),
            effort=valid_claude_effort(effort),
            permission_mode=valid_claude_permission(permission_mode),
            auto_compact_mode=checked_auto_mode,
            auto_compact_threshold_tokens=checked_auto_threshold,
            applied_auto_compact_mode=applied_mode,
            applied_auto_compact_threshold_tokens=applied_threshold,
        )
        payload = controls.as_dict()
        with self._lock:
            updated = dict(self._sessions)
            if payload:
                # Move refreshed sessions to the insertion-order tail so the
                # bounded eviction policy behaves like a small LRU cache.
                updated.pop(session_id, None)
                updated[session_id] = payload
            else:
                updated.pop(session_id, None)
            while len(updated) > _MAX_ENTRIES:
                updated.pop(next(iter(updated)))
            self._persist(updated)
            self._sessions = updated
        return controls

    def update_auto_compact(
        self,
        session_id: str,
        *,
        mode: str,
        threshold_tokens: int | None,
        applied_mode: str | None = None,
        applied_threshold_tokens: int | None = None,
        preserve_other_controls: bool = True,
    ) -> ClaudeControls:
        """Atomically replace autocompact, optionally clearing Code controls.

        Work sessions share the Claude engine but do not expose Code's
        model/effort/permission preferences.  Their caller passes ``False`` so
        a legacy record for the same native UUID cannot leak those controls
        back into a later Code resume.
        """
        session_id = _canonical_session_id(session_id)
        checked_mode, checked_threshold = valid_claude_auto_compact(
            mode, threshold_tokens)
        if applied_mode is None:
            checked_applied_mode, checked_applied_threshold = (
                checked_mode, checked_threshold)
        else:
            checked_applied_mode, checked_applied_threshold = (
                valid_claude_auto_compact(
                    applied_mode, applied_threshold_tokens)
            )
        with self._lock:
            raw = (
                dict(self._sessions.get(session_id, {}))
                if preserve_other_controls else {}
            )
            controls = ClaudeControls(
                model=valid_claude_model(raw.get("model")),
                effort=valid_claude_effort(raw.get("effort")),
                permission_mode=valid_claude_permission(
                    raw.get("permission_mode")),
                auto_compact_mode=checked_mode,
                auto_compact_threshold_tokens=checked_threshold,
                applied_auto_compact_mode=checked_applied_mode,
                applied_auto_compact_threshold_tokens=(
                    checked_applied_threshold),
            )
            payload = controls.as_dict()
            updated = dict(self._sessions)
            updated.pop(session_id, None)
            if payload:
                updated[session_id] = payload
            while len(updated) > _MAX_ENTRIES:
                updated.pop(next(iter(updated)))
            self._persist(updated)
            self._sessions = updated
        return controls

    def inherit_if_absent(
        self,
        session_id: str,
        *,
        model: str | None,
        effort: str | None,
        permission_mode: str | None,
        auto_compact_mode: str = CLAUDE_DEFAULT_AUTO_COMPACT_MODE,
        auto_compact_threshold_tokens: int | None = None,
        applied_auto_compact_mode: str | None = None,
        applied_auto_compact_threshold_tokens: int | None = None,
    ) -> ClaudeControls:
        """Seed a new fork once without overwriting later child choices."""
        session_id = _canonical_session_id(session_id)
        checked_auto_mode, checked_auto_threshold = valid_claude_auto_compact(
            auto_compact_mode, auto_compact_threshold_tokens)
        if applied_auto_compact_mode is None:
            applied_mode, applied_threshold = (
                checked_auto_mode, checked_auto_threshold)
        else:
            applied_mode, applied_threshold = valid_claude_auto_compact(
                applied_auto_compact_mode,
                applied_auto_compact_threshold_tokens,
            )
        controls = ClaudeControls(
            model=valid_claude_model(model),
            effort=valid_claude_effort(effort),
            permission_mode=valid_claude_permission(permission_mode),
            auto_compact_mode=checked_auto_mode,
            auto_compact_threshold_tokens=checked_auto_threshold,
            applied_auto_compact_mode=applied_mode,
            applied_auto_compact_threshold_tokens=applied_threshold,
        )
        payload = controls.as_dict()
        with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                (existing_auto_mode, existing_auto_threshold,
                 existing_applied_mode, existing_applied_threshold) = (
                    _stored_auto_compact(existing)
                )
                return ClaudeControls(
                    model=valid_claude_model(existing.get("model")),
                    effort=valid_claude_effort(existing.get("effort")),
                    permission_mode=valid_claude_permission(
                        existing.get("permission_mode")),
                    auto_compact_mode=existing_auto_mode,
                    auto_compact_threshold_tokens=existing_auto_threshold,
                    applied_auto_compact_mode=existing_applied_mode,
                    applied_auto_compact_threshold_tokens=(
                        existing_applied_threshold),
                )
            if not payload:
                return controls
            updated = dict(self._sessions)
            updated[session_id] = payload
            while len(updated) > _MAX_ENTRIES:
                updated.pop(next(iter(updated)))
            self._persist(updated)
            self._sessions = updated
        return controls

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return {}
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size > _MAX_FILE_BYTES):
            raise ClaudeControlStoreError(
                "Claude control store is not a private bounded file")
        try:
            raw_bytes = self.path.read_bytes()
            if len(raw_bytes) > _MAX_FILE_BYTES:
                raise ValueError("control store exceeds size limit")
            raw = json.loads(raw_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ClaudeControlStoreError("Claude control store is unreadable") from exc
        sessions = raw.get("sessions") if isinstance(raw, dict) else None
        version = raw.get("version") if isinstance(raw, dict) else None
        if (not isinstance(raw, dict)
                or not isinstance(version, int)
                or isinstance(version, bool)
                or version not in {1, 2, 3}
                or not isinstance(sessions, dict)
                or len(sessions) > _MAX_ENTRIES):
            raise ClaudeControlStoreError("Claude control store has invalid shape")
        profile_revision = raw.get("profile_revision", 0)
        if (
            isinstance(profile_revision, bool)
            or not isinstance(profile_revision, int)
            or profile_revision < 0
        ):
            raise ClaudeControlStoreError(
                "Claude control profile revision is invalid")
        self._profile_revision = profile_revision
        loaded: dict[str, dict[str, Any]] = {}
        for raw_id, values in sessions.items():
            if not isinstance(values, dict):
                continue
            try:
                session_id = _canonical_session_id(raw_id)
            except ClaudeControlStoreError:
                continue
            (auto_mode, auto_threshold,
             applied_mode, applied_threshold) = _stored_auto_compact(
                values,
                legacy_desired_was_applied=version in {1, 2},
            )
            if (
                version == 3
                and raw.get("auto_compact_policy")
                    != _NATIVE_AUTO_COMPACT_POLICY
            ):
                (auto_mode, auto_threshold,
                 applied_mode, applied_threshold) = (
                    _migrate_v3_forced_auto_compact_default(
                        auto_mode,
                        auto_threshold,
                        applied_mode,
                        applied_threshold,
                    )
                )
            controls = ClaudeControls(
                model=valid_claude_model(values.get("model")),
                effort=valid_claude_effort(values.get("effort")),
                permission_mode=valid_claude_permission(
                    values.get("permission_mode")),
                auto_compact_mode=auto_mode,
                auto_compact_threshold_tokens=auto_threshold,
                applied_auto_compact_mode=applied_mode,
                applied_auto_compact_threshold_tokens=applied_threshold,
            ).as_dict()
            if controls:
                loaded[session_id] = controls
        return loaded

    def _persist(
        self,
        sessions: dict[str, dict[str, Any]],
        *,
        profile_revision: int | None = None,
    ) -> None:
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(parent, 0o700)
        payload = json.dumps(
            {
                # Keep the format readable by the immediately preceding
                # release so an immutable deployment rollback still starts.
                # That release ignores this additive marker.
                "version": 3,
                "auto_compact_policy": _NATIVE_AUTO_COMPACT_POLICY,
                "profile_revision": (
                    self._profile_revision
                    if profile_revision is None else profile_revision
                ),
                "sessions": sessions,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(payload) > _MAX_FILE_BYTES:
            raise ClaudeControlStoreError("Claude control store exceeds size limit")
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid4().hex}.tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception as exc:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise ClaudeControlStoreError(
                "Claude control store could not be persisted") from exc


def _is_synthetic_no_response(message: dict[str, Any]) -> bool:
    if message.get("model") != "<synthetic>":
        return False
    content = message.get("content")
    return (
        isinstance(content, list)
        and len(content) == 1
        and isinstance(content[0], dict)
        and content[0].get("type") == "text"
        and isinstance(content[0].get("text"), str)
        and content[0]["text"].strip() == _SYNTHETIC_NO_RESPONSE_TEXT
    )


def _bounded_jsonl(path: str, max_bytes: int):
    info = os.stat(path, follow_symlinks=False)
    if (not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes):
        return
    with open(path, "rb") as stream:
        while True:
            line = stream.readline(_MAX_RECORD_BYTES + 1)
            if not line:
                return
            if len(line) > _MAX_RECORD_BYTES and not line.endswith(b"\n"):
                while line and not line.endswith(b"\n"):
                    line = stream.readline(_MAX_RECORD_BYTES + 1)
                continue
            try:
                row = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                continue
            if isinstance(row, dict):
                yield row


def last_completed_assistant_controls(
    session_id: str,
    *,
    directory: str,
    max_bytes: int,
    index_store=None,
    config_dir: str | os.PathLike[str] | None = None,
) -> ClaudeControls:
    """Read controls from the latest completed native assistant turn.

    A menu-only /model or /effort change creates no completed assistant row and
    is intentionally invisible. The SDK's SessionMessage identifies the active
    conversation chain; the matching raw JSONL row supplies top-level effort,
    which SessionMessage currently drops.
    """
    _canonical_session_id(session_id)
    path = (
        str(found) if config_dir is not None and (
            found := claude_catalog.find_session_file(
                config_dir, session_id, directory=directory)
        ) is not None else None
    ) if config_dir is not None else transcript_path(session_id)
    if path is None:
        return ClaudeControls()
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return ClaudeControls()
    if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
        return ClaudeControls()

    messages = recover_claude_delayed_retry_tail(
        session_id,
        (
            claude_catalog.get_session_messages(
                config_dir, session_id, directory=directory)
            if config_dir is not None else
            get_session_messages(session_id, directory=directory)
        ),
        path=path,
        index_store=index_store,
        snapshot_size=info.st_size,
        max_record_bytes=max_bytes,
    )
    turns: list[list[Any]] = []
    for item in messages:
        if getattr(item, "type", None) == "user" or not turns:
            turns.append([])
        if getattr(item, "type", None) == "assistant":
            turns[-1].append(item)

    selected: list[Any] | None = None
    selected_model: str | None = None
    for turn in reversed(turns):
        for item in reversed(turn):
            message = getattr(item, "message", None)
            if not isinstance(message, dict) or _is_synthetic_no_response(message):
                continue
            if message.get("stop_reason") not in _COMPLETED_STOP_REASONS:
                continue
            selected = turn
            selected_model = valid_claude_model(message.get("model"))
            break
        if selected is not None:
            break
    if selected is None:
        return ClaudeControls()

    assistant_ids = {
        getattr(item, "uuid", None) for item in selected
        if isinstance(getattr(item, "uuid", None), str)
    }
    efforts: dict[str, str] = {}
    try:
        for row in _bounded_jsonl(path, max_bytes):
            row_id = row.get("uuid")
            if row_id not in assistant_ids or row.get("type") != "assistant":
                continue
            effort = valid_claude_effort(row.get("effort"))
            if effort is not None:
                efforts[row_id] = effort
    except OSError:
        return ClaudeControls(model=selected_model)

    selected_effort = next(
        (efforts[item.uuid] for item in reversed(selected)
         if getattr(item, "uuid", None) in efforts),
        None,
    )
    return ClaudeControls(model=selected_model, effort=selected_effort)
