"""Pair this machine with a cc-remote relay.

Usage:
    python -m cc_remote.device pair https://remote.example PAIR-CODE
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from cc_remote.config import device_config_path
from cc_remote.wrapper.os_compat import fchmod


def _origin(value: str) -> str:
    parsed = urlsplit(value.strip().rstrip("/"))
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("relay must be an http(s) origin without a path")
    loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    if parsed.scheme != "https" and not loopback:
        raise ValueError("relay must use https except on loopback")
    return value.strip().rstrip("/")


def _default_label() -> str:
    hostname = socket.gethostname().split(".", 1)[0].strip()
    return hostname[:64] or f"{platform.system()} device"


def _write_private_text(path: Path, content: str, *, replace: bool) -> None:
    path = path.expanduser()
    if path.exists() and not replace:
        raise FileExistsError(
            f"device config already exists: {path} (pass --replace to replace it)")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, staged_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    staged = Path(staged_name)
    try:
        fchmod(fd, staged, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        staged.unlink(missing_ok=True)
        raise


def _write_private_json(path: Path, payload: dict[str, str], *, replace: bool) -> None:
    _write_private_text(
        path,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        replace=replace,
    )


def pair(args: argparse.Namespace) -> int:
    origin = _origin(args.relay)
    label = (args.name or _default_label()).strip()
    if not label or len(label) > 64 or any(ord(char) < 32 for char in label):
        raise ValueError("device name must be 1-64 printable characters")
    response = httpx.post(
        f"{origin}/api/devices/pair",
        json={
            "code": args.code,
            "label": label,
            "platform": platform.system().lower()[:32] or "unknown",
            "hostname": socket.gethostname()[:255] or "unknown",
        },
        timeout=20,
        follow_redirects=False,
    )
    try:
        result = response.json()
    except Exception as exc:
        raise RuntimeError(f"relay returned HTTP {response.status_code}") from exc
    if response.status_code != 200 or not isinstance(result, dict) or not result.get("ok"):
        error = result.get("error", "pairing_failed") if isinstance(result, dict) else "pairing_failed"
        raise RuntimeError(f"pairing failed: {error}")
    required = ("relay_url", "token", "machine_id", "label")
    if any(not isinstance(result.get(key), str) or not result[key] for key in required):
        raise RuntimeError("relay returned an invalid device credential")
    if args.env_file:
        target = Path(args.env_file).expanduser()
        _write_private_text(
            target,
            "\n".join((
                f"RELAY_URL={result['relay_url']}",
                f"WRAPPER_TOKEN={result['token']}",
                f"CC_REMOTE_MACHINE_ID={result['machine_id']}",
                "",
            )),
            replace=args.replace,
        )
    else:
        target = Path(args.config).expanduser() if args.config else device_config_path()
        _write_private_json(
            target,
            {
                "relay_url": result["relay_url"],
                "wrapper_token": result["token"],
                "machine_id": result["machine_id"],
                "label": result["label"],
            },
            replace=args.replace,
        )
    print(f"Paired {result['label']} as {result['machine_id']}.")
    print(f"Credential saved with mode 0600: {target}")
    print("Start or restart the cc-remote wrapper to connect this device.")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="cc-remote-device")
    commands = root.add_subparsers(dest="command", required=True)
    command = commands.add_parser("pair", help="pair this machine with a relay")
    command.add_argument("relay", help="relay origin, for example https://remote.example")
    command.add_argument("code", help="one-time code shown by the web device center")
    command.add_argument("--name", help="friendly device name")
    output = command.add_mutually_exclusive_group()
    output.add_argument("--config", help="JSON credential file path")
    output.add_argument(
        "--env-file",
        help="systemd EnvironmentFile path (run with sudo for /etc)",
    )
    command.add_argument("--replace", action="store_true", help="replace an existing credential")
    command.set_defaults(handler=pair)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        return int(args.handler(args))
    except (FileExistsError, OSError, RuntimeError, ValueError) as exc:
        print(f"cc-remote-device: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
