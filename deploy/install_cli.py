#!/usr/bin/env python3
"""Register the release-management command and non-secret install identity."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import tempfile


_HEADER = b"#!/usr/bin/env bash\n# cc-remote release management launcher."


def check_destination(destination: Path) -> None:
    if destination.is_symlink():
        raise ValueError(f"refusing to replace an existing command symlink: {destination}")
    if destination.exists():
        if not destination.is_file():
            raise ValueError(f"command destination is not a regular file: {destination}")
        with destination.open("rb") as stream:
            if not stream.read(len(_HEADER)).startswith(_HEADER):
                raise ValueError(f"refusing to replace an unrelated command: {destination}")


def _atomic_file(destination: Path, content: bytes, mode: int) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            os.fchmod(stream.fileno(), mode)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def install_cli(root: Path, destination: Path, *, role: str, user: str | None = None,
                domain: str | None = None) -> None:
    check_destination(destination)
    current = root / "current"
    if not current.is_symlink() or current.resolve().parent != (root / "releases").resolve():
        raise ValueError("management command requires an active immutable Release installation")
    source = current / "bin" / "cc-remote"
    content = source.read_bytes()
    if not content.startswith(_HEADER) or not stat.S_ISREG(source.stat().st_mode):
        raise ValueError("release management launcher is missing or invalid")
    metadata = {"schema": 1, "role": role}
    if role == "wrapper":
        if not user or user == "root":
            raise ValueError("wrapper management requires the original service user")
        metadata["user"] = user
    elif role == "relay":
        if not domain:
            raise ValueError("relay management requires the configured domain")
        metadata["domain"] = domain
    else:
        raise ValueError("unknown installation role")
    destination.parent.mkdir(parents=True, exist_ok=True)
    previous = destination.read_bytes() if destination.exists() else None
    previous_mode = stat.S_IMODE(destination.stat().st_mode) if previous is not None else 0o755
    _atomic_file(destination, content, 0o755)
    try:
        _atomic_file(root / "installation.json", (json.dumps(metadata, sort_keys=True) + "\n").encode(), 0o644)
    except OSError:
        if previous is None:
            destination.unlink()
        else:
            _atomic_file(destination, previous, previous_mode)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--role", choices=("relay", "wrapper"))
    parser.add_argument("--user")
    parser.add_argument("--domain")
    args = parser.parse_args()
    try:
        if args.check:
            check_destination(args.destination)
        else:
            if args.root is None or args.role is None:
                parser.error("--root and --role are required for registration")
            install_cli(args.root, args.destination, role=args.role, user=args.user, domain=args.domain)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"ERROR: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
