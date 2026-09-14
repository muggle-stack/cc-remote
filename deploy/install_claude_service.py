"""Install the SDK service as the Wrapper's user; never restart a live service.

Run with the staged release's Python, from its immutable source root. The unit
uses that exact venv and source until an explicitly scheduled service upgrade.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cc_remote.claude_service.client import Connection
from cc_remote.claude_service.wire import private_directory

LABEL = "com.muggle.cc-remote.claude-service"
UNIT = "cc-remote-claude-service.service"


def commands(source: Path, state_dir: Path) -> list[str]:
    executable = Path(sys.executable)
    python = executable.parent.resolve() / executable.name
    return [str(python), "-m", "cc_remote.claude_service", "--state-dir", str(state_dir)]


def unit_text(source: Path, state_dir: Path) -> str:
    def quote(value):
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
    argv = " ".join(quote(value) for value in commands(source, state_dir))
    return f"""[Unit]
Description=cc-remote persistent Claude SDK sessions

[Service]
Type=simple
WorkingDirectory={quote(source)}
ExecStart={argv}
UMask=0077
Restart=on-failure
RestartSec=3
KillMode=control-group

[Install]
WantedBy=default.target
"""


async def healthy(path: Path) -> bool:
    connection = Connection(str(path))
    try:
        await asyncio.wait_for(connection.connect(), 2)
        await connection.call("list", timeout=3)
        return True
    except (OSError, TimeoutError, RuntimeError):
        return False
    finally:
        await connection.disconnect()


def install(source: Path, state_dir: Path) -> None:
    private_directory(state_dir)
    socket_path = state_dir / "service.sock"
    # This is a readiness probe, not a reason to terminate a process whose
    # protocol/version cannot be read. Existing units are never replaced here.
    if asyncio.run(healthy(socket_path)):
        print(f"Claude service already running: {socket_path}")
        return
    if sys.platform == "darwin":
        destination = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
        if destination.exists():
            raise RuntimeError("existing Claude service is not ready; inspect it before retrying")
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "Label": LABEL, "ProgramArguments": commands(source, state_dir),
            "WorkingDirectory": str(source), "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False}, "Umask": 0o077,
            "StandardOutPath": str(state_dir / "stdout.log"),
            "StandardErrorPath": str(state_dir / "stderr.log"),
        }
        destination.write_bytes(plistlib.dumps(payload))
        destination.chmod(0o600)
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(destination)], check=True)
    elif sys.platform == "linux":
        destination = Path.home() / ".config/systemd/user" / UNIT
        if destination.exists():
            raise RuntimeError("existing Claude service is not ready; inspect it before retrying")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(unit_text(source, state_dir))
        destination.chmod(0o600)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", UNIT], check=True)
    else:
        raise RuntimeError("Claude session service requires macOS or Linux")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--register-wrapper", type=Path,
                        help="write an explicit registration inside this Wrapper state directory")
    args = parser.parse_args()
    if os.getuid() == 0:
        parser.error("run as the Wrapper user, not root")
    source = Path(__file__).resolve().parents[1]
    state_dir = args.state_dir.expanduser().absolute()
    install(source, state_dir)
    if args.register_wrapper is not None:
        directory = args.register_wrapper.expanduser().absolute()
        private_directory(directory)
        destination = directory / "claude-service.json"
        payload = {"socket": str(state_dir / "service.sock")}
        if destination.exists() or destination.is_symlink():
            if (destination.is_symlink() or destination.stat().st_uid != os.getuid()
                    or destination.stat().st_mode & 0o077
                    or json.loads(destination.read_text()) != payload):
                raise RuntimeError("existing Claude registration differs; inspect it before changing it")
        else:
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as out:
                json.dump(payload, out)


if __name__ == "__main__":
    main()
