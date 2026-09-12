"""Bounded, non-recursive directory reads starting at a session's cwd."""

from __future__ import annotations

import os
from pathlib import Path
import stat

MAX_DIRECTORY_ENTRIES = 20_000


def browse_workspace(
    cwd: str, path: str, *, offset: int = 0, limit: int = 100,
    hidden: bool = False, revision: str | None = None,
    confine_to_cwd: bool = True,
) -> dict:
    start = os.path.realpath(cwd)
    root = start if confine_to_cwd else os.path.abspath(os.sep)
    candidate = os.path.abspath(os.path.join(start, os.path.expanduser(path or ".")))
    if os.path.commonpath([root, candidate]) != root:
        raise ValueError("请选择当前会话目录内的文件或文件夹")
    parts = Path(os.path.relpath(candidate, root)).parts
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(root, flags)
    try:
        for index, part in enumerate(parts):
            info = os.stat(part, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISREG(info.st_mode) and index == len(parts) - 1:
                return {"root": root, "path": candidate, "kind": "file"}
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("不支持打开符号链接或特殊文件")
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        current_revision = f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}"
        if offset and revision != current_revision:
            raise ValueError("目录已变化，请刷新后重新查看")
        entries = []
        scanned = 0
        with os.scandir(fd) as iterator:
            for entry in iterator:
                scanned += 1
                if scanned > MAX_DIRECTORY_ENTRIES:
                    raise ValueError("目录条目过多，请输入更具体的子目录路径")
                if not hidden and entry.name.startswith("."):
                    continue
                kind = ("directory" if entry.is_dir(follow_symlinks=False)
                        else "file" if entry.is_file(follow_symlinks=False)
                        else "unsupported")
                entries.append({"name": entry.name,
                                "path": os.path.join(candidate, entry.name),
                                "kind": kind})
        entries.sort(key=lambda entry: (
            entry["kind"] != "directory", entry["name"].casefold(), entry["name"]))
        return {
            "root": root, "path": candidate, "kind": "directory",
            "parent": os.path.dirname(candidate) if candidate != root else None,
            "entries": entries[offset:offset + limit],
            "revision": current_revision,
            "next_offset": offset + limit if offset + limit < len(entries) else None,
        }
    finally:
        os.close(fd)
