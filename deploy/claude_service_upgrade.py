"""Switch an idle, installer-managed Claude sidecar under the Wrapper activation lock.

OS operations run as the Wrapper's user. The parent activation receipt records
the old immutable source so a departed installer can restore the same pair.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cc_remote.claude_service.client import upgrade_inventory, worker_busy_for_upgrade
from deploy.install_claude_service import LABEL, UNIT, launch_agent, unit_text
from deploy.install_cli import _atomic_file


class MigrationError(ValueError):
    pass


def manager(*args: str) -> subprocess.CompletedProcess:
    if sys.platform == "darwin":
        command = ("/bin/launchctl", *args)
        env = None
    else:
        from cc_remote.wrapper.codex_service import user_manager_environment
        command = ("/usr/bin/systemctl", "--user", *args)
        env = user_manager_environment()
    return subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)


class Service:
    def __init__(self, socket: str):
        self.socket = Path(socket)
        self.directory = self.socket.parent
        info = self.directory.lstat()
        if (not self.socket.is_absolute() or self.socket.name != "service.sock"
                or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise MigrationError("unrecognized Claude service directory")
        self.mac = sys.platform == "darwin"
        if self.mac:
            self.domain = f"gui/{os.getuid()}"
            self.label = f"{self.domain}/{LABEL}"
            self.path = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
        else:
            self.path = Path.home() / ".config/systemd/user" / UNIT

    def definition(self, source: Path) -> bytes:
        python = source / ".venv/bin/python"
        if self.mac:
            return plistlib.dumps(launch_agent(source, self.directory, interpreter=python))
        return unit_text(source, self.directory, interpreter=python).encode()

    def matches(self, definition: bytes) -> bool:
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o022 or info.st_size > 8192):
                raise MigrationError("unsafe Claude service configuration")
            observed = stream.read(8193)
        if self.mac:
            return plistlib.loads(observed) == plistlib.loads(definition)
        return observed == definition

    def pid(self) -> int | None:
        if self.mac:
            result = manager("print", self.label)
            if result.returncode == 113:  # EDOM: launchd could not find this job.
                return None
            if result.returncode or f"path = {self.path}" not in result.stdout:
                raise MigrationError("Claude LaunchAgent does not match the registered definition")
            pid = re.search(r"^\s*pid = ([1-9][0-9]*)\s*$", result.stdout, re.MULTILINE)
            return int(pid[1]) if pid else 0
        result = manager("show", "--property=MainPID,LoadState,FragmentPath,DropInPaths", UNIT)
        if result.returncode:
            raise MigrationError("Claude systemd user unit could not be verified")
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if values.get("LoadState") == "not-found":
            return 0
        if (values.get("LoadState") != "loaded" or values.get("FragmentPath") != str(self.path)
                or values.get("DropInPaths")):
            raise MigrationError("Claude systemd user unit does not match the registered definition")
        return int(values["MainPID"])

    def probe(self, source: Path, sdk: str, *, pid: int | None, require_idle: bool = False) -> bool:
        if not pid:
            return False
        try:
            info = self.socket.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                return False
            hello, workers = asyncio.run(upgrade_inventory(str(self.socket)))
            return (
                hello["pid"] == pid and hello["source"] == str(source) and hello["sdk_version"] == sdk
                and (not require_idle or not any(map(worker_busy_for_upgrade, workers)))
            )
        except (OSError, TimeoutError, RuntimeError, ValueError, TypeError, KeyError):
            return False

    def wait(self, source: Path, sdk: str) -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.probe(source, sdk, pid=self.pid()):
                return
            time.sleep(0.25)
        raise MigrationError("Claude service failed its SDK handshake")

    def stop(self) -> None:
        args = ("bootout", self.label) if self.mac else ("stop", UNIT)
        if manager(*args).returncode:
            raise MigrationError("Claude service stop could not be verified")
        remaining = self.pid()
        if remaining or (self.mac and remaining is not None):
            raise MigrationError("Claude service stop could not be verified")

    def start(self, definition: bytes) -> None:
        _atomic_file(self.path, definition, 0o600)
        if self.mac:
            commands = [("bootstrap", self.domain, str(self.path))]
        else:
            commands = [("daemon-reload",), ("start", UNIT)]
        for command in commands:
            if manager(*command).returncode:
                raise MigrationError("Claude service could not be started")


def migrate(action: str, old: dict, release: Path, sdk: str) -> None:
    service = Service(old["socket"])
    source = Path(old["source"])
    previous = service.definition(source)
    target = service.definition(release)
    pid = service.pid()
    if action in {"plan", "switch"}:
        if not service.matches(previous) or pid != old["pid"]:
            raise MigrationError("Claude service is not the managed service admitted for this upgrade")
        if not service.probe(source, old["sdk"], pid=pid, require_idle=True):
            raise MigrationError("Claude service has unfinished work or its identity changed")
        if action == "switch":
            service.stop()
            service.start(target)
            service.wait(release, sdk)
    elif action == "restore":
        old_definition = service.matches(previous)
        if old_definition and service.probe(source, old["sdk"], pid=pid):
            return
        if not old_definition and not service.matches(target):
            raise MigrationError("Claude service configuration changed during the upgrade")
        # A failing launchd job can remain loaded with no PID. Unload its
        # definition before restoring it, or launchd will reject bootstrap.
        if pid or service.mac and pid is not None:
            service.stop()
        service.start(previous)
        service.wait(source, old["sdk"])
    elif action == "verify":
        if not service.matches(target) or not service.probe(release, sdk, pid=pid):
            raise MigrationError("Claude service is not serving the selected release")
    else:
        raise MigrationError("unknown Claude migration operation")


if __name__ == "__main__":
    try:
        if os.getuid() == 0 or sys.platform not in {"linux", "darwin"}:
            raise MigrationError("Claude service migration must run as its owner")
        request = json.load(sys.stdin)
        migrate(sys.argv[1], request["previous"], Path(request["release"]), request["sdk"])
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        message = str(exc) if isinstance(exc, MigrationError) else "Claude service migration could not be verified"
        sys.exit(message)
