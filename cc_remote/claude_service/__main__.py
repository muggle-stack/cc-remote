"""Run the SDK service under its own LaunchAgent or systemd user unit."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import signal
import stat
from pathlib import Path

from cc_remote.wrapper.process_scan import process_identity

from .server import Service
from .wire import private_directory


async def serve(directory: Path) -> None:
    private_directory(directory)
    lock_fd = os.open(directory / "service.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = directory / "service.sock"
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if info.st_uid != os.getuid() or not stat.S_ISSOCK(info.st_mode):
                raise PermissionError("unsafe Claude service socket")
            path.unlink()
        service = Service(directory)
        server = await asyncio.start_unix_server(service.connection, path)
        os.chmod(path, 0o600)
        identity = process_identity(os.getpid())
        state = {
            "pid": os.getpid(), "start_ticks": identity.start_ticks if identity else None,
            "source": str(Path(__file__).resolve().parents[2]),
            "socket": str(path), "protocol": 1,
        }
        descriptor = directory / "service.json"
        fd = os.open(descriptor, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump(state, out)
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stopped.set)
        try:
            await stopped.wait()
        finally:
            server.close()
            await server.wait_closed()
            await asyncio.gather(*(session.close() for session in service.sessions.values()))
            path.unlink(missing_ok=True)
            descriptor.unlink(missing_ok=True)
    finally:
        os.close(lock_fd)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    arguments = parser.parse_args()
    asyncio.run(serve(arguments.state_dir.expanduser().absolute()))


if __name__ == "__main__":
    main()
