"""Validate an explicit Linux legacy-to-managed Wrapper migration.

Only the existing system service is eligible. No discovery, new pairing, copying
native state or execution of code from the legacy installation occurs here.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import stat
import subprocess


def root_owned(path: Path) -> None:
    info = path.lstat()
    if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode):
        raise ValueError("legacy install/service must be root-owned and not writable by other users")


def rebase_service(text: str, current: Path, target: Path, user: str) -> str:
    """Preserve operator policy across migration and later managed upgrades."""
    values: dict[str, list[str]] = {}
    section = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
        elif section == "[Service]" and line and not line.startswith(("#", ";")) and "=" in line:
            key, value = line.split("=", 1)
            values.setdefault(key, []).append(value)
    if (values.get("User") != [user] or values.get("WorkingDirectory") != [str(current)]
            or values.get("ExecStart") != [f"{current}/.venv/bin/python -m cc_remote.wrapper"]):
        raise ValueError("service identity/command does not match the requested root and user")
    env_files = values.get("EnvironmentFile", [])
    if not env_files or any(p.lstrip("-") not in {
        "/etc/cc-remote/wrapper.env", "/etc/cc-remote/device.env",
    } for p in env_files):
        raise ValueError("service uses a custom environment layout; migrate it explicitly first")
    # Match path boundaries: do not rewrite a different path sharing a prefix.
    return re.sub(re.escape(str(current)) + r"(?=/|\s|$|[\"'])", lambda _: str(target), text)


def validate(root: Path, user: str, destination: Path,
             service_file: Path = Path("/etc/systemd/system/cc-remote-wrapper.service")) -> Path:
    if (not root.is_absolute() or root.resolve(strict=True) != root
            or re.search(r"[\s\"'\\\x00-\x1f]", str(root)) or root == destination
            or root.is_relative_to(destination) or destination.is_relative_to(root)):
        raise ValueError("legacy root must be a separate canonical absolute directory")
    if (destination / "current").exists() or (destination / "installation.json").exists():
        raise ValueError("managed destination is already installed; migration would overwrite it")
    for path in (root, root / "releases", service_file):
        root_owned(path)
    current = root / "current"
    old = current.resolve(strict=True)
    if not current.is_symlink() or old.parent != root / "releases":
        raise ValueError("legacy current must point inside its immutable releases")
    root_owned(old)
    if not (old / "release-manifest.json").is_file():
        raise ValueError("legacy release manifest is required for compatibility checks")
    rebase_service(service_file.read_text(), current, destination / "current", user)
    output = subprocess.check_output([
        "systemctl", "show", "cc-remote-wrapper", "-p", "User", "-p", "WorkingDirectory",
        "-p", "FragmentPath", "-p", "DropInPaths",
    ], text=True, timeout=15)
    live = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    if (live.get("User") != user or live.get("WorkingDirectory") != str(current)
            or live.get("FragmentPath") != str(service_file) or live.get("DropInPaths")):
        raise ValueError("effective service differs or has drop-ins; explicit reconciliation required")
    return old


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--user", required=True)
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise ValueError("Linux migration requires the installation administrator")
        print(validate(args.root, args.user, args.destination))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Migration refused: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
