"""Durable ownership claims for turns started by cc-remote.

The official shared daemon can outlive the wrapper process.  A lease is only an
attribution hint: recovery still requires the same turn to be the rollout tail
and the official thread status to be active.  No Codex credentials or prompts
are stored here.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import string
import tempfile
import time
from typing import Optional

from cc_remote.wrapper.os_compat import fchmod


_SCHEMA_VERSION = 1
_FILENAME = "codex-turn-leases.json"
_MAX_BYTES = 64 * 1024
_MAX_LEASES = 64
_MAX_VALUE_LENGTH = 512


@dataclass(frozen=True)
class CodexTurnLease:
    session_id: str
    turn_id: str
    msg_id: str
    daemon_epoch: Optional[str]
    automatic: bool
    updated_at: float


class CodexTurnLeaseStore:
    def __init__(self, state_dir: str | Path):
        self.path = Path(state_dir).expanduser() / _FILENAME

    @staticmethod
    def _valid_text(value: object) -> bool:
        return (
            isinstance(value, str)
            and 0 < len(value) <= _MAX_VALUE_LENGTH
            and "\x00" not in value
        )

    @staticmethod
    def _valid_epoch(value: object) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 32
            and all(char in string.hexdigits for char in value)
        )

    def _read(self) -> dict[str, CodexTurnLease]:
        try:
            if self.path.stat().st_size > _MAX_BYTES:
                return {}
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict) or raw.get("version") != _SCHEMA_VERSION:
            return {}
        records = raw.get("leases")
        if not isinstance(records, dict) or len(records) > _MAX_LEASES:
            return {}
        leases: dict[str, CodexTurnLease] = {}
        for session_id, record in records.items():
            if (
                not self._valid_text(session_id)
                or not isinstance(record, dict)
                or not self._valid_text(record.get("turn_id"))
                or not self._valid_text(record.get("msg_id"))
                or (
                    record.get("daemon_epoch") is not None
                    and not self._valid_epoch(record.get("daemon_epoch"))
                )
                or not isinstance(record.get("automatic", False), bool)
                or not isinstance(record.get("updated_at"), (int, float))
                or isinstance(record.get("updated_at"), bool)
            ):
                continue
            leases[session_id] = CodexTurnLease(
                session_id=session_id,
                turn_id=record["turn_id"],
                msg_id=record["msg_id"],
                daemon_epoch=record.get("daemon_epoch"),
                automatic=record.get("automatic", False),
                updated_at=float(record["updated_at"]),
            )
        return leases

    def _write(self, leases: dict[str, CodexTurnLease]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = json.dumps({
            "version": _SCHEMA_VERSION,
            "leases": {
                session_id: {
                    "turn_id": lease.turn_id,
                    "msg_id": lease.msg_id,
                    "daemon_epoch": lease.daemon_epoch,
                    "automatic": lease.automatic,
                    "updated_at": lease.updated_at,
                }
                for session_id, lease in leases.items()
            },
        }, separators=(",", ":")) + "\n"
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            fchmod(fd, temporary, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                fd = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def get(self, session_id: str) -> Optional[CodexTurnLease]:
        return self._read().get(session_id)

    def list(self) -> tuple[CodexTurnLease, ...]:
        return tuple(sorted(
            self._read().values(),
            key=lambda lease: lease.updated_at,
            reverse=True,
        ))

    def claim(
        self,
        session_id: str,
        turn_id: str,
        msg_id: str,
        *,
        daemon_epoch: Optional[str] = None,
        automatic: bool = False,
    ) -> None:
        if not all(self._valid_text(value)
                   for value in (session_id, turn_id, msg_id)):
            raise ValueError("invalid Codex turn lease")
        if daemon_epoch is not None and not self._valid_epoch(daemon_epoch):
            raise ValueError("invalid Codex daemon epoch")
        if not isinstance(automatic, bool):
            raise ValueError("invalid Codex automatic-turn flag")
        leases = self._read()
        leases.pop(session_id, None)
        leases[session_id] = CodexTurnLease(
            session_id=session_id,
            turn_id=turn_id,
            msg_id=msg_id,
            daemon_epoch=daemon_epoch,
            automatic=automatic,
            updated_at=time.time(),
        )
        while len(leases) > _MAX_LEASES:
            leases.pop(next(iter(leases)))
        self._write(leases)

    def release(
        self, session_id: str, *, turn_id: Optional[str] = None,
    ) -> bool:
        leases = self._read()
        current = leases.get(session_id)
        if current is None or (
            turn_id is not None and current.turn_id != turn_id
        ):
            return False
        leases.pop(session_id, None)
        self._write(leases)
        return True
