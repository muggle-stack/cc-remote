"""Forward an agy stdio MCP connection to its Wrapper-owned session socket."""
from __future__ import annotations

import os
import socket
import stat
import sys
from contextlib import suppress
from pathlib import Path
from threading import Thread


def main() -> None:
    endpoint = Path(sys.argv[1])
    try:
        directory = endpoint.parent.lstat()
        info = endpoint.lstat()
        if (not stat.S_ISDIR(directory.st_mode) or not stat.S_ISSOCK(info.st_mode)
                or any(entry.st_uid != os.geteuid() or entry.st_mode & 0o077
                       for entry in (directory, info))):
            raise OSError("Invalid session socket")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(str(endpoint))

            def forward_input():
                with suppress(OSError):
                    while chunk := os.read(0, 65536):
                        connection.sendall(chunk)
                with suppress(OSError):
                    connection.shutdown(socket.SHUT_WR)

            Thread(target=forward_input, daemon=True).start()
            while chunk := connection.recv(65536):
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
    except OSError:
        raise SystemExit("CC Remote session tool connection closed.") from None


if __name__ == "__main__":
    main()
