"""Wrapper-owned Gemini selections for one native ACP session."""
from __future__ import annotations

import os
import stat
from pathlib import Path

from cc_remote.update import UpdateError
from cc_remote.update_jobs import read_json, write_json
from cc_remote.wrapper.antigravity_client import AcpError
from cc_remote.wrapper.antigravity_stream import wire_session_id

CONTROL_CATEGORIES = ("model", "thought_level", "mode")


class AntigravityControlStore:
    def __init__(self, state_dir: Path):
        self.directory = state_dir / "antigravity-session-controls"

    def path(self, sid: str) -> Path:
        if wire_session_id(sid.removeprefix("agy@")) != sid:
            raise ValueError("Invalid Gemini session ID")
        info = self.directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077):
            raise ValueError("Unsafe Gemini control directory")
        return self.directory / f"{sid}.json"

    def get(self, sid: str) -> dict[str, str]:
        try:
            data = read_json(self.path(sid), os.geteuid(), limit=16 * 1024)
            options = data.get("options")
            if (data.get("schema") != 1 or data.get("sid") != sid
                    or not isinstance(options, dict)
                    or any(key not in CONTROL_CATEGORIES or not isinstance(value, str)
                           or not 0 < len(value) <= 256 for key, value in options.items())):
                raise ValueError("Invalid Gemini session settings")
            return options
        except FileNotFoundError:
            return {}
        except (OSError, UpdateError, ValueError):
            raise AcpError("controls_state", "本机 Gemini 会话设置无法读取，请修复本机状态文件后重试。") from None

    def save(self, sid: str, options: dict[str, str]) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_json(self.path(sid), {"schema": 1, "sid": sid, "options": options}, mode=0o600)
