"""Bounded local framing. This is not the browser/relay wire protocol."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import socket
import struct
import sys
from pathlib import Path

VERSION = 1
MAX_FRAME = 32 * 1024 * 1024


class ControllerLeaseConflict(RuntimeError):
    """An existing native worker still belongs to another controller."""


async def read_frame(reader: asyncio.StreamReader) -> dict:
    size = struct.unpack("!I", await reader.readexactly(4))[0]
    if not 0 < size <= MAX_FRAME:
        raise ValueError("invalid Claude service frame size")
    value = json.loads(await reader.readexactly(size))
    if not isinstance(value, dict) or value.get("v") != VERSION:
        raise ValueError("incompatible Claude service protocol")
    return value


async def write_frame(writer: asyncio.StreamWriter, value: dict) -> None:
    data = json.dumps({**value, "v": VERSION}, ensure_ascii=False).encode()
    if len(data) > MAX_FRAME:
        raise ValueError("Claude service frame too large")
    writer.write(struct.pack("!I", len(data)) + data)
    await writer.drain()


def same_user(writer: asyncio.StreamWriter) -> bool:
    sock = writer.get_extra_info("socket")
    if sys.platform == "darwin":
        # SOL_LOCAL / LOCAL_PEERCRED: xucred = version, uid, ngroups, groups.
        cred = sock.getsockopt(0, 1, 80)
        return struct.unpack_from("=I", cred, 4)[0] == os.getuid()
    cred = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    return struct.unpack("3i", cred)[1] == os.getuid()


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError("Claude service directory must be private and owned by this user")


def encode_sdk(value):
    """Only SDK dataclasses cross this boundary; never pickle executable data."""
    if dataclasses.is_dataclass(value):
        return {"sdk_type": type(value).__name__, "fields": {
            field.name: encode_sdk(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }}
    if isinstance(value, dict):
        return {key: encode_sdk(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode_sdk(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported SDK value: {type(value).__name__}")


def decode_sdk(value):
    if isinstance(value, list):
        return [decode_sdk(item) for item in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {"sdk_type", "fields"}:
        from claude_agent_sdk import types

        cls = getattr(types, value["sdk_type"], None)
        if not isinstance(cls, type) or not dataclasses.is_dataclass(cls):
            raise ValueError("unknown SDK data type")
        return cls(**{key: decode_sdk(item) for key, item in value["fields"].items()})
    return {key: decode_sdk(item) for key, item in value.items()}
