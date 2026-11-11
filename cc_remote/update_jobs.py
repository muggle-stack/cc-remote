"""Durable, bounded UI update receipts; browser input never supplies commands."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time

from cc_remote.update import UpdateError, version_tuple

POLICY = "ui-update-policy.json"
JOBS = "ui-update-jobs"
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "blocked"})
PHASES = frozenset({"submitting", "preparing", "downloading", "validating", "waiting",
                   "updating_relay", "installing", "verifying", "unknown", *TERMINAL})
MAX_JOBS = 1024


def job_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise UpdateError("invalid update request identity")
    return value


def helper_path(root: Path) -> Path:
    suffix = hashlib.sha256(str(root).encode()).hexdigest()[:16]
    return Path("/usr/local/libexec") / f"cc-remote-ui-update-{suffix}"


def read_json(path: Path, owner: int, *, limit: int = 32768) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner
                or info.st_mode & 0o022 or info.st_size > limit):
            raise UpdateError("unsafe update receipt")
        value = json.loads(stream.read(limit + 1))
    if not isinstance(value, dict):
        raise UpdateError("invalid update receipt")
    return value


def write_json(path: Path, value: dict, *, mode: int = 0o644) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".update-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), mode)
            json.dump(value, stream, ensure_ascii=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


class UpdateJobs:
    def __init__(self, root: Path):
        self.root = root
        self.owner = root.stat().st_uid
        self.folder = root / JOBS

    def policy(self) -> dict:
        value = read_json(self.root / POLICY, self.owner)
        if (type(value.get("schema")) is not int or value["schema"] != 1 or value.get("root") != str(self.root)
                or type(value.get("enabled")) is not bool
                or type(value.get("allow_relay_update")) is not bool
                or type(value.get("uid")) is not int
                or value.get("role") not in ("wrapper", "relay")
                or not isinstance(value.get("repository"), str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["repository"])
                or (value["role"] == "wrapper" and (
                    not isinstance(value.get("control_socket"), str)
                    or not Path(value["control_socket"]).is_absolute()))
                or (value["role"] == "relay" and (
                    value.get("control_socket") is not None or value["allow_relay_update"]))):
            raise UpdateError("invalid UI update policy")
        return value

    def validate_folder(self) -> None:
        info = self.folder.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != self.owner
                or info.st_mode & 0o022 or self.root.resolve() != self.root):
            raise UpdateError("unsafe update job directory")

    def state(self, identity: str) -> dict:
        self.validate_folder()
        value = read_json(self.folder / f"{job_id(identity)}.json", self.owner)
        if (value.get("schema") != 1 or value.get("id") != identity
                or value.get("phase") not in PHASES):
            raise UpdateError("invalid update job state")
        version_tuple(value.get("version"))
        version_tuple(value.get("previous"))
        return value

    def active(self) -> dict | None:
        self.validate_folder()
        try:
            active = read_json(self.folder / "active.json", self.owner)
        except FileNotFoundError:
            return None
        return self.state(job_id(active.get("id")))

    def save(self, value: dict) -> None:
        self.validate_folder()
        value = {**value, "updated_at": time.time()}
        write_json(self.folder / f"{job_id(value['id'])}.json", value)

    @contextmanager
    def lock(self, *, blocking: bool = False):
        self.validate_folder()
        fd = os.open(self.folder / "launch.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner or info.st_mode & 0o022:
                raise UpdateError("unsafe update job lock")
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            yield
        finally:
            os.close(fd)

    def create(self, identity: str, version: str, current: str) -> tuple[dict, bool]:
        """Caller holds lock. Persist intent before the OS accepts the job."""
        job_id(identity)
        version_tuple(version)
        try:
            prior = self.state(identity)
        except FileNotFoundError:
            prior = None
        if prior:
            if prior["version"] != version:
                raise UpdateError("update request identity was reused")
            return prior, False
        active = self.active()
        if active and active["phase"] not in TERMINAL:
            raise UpdateError("another update is already pending")
        if sum(1 for _ in self.folder.glob("[0-9a-f]" * 32 + ".json")) >= MAX_JOBS:
            raise UpdateError("update receipt limit reached; administrator maintenance required")
        value = {"schema": 1, "id": identity, "version": version, "previous": current,
                 "phase": "submitting", "created_at": time.time(), "reason": None}
        self.save(value)
        write_json(self.folder / "active.json", {"id": identity})
        return value, True


def public_job(value: dict | None) -> dict | None:
    if value is None:
        return None
    # No local paths, subprocess output, commands, environment or credentials.
    return {key: value.get(key) for key in (
        "id", "version", "previous", "phase", "reason", "updated_at",
        "created_at", "cleanup_pending", "cancel_requested",
    )}


def failure_reason(exc: BaseException) -> str:
    import ssl
    from urllib.error import URLError
    if isinstance(exc, URLError) and isinstance(exc.reason, BaseException):
        return failure_reason(exc.reason)
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "tls_certificate"
    if isinstance(exc, ssl.SSLError):
        return "tls_connection"
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "network"
    message = str(exc).lower()
    for pattern, reason in (
        ("control connection unavailable", "control_unavailable"),
        ("cancelled before activation", "cancelled"),
        ("certificate verification", "tls_certificate"), ("tls", "tls_connection"),
        ("ssh authentication", "ssh_auth"), ("host key verification", "ssh_host_key"),
        ("ssh connection", "network"), ("ssh hostname", "network"), ("ssh host is unreachable", "network"),
        ("no space left", "disk_space"), ("disk space", "disk_space"),
        ("disabled", "setup_required"), ("selected release changed", "release_changed"),
        ("os job manager", "unsupported"),
        ("not newer", "release_changed"),
        ("protocol", "protocol_change"), ("service compatibility", "service_migration"),
        ("upstream upgrade is not authorized", "relay_permission"),
        ("ssh", "relay_permission"), ("sudo", "permission_required"),
        ("permission", "permission_required"), ("lock", "busy"),
        ("another update", "busy"), ("receipt limit", "receipt_limit"),
        ("download", "network"), ("github", "network"),
    ):
        if pattern in message:
            return reason
    return "update_failed"
