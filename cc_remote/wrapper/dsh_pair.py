"""Pair a Wrapper with an already-running DSH using its official login URL.

Run locally: python -m cc_remote.wrapper.dsh_pair --file <private-file>
The URL is read without echo, never placed in argv or sent to the relay.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import stat
import tempfile
from pathlib import Path

from cc_remote.wrapper.dsh_client import (
    DshClient, DshConnection, DshError, exchange_launch_url,
)


def save_connection(connection: DshConnection, path: Path) -> None:
    path = path.expanduser().absolute()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.parent.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
        raise DshError("invalid_connection", "配对文件所在目录必须由当前用户拥有且其他用户不可写。")
    fd, staging = tempfile.mkstemp(prefix=".dsh-pair-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"origin": connection.origin, "cookie": connection.cookie}, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staging, path)
    finally:
        if os.path.exists(staging):
            os.unlink(staging)


async def pair(launch_url: str, path: Path) -> None:
    connection = await exchange_launch_url(launch_url)
    client = DshClient(connection)
    try:
        # Read-only contract validation. Pairing must not create/resume an Agent.
        roster = await client.rpc("agentPresets/list")
        catalog = await client.rpc("session/modelCatalog")
        await client.list_sessions()
        if (not isinstance(roster, dict) or not isinstance(roster.get("presets"), list)
                or not isinstance(catalog, dict) or not isinstance(catalog.get("groups"), list)):
            raise DshError("unsupported", "DSH 未提供 0.1.5 的模型与 Preset 接口。")
    finally:
        await client.close()
    save_connection(connection, path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Pair cc-remote with local DSH 0.1.5")
    parser.add_argument("--file", type=Path, required=True, help="Private Wrapper connection file")
    args = parser.parse_args()
    try:
        url = getpass.getpass("DSH 本机登录地址（输入不回显）：")
        asyncio.run(pair(url, args.file))
    except (EOFError, KeyboardInterrupt):
        return 130
    except DshError as exc:
        print(str(exc))
        return 1
    print("DSH 本机配对完成；配对文件已保存。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
