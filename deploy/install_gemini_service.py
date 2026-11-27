"""Opt-in installation and idle upgrades for the persistent Gemini service."""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from tempfile import NamedTemporaryFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cc_remote.antigravity_service import GEMINI_SWITCH_FENCE, service_control
from cc_remote.claude_service.wire import private_directory
from cc_remote.config import valid_machine_id
from cc_remote.update_jobs import read_json, write_json

LABEL = "com.muggle.cc-remote.gemini-service"
UNIT = "cc-remote-gemini-service.service"


def systemd_arg(value, *, command=True):
    quoted = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return '"' + (quoted.replace("$", "$$") if command else quoted) + '"'


def render_unit(platform, source, state_dir, argv, env, *, label=LABEL):
    if platform == "darwin":
        return plistlib.dumps({
            "Label": label, "ProgramArguments": argv, "EnvironmentVariables": env,
            "WorkingDirectory": str(source), "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False}, "Umask": 0o077,
            "StandardOutPath": str(state_dir / "stdout.log"),
            "StandardErrorPath": str(state_dir / "stderr.log"),
        })
    if platform != "linux":
        raise ValueError("Gemini service requires macOS or Linux")
    # WorkingDirectory is a single path, unlike the ExecStart arguments.
    workdir = str(source).replace("\\", "\\\\").replace("%", "%%")
    environment = " ".join(systemd_arg(f"{key}={value}", command=False) for key, value in env.items())
    return ("[Unit]\nDescription=cc-remote persistent Gemini Code sessions\n\n"
            "[Service]\nType=simple\n"
            f"WorkingDirectory={workdir}\nExecStart={' '.join(map(systemd_arg, argv))}\n"
            f"Environment={environment}\n"
            "UMask=0077\nRestart=on-failure\nRestartSec=3\nKillMode=control-group\n\n"
            "[Install]\nWantedBy=default.target\n").encode()


def unit_destination(wrapper_state):
    suffix = hashlib.sha256(os.fsencode(wrapper_state.resolve())).hexdigest()[:16]
    if sys.platform == "darwin":
        return Path.home() / "Library/LaunchAgents" / f"{LABEL}.{suffix}.plist"
    if sys.platform == "linux":
        return Path.home() / ".config/systemd/user" / f"{UNIT.removesuffix('.service')}-{suffix}.service"
    raise ValueError("Gemini service requires macOS or Linux")


def manage_unit(destination, *, start):
    if sys.platform == "darwin":
        domain = f"gui/{os.getuid()}"
        target = f"{domain}/{destination.stem}"

        def loaded():
            return subprocess.run(["launchctl", "print", target], stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=5).returncode == 0

        if start:
            command = (["launchctl", "kickstart", target] if loaded()
                       else ["launchctl", "bootstrap", domain, str(destination)])
            subprocess.run(command, check=True, timeout=30)
        elif loaded():
            subprocess.run(["launchctl", "bootout", target], check=True, timeout=30)
            deadline = time.monotonic() + 15
            while loaded():
                if time.monotonic() >= deadline:
                    raise TimeoutError("Gemini LaunchAgent did not finish unloading")
                time.sleep(.1)
        return
    if start:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=15)
    command = (["systemctl", "--user", "enable", "--now", destination.name] if start
               else ["systemctl", "--user", "stop", destination.name])
    subprocess.run(command, check=True, timeout=30)


def read_unit(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 65536):
            raise ValueError("existing Gemini service definition is not a private user-owned file")
        return stream.read(65536)


def write_unit(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, prefix=".gemini-service-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


async def wait_ready(socket, machine, wrapper_state, release):
    async with asyncio.timeout(16):
        while True:
            try:
                async with service_control(socket, machine, "status") as status:
                    if status.get("state_dir") != str(wrapper_state):
                        raise ValueError("Gemini service belongs to another Wrapper")
                    if release is not None and status.get("release") != str(release):
                        raise ValueError("Gemini service is running a different release")
                    if status.get("ready") is True:
                        return
            except (FileNotFoundError, ConnectionRefusedError, asyncio.IncompleteReadError):
                pass
            await asyncio.sleep(.25)


async def install_unit(destination, definition, state_dir, wrapper_state, machine, release, *, upgrade=False):
    socket = state_dir / "service.sock"
    fence = state_dir / GEMINI_SWITCH_FENCE
    registration = wrapper_state / "gemini-service.json"
    binding = {"socket": str(socket), "machine": machine}
    try:
        registered = read_json(registration, os.getuid(), limit=4096)
    except FileNotFoundError:
        registered = None
    if registered is not None and registered != binding:
        raise ValueError("existing Gemini registration belongs to a different service")
    previous = read_unit(destination)
    if previous is None:
        # A manually started worker already owns this state directory.
        if socket.exists() or socket.is_symlink():
            raise ValueError("Gemini socket already exists; inspect the existing service before installing")
        write_json(fence, {"release": str(release)}, mode=0o600)
        write_unit(destination, definition)
        try:
            await asyncio.to_thread(manage_unit, destination, start=True)
            await wait_ready(socket, machine, wrapper_state, release)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            # This unit is fenced and has no Wrapper registration. Keep the
            # definition for a retry, but stop any supervisor restart loop.
            await asyncio.to_thread(manage_unit, destination, start=False)
            raise
    elif previous != definition:
        if not upgrade or registered != binding:
            raise ValueError("existing Gemini service definition differs; use --upgrade on its registered service")
        async with service_control(socket, machine, "drain") as reply:
            if reply.get("draining") is not True:
                raise RuntimeError("Gemini is busy; its existing service is still running")
            if reply.get("state_dir") != str(wrapper_state):
                raise ValueError("Gemini service belongs to another Wrapper")
            # Retain the previous immutable launch definition for recovery if
            # the OS start result or the new worker's ownership is uncertain.
            write_unit(state_dir / "previous-unit", previous)
            await asyncio.to_thread(manage_unit, destination, start=False)
            try:
                write_json(fence, {"release": str(release)}, mode=0o600)
                write_unit(destination, definition)
                await asyncio.to_thread(manage_unit, destination, start=True)
                await wait_ready(socket, machine, wrapper_state, release)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                # Any replacement using this state directory is fenced until
                # readiness and registration are confirmed.
                await asyncio.to_thread(manage_unit, destination, start=False)
                write_unit(destination, previous)
                await asyncio.to_thread(manage_unit, destination, start=True)
                await wait_ready(socket, machine, wrapper_state, None)
                fence.unlink(missing_ok=True)
                raise
    else:
        # Recover a staged install interrupted before its readiness check.
        if fence.exists() and not socket.exists():
            await asyncio.to_thread(manage_unit, destination, start=True)
        await wait_ready(socket, machine, wrapper_state, release)
    if registered is None:
        write_json(registration, binding, mode=0o600)
    fence.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--wrapper-state-dir", type=Path, required=True)
    parser.add_argument("--machine-id", required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--agy", type=Path)
    parser.add_argument("--upgrade", action="store_true")
    args = parser.parse_args()
    if os.getuid() == 0 or not valid_machine_id(args.machine_id):
        parser.error("run as the Wrapper user with its exact device ID")
    source = Path(__file__).resolve().parents[1]
    state = args.state_dir.expanduser().absolute()
    wrapper = args.wrapper_state_dir.expanduser().resolve()
    cwd = args.cwd.expanduser().resolve(strict=True)
    configured = os.environ.get("CC_REMOTE_ANTIGRAVITY_BIN")
    agy = args.agy or Path(configured or shutil.which("agy") or Path.home() / ".local/bin/agy")
    agy = agy.expanduser().absolute()
    if state.resolve() == wrapper or not cwd.is_dir() or not agy.is_file() or not os.access(agy, os.X_OK):
        parser.error("Gemini requires a separate state directory, the Wrapper working directory and an executable agy")
    if len(os.fsencode(state / "service.sock")) > 103:
        parser.error("Gemini service socket path is too long")
    interpreter = Path(sys.executable)
    argv = [str(interpreter.parent.resolve() / interpreter.name), "-m", "cc_remote.antigravity_service",
            "--state-dir", str(state), "--wrapper-state-dir", str(wrapper),
            "--machine-id", args.machine_id, "--cwd", str(cwd)]
    if any(any(char in value for char in "\0\n\r") for value in [*argv, str(source), str(agy)]):
        parser.error("service paths must not contain line breaks or NUL bytes")
    env = {"CC_REMOTE_ANTIGRAVITY": "1", "CC_REMOTE_ANTIGRAVITY_BIN": str(agy)}
    private_directory(state)
    private_directory(wrapper)
    # Serialize installers without taking the worker's own lifetime lock.
    fd = os.open(wrapper / "gemini-service-install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        destination = unit_destination(wrapper)
        definition = render_unit(sys.platform, source, state, argv, env, label=destination.stem)
        asyncio.run(install_unit(destination, definition,
                                 state, wrapper, args.machine_id, source, upgrade=args.upgrade))
    finally:
        os.close(fd)
    print(f"Gemini service ready: {state / 'service.sock'}")


if __name__ == "__main__":
    main()
