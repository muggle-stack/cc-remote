"""Validated local Claude account/profile registry.

Each profile selects a complete native account boundary. The per-user
``~/.claude`` root retains Claude's unset-``CLAUDE_CONFIG_DIR`` layout;
other roots use an explicit ``CLAUDE_CONFIG_DIR``. Local paths stay
private; the browser receives only stable ids and labels.  A single profile
keeps native Claude session ids for compatibility, while multiple profiles use
``<profile>@<native-id>`` routing so identical UUIDs cannot cross accounts.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Iterator


_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
# Claude currently emits UUIDs, but the historical wrapper contract accepted
# any bounded wire-safe native id (including broker/test adapters).  Preserve
# that single-account API while reserving ``@`` solely for profile routing.
_NATIVE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_WIRE_ID_CHARS = 128
_MAX_PROFILES = 32
_MAX_LABEL_CHARS = 48
_MAX_PATH_BYTES = 4096
_TOPOLOGY_VERSION = 1
_TRANSITION_VERSION = 1
_TOPOLOGY_MAX_BYTES = 64 * 1024


@dataclass(frozen=True)
class ClaudeProfile:
    id: str
    label: str
    config_dir: Path
    is_default: bool = False

    def public(self) -> dict[str, str]:
        return {"id": self.id, "label": self.label}


@dataclass(frozen=True)
class ClaudeProfileTopologyTransition:
    revision: int
    previous: tuple[tuple[str, str], ...]
    previous_default_id: str | None
    current: tuple[tuple[str, str], ...]
    current_default_id: str
    remaps: dict[str, str]

    @property
    def previous_is_multi(self) -> bool:
        return len(self.previous) > 1

    @property
    def current_is_multi(self) -> bool:
        return len(self.current) > 1

    @property
    def legacy_profile_id(self) -> str:
        if len(self.previous) == 1:
            old_id = self.previous[0][0]
            return self.remaps.get(old_id, old_id)
        if self.previous_default_id is not None:
            return self.remaps.get(
                self.previous_default_id, self.previous_default_id)
        return self.current_default_id

    def wire_session_id(self, session_id: str) -> str:
        """Translate a persisted Claude route into the target topology."""
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("invalid persisted Claude session id")
        if "@" in session_id:
            old_profile_id, native_id = session_id.split("@", 1)
            profile_id = self.remaps.get(old_profile_id, old_profile_id)
        else:
            profile_id = self.legacy_profile_id
            native_id = session_id
        if not _NATIVE_SESSION_ID.fullmatch(native_id):
            raise ValueError("invalid persisted Claude native session id")
        if self.current_is_multi or profile_id != self.current_default_id:
            routed = f"{profile_id}@{native_id}"
            if len(routed) > _MAX_WIRE_ID_CHARS:
                raise ValueError("persisted Claude session id is too long")
            return routed
        return native_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": _TRANSITION_VERSION,
            "revision": self.revision,
            "previous": [
                {"id": profile_id, "config_dir": config_dir}
                for profile_id, config_dir in self.previous
            ],
            "previous_default_id": self.previous_default_id,
            "current": [
                {"id": profile_id, "config_dir": config_dir}
                for profile_id, config_dir in self.current
            ],
            "current_default_id": self.current_default_id,
        }


@dataclass(frozen=True)
class _StoredTopology:
    revision: int
    profiles: tuple[tuple[str, str], ...]
    default_id: str


class ClaudeProfileRegistry:
    """Ordered, immutable Claude account registry with wire translation."""

    def __init__(self, profiles: tuple[ClaudeProfile, ...]) -> None:
        if not profiles:
            raise ValueError("Claude profiles must not be empty")
        self._profiles = profiles
        self._by_id = {profile.id: profile for profile in profiles}
        defaults = [profile for profile in profiles if profile.is_default]
        if len(defaults) != 1:
            raise ValueError("Claude profiles must contain exactly one default")
        self.default = defaults[0]

    @classmethod
    def from_json(
        cls,
        raw: str,
        *,
        default_config_dir: str | os.PathLike[str] | None = None,
    ) -> "ClaudeProfileRegistry":
        if not isinstance(raw, str):
            raise ValueError(
                "CC_REMOTE_CLAUDE_PROFILES_JSON must be a JSON object")
        if not raw.strip():
            fallback = default_config_dir
            if fallback is None:
                fallback = (
                    os.environ.get("CLAUDE_CONFIG_DIR")
                    or Path.home() / ".claude"
                )
            return cls((ClaudeProfile(
                id="primary",
                label="默认账号",
                config_dir=cls._effective_config_dir(fallback),
                is_default=True,
            ),))
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "CC_REMOTE_CLAUDE_PROFILES_JSON must be valid JSON") from exc
        if not isinstance(payload, dict) or not payload:
            raise ValueError(
                "CC_REMOTE_CLAUDE_PROFILES_JSON must be a non-empty object")
        if len(payload) > _MAX_PROFILES:
            raise ValueError(
                "CC_REMOTE_CLAUDE_PROFILES_JSON supports at most "
                f"{_MAX_PROFILES} profiles")

        profiles: list[ClaudeProfile] = []
        config_dirs: set[Path] = set()
        for profile_id, entry in payload.items():
            if (
                not isinstance(profile_id, str)
                or not _PROFILE_ID.fullmatch(profile_id)
            ):
                raise ValueError(
                    "CC_REMOTE_CLAUDE_PROFILES_JSON contains an invalid "
                    "profile id")
            if not isinstance(entry, dict):
                raise ValueError("Claude profile entries must be objects")
            if set(entry) - {"label", "config_dir", "default"}:
                raise ValueError("Claude profile entry contains unknown fields")
            label = entry.get("label")
            if (
                not isinstance(label, str)
                or not label.strip()
                or label != label.strip()
                or len(label) > _MAX_LABEL_CHARS
                or any(ord(char) < 32 for char in label)
            ):
                raise ValueError(
                    "Claude profile labels must be printable and non-empty")
            config_dir = cls._config_dir(entry.get("config_dir"))
            if config_dir in config_dirs:
                raise ValueError(
                    "Claude profile config dirs must be unique after realpath "
                    "resolution")
            config_dirs.add(config_dir)
            is_default = entry.get("default", False)
            if not isinstance(is_default, bool):
                raise ValueError("Claude profile default must be a boolean")
            profiles.append(ClaudeProfile(
                id=profile_id,
                label=label,
                config_dir=config_dir,
                is_default=is_default,
            ))
        return cls(tuple(profiles))

    @staticmethod
    def _config_dir(value: Any) -> Path:
        if not isinstance(value, (str, os.PathLike)):
            raise ValueError(
                "Claude profile config_dir must be an absolute path")
        raw = os.fspath(value)
        if (
            not raw
            or "\x00" in raw
            or len(raw.encode("utf-8", errors="surrogatepass"))
                > _MAX_PATH_BYTES
        ):
            raise ValueError(
                "Claude profile config_dir must be a bounded absolute path")
        expanded = Path(raw).expanduser()
        if not expanded.is_absolute():
            raise ValueError(
                "Claude profile config_dir must be an absolute path")
        return expanded.resolve(strict=False)

    @staticmethod
    def _effective_config_dir(value: Any) -> Path:
        """Resolve an inherited native value without shell-style expansion.

        The pinned SDK deliberately treats ``~`` in CLAUDE_CONFIG_DIR as a
        literal relative component. Preserve that legacy behavior while
        storing one absolute identity for topology comparisons.
        """
        if not isinstance(value, (str, os.PathLike)):
            raise ValueError("invalid effective CLAUDE_CONFIG_DIR")
        raw = os.fspath(value)
        if (
            not raw
            or "\x00" in raw
            or len(raw.encode("utf-8", errors="surrogatepass"))
                > _MAX_PATH_BYTES
        ):
            raise ValueError("invalid effective CLAUDE_CONFIG_DIR")
        path = Path(raw)
        if not path.is_absolute():
            path = Path.cwd() / path
        return path.resolve(strict=False)

    def __iter__(self) -> Iterator[ClaudeProfile]:
        return iter(self._profiles)

    def __len__(self) -> int:
        return len(self._profiles)

    @property
    def is_multi_profile(self) -> bool:
        return len(self._profiles) > 1

    def get(self, profile_id: str | None = None) -> ClaudeProfile:
        if profile_id is None:
            return self.default
        try:
            return self._by_id[profile_id]
        except KeyError as exc:
            raise ValueError(f"unknown Claude profile: {profile_id}") from exc

    def public_profiles(self) -> list[dict[str, str]]:
        return [profile.public() for profile in self._profiles]

    def wire_session_id(self, profile_id: str, native_session_id: str) -> str:
        profile = self.get(profile_id)
        if (
            not isinstance(native_session_id, str)
            or not _NATIVE_SESSION_ID.fullmatch(native_session_id)
        ):
            raise ValueError("invalid native Claude session id")
        if not self.is_multi_profile:
            return native_session_id
        routed = f"{profile.id}@{native_session_id}"
        if len(routed) > _MAX_WIRE_ID_CHARS:
            raise ValueError(
                "native Claude session id is too long for its profile")
        return routed

    def resolve_wire_session_id(
        self,
        wire_session_id: str,
    ) -> tuple[ClaudeProfile, str]:
        if (
            not isinstance(wire_session_id, str)
            or not wire_session_id
            or len(wire_session_id) > _MAX_WIRE_ID_CHARS
        ):
            raise ValueError("invalid Claude wire session id")
        if "@" not in wire_session_id:
            if self.is_multi_profile:
                raise ValueError(
                    "multi-profile Claude session ids must be namespaced")
            profile = self.default
            native_session_id = wire_session_id
        else:
            if not self.is_multi_profile:
                raise ValueError(
                    "single-profile Claude session ids must not be namespaced")
            profile_id, native_session_id = wire_session_id.split("@", 1)
            profile = self.get(profile_id)
        if not _NATIVE_SESSION_ID.fullmatch(native_session_id):
            raise ValueError("invalid native Claude session id")
        return profile, native_session_id


class ClaudeProfileTopologyStore:
    """Profile-id continuity keyed by private config-directory realpaths."""

    def __init__(self, state_dir: str | os.PathLike[str]) -> None:
        root = Path(state_dir)
        self.path = root / "claude-profile-topology.json"
        self.pending_path = root / "claude-profile-transition.json"

    def prepare(
        self,
        registry: ClaudeProfileRegistry,
        *,
        legacy_config_dir: str | os.PathLike[str],
    ) -> ClaudeProfileTopologyTransition | None:
        transition = self.transition(
            registry, legacy_config_dir=legacy_config_dir)
        pending = self._load_pending()
        if transition is None:
            if pending is not None:
                current = tuple(
                    (profile.id, str(profile.config_dir))
                    for profile in registry
                )
                if (
                    pending.get("current") != [
                        {"id": profile_id, "config_dir": config_dir}
                        for profile_id, config_dir in current
                    ]
                    or pending.get("current_default_id")
                        != registry.default.id
                ):
                    raise ValueError(
                        "Claude profile transition targets another registry")
                # A crash may land after the topology replace but before the
                # pending marker unlink. Every required store is revisioned and
                # replay-safe, so this marker is already complete.
                self._clear_pending()
            return None
        payload = transition.as_dict()
        if pending is not None:
            if pending != payload:
                raise ValueError(
                    "Claude profile transition targets another registry")
            return transition
        self._atomic_write(self.pending_path, payload)
        return transition

    def complete(
        self,
        registry: ClaudeProfileRegistry,
        transition: ClaudeProfileTopologyTransition,
    ) -> None:
        if self._load_pending() != transition.as_dict():
            raise ValueError("Claude profile transition marker is missing")
        self.persist(registry, revision=transition.revision)
        self._clear_pending()

    def transition(
        self,
        registry: ClaudeProfileRegistry,
        *,
        legacy_config_dir: str | os.PathLike[str],
    ) -> ClaudeProfileTopologyTransition | None:
        previous = self._load()
        current = tuple(
            (profile.id, str(profile.config_dir)) for profile in registry)
        if previous is not None and (
            previous.profiles == current
            and previous.default_id == registry.default.id
        ):
            return None

        if previous is None:
            legacy = str(ClaudeProfileRegistry._effective_config_dir(
                legacy_config_dir))
            matches = [
                profile for profile in registry
                if str(profile.config_dir) == legacy
            ]
            if len(matches) != 1:
                raise ValueError(
                    "initial Claude profile registry must contain the legacy "
                    "CLAUDE_CONFIG_DIR exactly once")
            old_profiles = ((matches[0].id, legacy),)
            old_default = matches[0].id
            revision = 1
        else:
            old_profiles = previous.profiles
            old_default = previous.default_id
            revision = previous.revision + 1

        remaps = self._remaps(old_profiles, registry)
        self._validate_replacements(old_profiles, registry, remaps)
        return ClaudeProfileTopologyTransition(
            revision=revision,
            previous=old_profiles,
            previous_default_id=old_default,
            current=current,
            current_default_id=registry.default.id,
            remaps=remaps,
        )

    def revision(
        self,
        registry: ClaudeProfileRegistry,
        *,
        legacy_config_dir: str | os.PathLike[str],
    ) -> int:
        transition = self.transition(
            registry, legacy_config_dir=legacy_config_dir)
        if transition is not None:
            return transition.revision
        previous = self._load()
        return previous.revision if previous is not None else 1

    @staticmethod
    def _remaps(
        previous: tuple[tuple[str, str], ...],
        registry: ClaudeProfileRegistry,
    ) -> dict[str, str]:
        old_by_dir = {
            config_dir: profile_id for profile_id, config_dir in previous}
        remaps = {
            old_by_dir[str(profile.config_dir)]: profile.id
            for profile in registry
            if str(profile.config_dir) in old_by_dir
            and old_by_dir[str(profile.config_dir)] != profile.id
        }
        old_ids = {profile_id for profile_id, _ in previous}
        targets = set(remaps.values())
        for start in tuple(remaps):
            if start in targets:
                continue
            tail = start
            seen: set[str] = set()
            while tail in remaps and tail not in seen:
                seen.add(tail)
                tail = remaps[tail]
            if tail in old_ids and tail not in remaps:
                remaps[tail] = start
        return remaps

    @staticmethod
    def _validate_replacements(
        previous: tuple[tuple[str, str], ...],
        registry: ClaudeProfileRegistry,
        remaps: dict[str, str],
    ) -> None:
        old_by_id = dict(previous)
        for profile in registry:
            old_dir = old_by_id.get(profile.id)
            if (
                old_dir is not None
                and old_dir != str(profile.config_dir)
                and profile.id not in remaps
            ):
                raise ValueError(
                    "Claude profile id cannot replace its config_dir without "
                    "preserving the previous account under another id")

    def persist(
        self,
        registry: ClaudeProfileRegistry,
        *,
        revision: int,
    ) -> None:
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
        ):
            raise ValueError("Claude profile topology revision is invalid")
        self._atomic_write(self.path, {
            "version": _TOPOLOGY_VERSION,
            "revision": revision,
            "default_id": registry.default.id,
            "profiles": [
                {"id": profile.id, "config_dir": str(profile.config_dir)}
                for profile in registry
            ],
        })

    @classmethod
    def _decode_profiles(
        cls,
        value: Any,
    ) -> tuple[tuple[str, str], ...]:
        if (
            not isinstance(value, list)
            or not value
            or len(value) > _MAX_PROFILES
        ):
            raise ValueError("invalid Claude profile topology file")
        result: list[tuple[str, str]] = []
        ids: set[str] = set()
        roots: set[str] = set()
        for entry in value:
            if (
                not isinstance(entry, dict)
                or set(entry) != {"id", "config_dir"}
            ):
                raise ValueError("invalid Claude profile topology file")
            profile_id = entry.get("id")
            if (
                not isinstance(profile_id, str)
                or not _PROFILE_ID.fullmatch(profile_id)
            ):
                raise ValueError("invalid Claude profile topology file")
            root = str(ClaudeProfileRegistry._config_dir(
                entry.get("config_dir")))
            if profile_id in ids or root in roots:
                raise ValueError("invalid Claude profile topology file")
            ids.add(profile_id)
            roots.add(root)
            result.append((profile_id, root))
        return tuple(result)

    def _load(self) -> _StoredTopology | None:
        payload = self._read_json(self.path, allow_missing=True)
        if payload is None:
            return None
        if (
            not isinstance(payload, dict)
            or set(payload) != {
                "version", "revision", "default_id", "profiles"}
            or payload.get("version") != _TOPOLOGY_VERSION
        ):
            raise ValueError("invalid Claude profile topology file")
        revision = payload.get("revision")
        default_id = payload.get("default_id")
        profiles = self._decode_profiles(payload.get("profiles"))
        ids = {profile_id for profile_id, _ in profiles}
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
            or not isinstance(default_id, str)
            or default_id not in ids
        ):
            raise ValueError("invalid Claude profile topology file")
        return _StoredTopology(revision, profiles, default_id)

    def _load_pending(self) -> dict[str, Any] | None:
        payload = self._read_json(self.pending_path, allow_missing=True)
        if payload is None:
            return None
        expected = {
            "version", "revision", "previous", "previous_default_id",
            "current", "current_default_id",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != expected
            or payload.get("version") != _TRANSITION_VERSION
        ):
            raise ValueError("invalid Claude profile transition file")
        revision = payload.get("revision")
        previous = self._decode_profiles(payload.get("previous"))
        current = self._decode_profiles(payload.get("current"))
        previous_default = payload.get("previous_default_id")
        current_default = payload.get("current_default_id")
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
            or previous_default not in {item[0] for item in previous}
            or current_default not in {item[0] for item in current}
        ):
            raise ValueError("invalid Claude profile transition file")
        return payload

    @staticmethod
    def _read_json(path: Path, *, allow_missing: bool) -> Any:
        try:
            info = path.lstat()
        except FileNotFoundError:
            if allow_missing:
                return None
            raise
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_size > _TOPOLOGY_MAX_BYTES
        ):
            raise ValueError("invalid Claude profile state file")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError("invalid Claude profile state file") from exc

    @staticmethod
    def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > _TOPOLOGY_MAX_BYTES:
            raise ValueError("Claude profile topology is too large")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                fd = -1
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            path.chmod(0o600)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _clear_pending(self) -> None:
        try:
            self.pending_path.unlink()
        except FileNotFoundError:
            return
        directory_fd = os.open(self.pending_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
