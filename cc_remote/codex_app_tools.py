"""Opt-in macOS Desktop MCP connection for an existing shared Codex host.

Discovery reads only process identity, the selected CODEX_HOME and the App's
own open startup log/socket. It never starts an App, daemon, or model turn.
The signed official Node runtime runs our stdio adapter and the official MCP;
the App's native peer authorization remains in force.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys
from urllib.parse import urlsplit

from cc_remote.wrapper.process_scan import (
    process_environment_value,
    process_identity,
    process_owner_uid,
)

_TEAM = "2DC432GLL2"
_MAX_LOG = 256 * 1024
_MAX_OUTPUT = 2 * 1024 * 1024
_PIPE = re.compile(
    r"(?m)^\d{4}-\d{2}-\d{2}T\S+ info \[dynamic-app-tools-native-pipe\] "
    r"dynamic_app_tools_listening pipePath=(/tmp/codex-browser-use/"
    r"[a-f0-9-]{36}\.sock)(?:\s|$)"
)


def _command(*args: str) -> str:
    result = subprocess.run(
        args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, timeout=4, check=False,
    )
    if result.returncode or len(result.stdout) > _MAX_OUTPUT:
        raise ValueError("process inspection failed")
    return result.stdout.decode("utf-8", errors="replace")


def _signed(path: Path) -> None:
    _command(
        "/usr/bin/codesign", "--verify", "--strict", "--test-requirement",
        f'=anchor apple generic and certificate leaf[subject.OU] = "{_TEAM}"',
        str(path),
    )


def app_paths(app: Path) -> tuple[Path, Path, Path, str]:
    app = app.resolve(strict=True)
    with (app / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    if info.get("CFBundleIdentifier") != "com.openai.codex":
        raise ValueError("not the official Desktop App bundle")
    executable = info.get("CFBundleExecutable", "")
    if not executable or Path(executable).name != executable:
        raise ValueError("invalid App executable")
    resources = app / "Contents/Resources"
    plugin = resources / "plugins/openai-bundled/plugins/codex-app-tools"
    node = resources / "cua_node/bin/node"
    if not node.is_file() or not (plugin / "server.mjs").is_file():
        raise ValueError("official App tools or signed Node runtime missing")
    return app / "Contents/MacOS" / executable, node, plugin, info["CFBundleIdentifier"]


def _private_socket(path: Path) -> os.stat_result:
    info = path.lstat()
    if (
        not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("socket is not private to this login user")
    return info


def _matching_app_pids(executable: Path) -> list[int]:
    found = []
    for line in _command("/bin/ps", "-axo", "pid=,uid=,comm=").splitlines():
        fields = line.strip().split(None, 2)
        if (
            len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit()
            and int(fields[1]) == os.getuid() and fields[2] == str(executable)
        ):
            found.append(int(fields[0]))
    return found


def _shared_app(identity, profile: Path) -> bool:
    complete, home = process_environment_value(identity, "CODEX_HOME")
    if not complete or not home or Path(home).resolve() != profile:
        return False
    complete, endpoint = process_environment_value(identity, "CODEX_APP_SERVER_WS_URL")
    if not complete or not endpoint:
        return False
    try:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "ws" or parsed.hostname != "127.0.0.1"
            or not parsed.port or parsed.username or parsed.password
            or parsed.query or parsed.fragment
        ):
            return False
        expected = f"->127.0.0.1:{parsed.port}"
        connections = _command(
            "/usr/sbin/lsof", "-nP", "-a", "-p", str(identity.pid),
            "-iTCP", "-sTCP:ESTABLISHED", "-Fn",
        )
        return any(
            line.startswith("n127.0.0.1:") and line.endswith(expected)
            for line in connections.splitlines()
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def _pipe_from_open_logs(pid: int, bundle_id: str) -> Path | None:
    # Never walk the user's home or scan session transcripts. The kernel gives
    # us only files already opened by this exact App process.
    files = _command("/usr/sbin/lsof", "-nP", "-p", str(pid), "-Fn")
    sockets = _command("/usr/sbin/lsof", "-nP", "-a", "-p", str(pid), "-U", "-Fn")
    owned = {line[1:] for line in sockets.splitlines() if line.startswith("n/")}
    log_root = Path.home() / "Library/Logs" / bundle_id
    candidates: set[Path] = set()
    inspected = 0
    for name in files.splitlines():
        if not name.startswith("n/"):
            continue
        path = Path(name[1:])
        if (
            not path.is_relative_to(log_root) or path.suffix != ".log"
            or f"-{pid}-t0-" not in path.name
        ):
            continue
        # Startup markers are near the beginning; cap both read size and count.
        if path.resolve() != path:
            continue
        inspected += 1
        if inspected > 8:
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022
            ):
                continue
            raw = os.read(fd, _MAX_LOG).decode("utf-8", errors="replace")
        finally:
            os.close(fd)
        for match in _PIPE.finditer(raw):
            candidate = Path(match[1])
            if str(candidate) in owned:
                _private_socket(candidate)
                candidates.add(candidate)
    if len(candidates) != 1:
        return None
    return candidates.pop()


def _manifest_hash(entry: dict) -> str:
    return hashlib.sha256(json.dumps(
        entry, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def _runtime_fingerprint(node: Path) -> str:
    info = node.stat()
    return f"{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}"


def discover(profile: Path, app: Path, expected_manifest: str | None = None) -> dict:
    """Fail closed on ambiguity, a private App backend, or another profile."""
    if sys.platform != "darwin":
        return {"state": "unavailable", "reason": "macos_required"}
    try:
        profile = profile.resolve(strict=True)
        _private_socket(profile / "app-server-control/app-server-control.sock")
        executable, node, plugin, bundle_id = app_paths(app)
        if expected_manifest is not None:
            entry = json.loads((plugin / "desktop-mcp.json").read_text())["mcpServers"]["codex_app"]
            if _manifest_hash(entry) != expected_manifest:
                return {"state": "unavailable", "reason": "official_policy_changed"}
        matches = []
        for pid in _matching_app_pids(executable):
            identity = process_identity(pid)
            if identity is None or not _shared_app(identity, profile):
                continue
            pipe = _pipe_from_open_logs(pid, bundle_id)
            if pipe is None:
                continue
            runtime = _runtime_fingerprint(node)
            _signed(app.resolve())
            _signed(node)
            if _runtime_fingerprint(node) != runtime:
                continue  # An updater replaced the runtime during validation.
            socket = _private_socket(pipe)
            if process_owner_uid(pid) != os.getuid() or process_identity(pid) != identity:
                continue
            matches.append({
                "state": "ready", "pid": pid,
                "generation": f"{pid}:{identity.start_ticks}:{socket.st_ino}:{socket.st_ctime_ns}:{runtime}",
                "pipe": str(pipe), "node": str(node),
                "script": str(plugin / "server.mjs"), "cwd": str(plugin),
            })
        if len(matches) == 1:
            return matches[0]
        return {"state": "unavailable", "reason": "no_unique_shared_app"}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, plistlib.InvalidFileException):
        return {"state": "unavailable", "reason": "app_identity_unavailable"}


def mcp_config(profile: Path, app: Path) -> dict:
    """Use official policy verbatim; this only replaces the launch transport."""
    _, node, plugin, _ = app_paths(app)
    _signed(app.resolve())
    _signed(node)
    entry = json.loads((plugin / "desktop-mcp.json").read_text())["mcpServers"]["codex_app"]
    manifest_hash = _manifest_hash(entry)
    entry.update({
        "command": str(node),
        "args": [
            str(Path(__file__).with_suffix(".mjs")),
            "--python", sys.executable, "--profile", str(profile.resolve(strict=True)),
            "--app", str(app.resolve(strict=True)),
            "--manifest-sha256", manifest_hash,
        ],
        "cwd": str(Path(__file__).resolve().parent.parent),
        "required": False,
        "startup_timeout_sec": 15,
    })
    # This adapter discovers the pipe rather than inheriting a stale per-launch
    # environment. Keep every other official environment/policy entry intact.
    entry["env_vars"] = [
        key for key in entry.get("env_vars", []) if key != "CODEX_APP_TOOLS_PIPE_PATH"
    ]
    return entry


def _toml_value(value) -> str:
    if isinstance(value, dict):
        return "{ " + ", ".join(
            f"{json.dumps(key)} = {_toml_value(item)}" for key, item in value.items()
        ) + " }"
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, (str, bool, int, float)):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    raise ValueError("unsupported official MCP config value")


def config_toml(entry: dict) -> str:
    return "[mcp_servers.codex_app]\n" + "\n".join(
        f"{json.dumps(key)} = {_toml_value(value)}" for key, value in entry.items()
    ) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["discover", "config"])
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--format", choices=["json", "toml"], default="json")
    parser.add_argument("--manifest-sha256")
    args = parser.parse_args()
    result = (
        discover(args.profile, args.app, args.manifest_sha256) if args.action == "discover"
        else mcp_config(args.profile, args.app)
    )
    if args.format == "toml":
        if args.action != "config":
            parser.error("TOML output is only supported for config")
        print(config_toml(result), end="")
    else:
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
