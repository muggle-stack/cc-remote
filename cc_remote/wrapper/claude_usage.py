"""Account-scoped, read-only usage helpers; credentials stay with the provider.

An operator-owned helper performs the authenticated GET and returns only usage
JSON. The Wrapper never reads OAuth/keychain/model credentials or runs a chat
command to obtain quota. Helpers are local configuration, never wire inputs.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import stat
import time

from cc_remote.claude_profiles import ClaudeProfile


MAX_BYTES = 64 * 1024
TIMEOUT = 15
REFRESH_INTERVAL = 15


class ClaudeUsageError(RuntimeError):
    """Only a fixed, credential-free reason may cross the control link."""


class ClaudeUsageReader:
    def __init__(self, state_dir: str | os.PathLike[str]):
        self.path = Path(state_dir) / "claude-usage-helpers.json"
        self._locks: dict[str, asyncio.Lock] = {}
        self._recent: dict[str, tuple[float, tuple[str, ...], str | None]] = {}

    def command(self, profile: ClaudeProfile) -> tuple[str, ...] | None:
        try:
            with self.path.open("rb") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o077 or info.st_size > MAX_BYTES):
                    raise ValueError()
                raw = json.loads(stream.read(MAX_BYTES + 1))
            if not isinstance(raw, dict) or raw.get("version") != 1:
                raise ValueError()
            entries = raw["profiles"]
            if not isinstance(entries, dict):
                raise ValueError()
            entry = entries.get(profile.id)
            if entry is None:
                return None
            if (not isinstance(entry, dict) or not isinstance(entry.get("config_dir"), str)
                    or Path(entry["config_dir"]).expanduser().resolve() != profile.config_dir):
                raise ValueError()
            command = entry["command"]
            if (not isinstance(command, list) or not 1 <= len(command) <= 16
                    or any(not isinstance(arg, str) or not arg or len(arg) > 4096
                           or "\x00" in arg for arg in command)
                    or not Path(command[0]).is_absolute()):
                raise ValueError()
            return tuple(command)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError):
            raise ClaudeUsageError("usage helper configuration invalid") from None

    async def refresh(self, profile: ClaudeProfile, apply) -> str | None:
        """Serialize each account's reads, including browser retry storms.

        Only refresh completion is cached here. The shared quota store remains
        authoritative, so native events received after a GET are never replaced
        with an old cached response.
        """
        async with self._locks.setdefault(profile.id, asyncio.Lock()):
            command = self.command(profile)
            if command is None:
                return "usage helper unavailable"
            recent = self._recent.get(profile.id)
            if (recent and recent[1] == command
                    and time.monotonic() - recent[0] < REFRESH_INTERVAL):
                return recent[2]
            error = None
            try:
                await apply(lambda: self._read(profile, command))
            except ClaudeUsageError as exc:
                error = str(exc)
            self._recent[profile.id] = (time.monotonic(), command, error)
            return error

    async def _read(self, profile: ClaudeProfile, command: tuple[str, ...]) -> dict:
        # No shell, project environment, prompt, relay secret or ambient model
        # selector. The helper receives the exact public profile binding on stdin.
        env = {key: os.environ[key] for key in (
            "HOME", "PATH", "LANG", "LC_ALL", "TMPDIR", "TMP", "TEMP",
        ) if key in os.environ}
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                cwd=Path.home(), env=env, start_new_session=True,
            )
            async def collect():
                process.stdin.write(json.dumps({
                    "version": 1, "profile_id": profile.id,
                    "config_dir": str(profile.config_dir),
                }).encode() + b"\n")
                await process.stdin.drain()
                process.stdin.close()
                output = bytearray()
                while chunk := await process.stdout.read(4096):
                    output.extend(chunk)
                    if len(output) > MAX_BYTES:
                        raise ClaudeUsageError("usage response invalid")
                if await process.wait() != 0:
                    raise ClaudeUsageError("usage request failed")
                return output
            data = json.loads(await asyncio.wait_for(collect(), TIMEOUT))
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except TimeoutError:
            raise ClaudeUsageError("usage request timed out") from None
        except (OSError, ValueError, RecursionError):
            raise ClaudeUsageError("usage response unavailable") from None
        finally:
            if process is not None and process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
