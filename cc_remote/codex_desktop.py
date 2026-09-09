"""Opt-in macOS shared Desktop launcher; never manages the official daemon.

The only network listener is loopback, restricted to the selected official App
process. JSON-RPC bytes pass through unchanged to the profile's private socket.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import select
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
import uuid

from websockets.asyncio.client import connect

from cc_remote import codex_app_tools as desktop
from cc_remote.wrapper.process_scan import (
    ProcessIdentity,
    process_command,
    process_environment_value,
    process_identity,
    process_owner_uid,
)

_REPO = Path(__file__).resolve().parent.parent
_START_TIMEOUT = 40
_FORBIDDEN = b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"


class LaunchError(Exception):
    """A safe, user-facing error without RPC payloads or credentials."""


def state_root() -> Path:
    return Path.home() / "Library/Application Support/cc-remote/desktop-launcher"


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise LaunchError("共享入口的状态目录不是当前用户的私有目录。")


def daemon_socket(profile: Path) -> Path:
    path = profile / "app-server-control/app-server-control.sock"
    try:
        if path.resolve(strict=True) != path:
            raise ValueError("symlink")
        desktop._private_socket(path)
    except (OSError, ValueError):
        raise LaunchError("共享 daemon 尚未就绪。请先启动该账号的 cc-remote Wrapper，再点共享入口。") from None
    return path


async def command(*args: str) -> str:
    return await asyncio.to_thread(desktop._command, *args)


def app_identity(executable: Path) -> ProcessIdentity | None:
    pids = desktop._matching_app_pids(executable)
    if not pids:
        return None
    if len(pids) != 1 or (identity := process_identity(pids[0])) is None:
        raise LaunchError("检测到多个或正在切换的 Codex App 进程；未改动它们，请稍后重试。")
    return identity


def matches_launch(identity: ProcessIdentity, profile: Path, endpoint: str) -> bool:
    home_ok, home = process_environment_value(identity, "CODEX_HOME")
    url_ok, url = process_environment_value(identity, "CODEX_APP_SERVER_WS_URL")
    return bool(
        home_ok and home and Path(home).resolve() == profile
        and url_ok and url == endpoint and process_identity(identity.pid) == identity
        and process_owner_uid(identity.pid) == os.getuid()
    )


async def reuse_app(app: Path, executable: Path, profile: Path) -> ProcessIdentity | None:
    identity = await asyncio.to_thread(app_identity, executable)
    if identity is None:
        return None
    shared = await asyncio.to_thread(desktop._shared_app, identity, profile)
    _, endpoint = process_environment_value(identity, "CODEX_APP_SERVER_WS_URL")
    try:
        parsed = urlsplit(endpoint or "")
        connecting = bool(
            parsed.scheme == "ws" and parsed.hostname == "127.0.0.1" and parsed.port
            and parsed.path == "/rpc" and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment
            and matches_launch(identity, profile, endpoint)
        )
    except ValueError:
        connecting = False
    # LaunchServices can expose the process before its first WebSocket exists.
    # Repeated clicks during that small window must not mislabel it as private.
    if not shared and connecting:
        for _ in range(24):
            await asyncio.sleep(0.25)
            shared = await asyncio.to_thread(desktop._shared_app, identity, profile)
            if shared or process_identity(identity.pid) != identity:
                break
    if process_identity(identity.pid) != identity:
        return None  # It finished quitting while we checked; a fresh launch is safe.
    if not shared:
        raise LaunchError(
            "Codex App 已打开，但不是该账号已连接的共享模式。"
            "请先在 App 中完全退出（⌘Q），再点击 Codex Shared；当前会话未被终止。"
        )
    # Only focus the existing process. Never use -n, kill, or override its env.
    await command("/usr/bin/open", "-a", str(app))
    return identity


@asynccontextmanager
async def launch_lock(app: Path, root: Path):
    """One launcher per official bundle, including competing account profiles.

    Keep the inode permanently: unlinking a flock file permits split locks.
    The kernel releases it on exit, so stale PIDs never need killing.
    """
    private_directory(root)
    key = hashlib.sha256(os.fsencode(app)).hexdigest()[:24]
    fd = os.open(root / f"{key}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    acquired = False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise LaunchError("共享启动锁不安全；未启动 App。")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        os.close(fd)


def valid_upgrade(header: bytes, port: int) -> bool:
    try:
        if len(header) > 16384 or not header.endswith(b"\r\n\r\n"):
            return False
        lines = header.decode("ascii").split("\r\n")
        fields = {}
        for line in lines[1:-2]:
            key, value = line.split(":", 1)
            if not key or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for c in key):
                return False
            key = key.lower()
            if key in fields:
                return False
            fields[key] = value.strip()
        return bool(
            lines[0] == "GET /rpc HTTP/1.1"
            and fields.get("host") == f"127.0.0.1:{port}"
            and "origin" not in fields and "transfer-encoding" not in fields
            and fields.get("content-length", "0") == "0"
            and fields.get("upgrade", "").lower() == "websocket"
            and "upgrade" in [v.strip().lower() for v in fields.get("connection", "").split(",")]
        )
    except (ValueError, UnicodeError):
        return False


class Bridge:
    def __init__(self, profile: Path, executable: Path):
        self.profile = profile
        self.executable = executable
        self.port = 0
        self.active: set[asyncio.Task] = set()
        self.app_connected = asyncio.Event()
        self.preflighting = False

    @property
    def endpoint(self) -> str:
        return f"ws://127.0.0.1:{self.port}/rpc"

    async def peer_identity(self, peer_port: int) -> ProcessIdentity | None:
        output = await command(
            "/usr/sbin/lsof", "-nP", "-a", f"-iTCP:{peer_port}",
            "-sTCP:ESTABLISHED", "-Fpun",
        )
        pid, uid = None, None
        expected = f"n127.0.0.1:{peer_port}->127.0.0.1:{self.port}"
        for line in output.splitlines():
            if line.startswith("p"):
                pid, uid = int(line[1:]), None
            elif line.startswith("u"):
                uid = int(line[1:])
            elif line == expected and uid == os.getuid() and pid is not None:
                identity = process_identity(pid)
                if identity is None:
                    return None
                if pid == os.getpid() and self.preflighting:
                    return identity
                argv = process_command(identity)
                if (
                    argv and os.fsdecode(argv[0]) == str(self.executable)
                    and matches_launch(identity, self.profile, self.endpoint)
                ):
                    return identity
        return None

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self.active.add(task)
        upstream = None
        pumps = []
        try:
            if len(self.active) > 12:
                return
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            peer = writer.get_extra_info("peername")
            identity = (
                await self.peer_identity(peer[1])
                if peer and peer[0] == "127.0.0.1" and valid_upgrade(header, self.port) else None
            )
            if identity is None or process_identity(identity.pid) != identity:
                writer.write(_FORBIDDEN)
                await writer.drain()
                return
            source, upstream = await asyncio.wait_for(
                asyncio.open_unix_connection(str(daemon_socket(self.profile))), 5,
            )
            if process_identity(identity.pid) != identity:
                return
            upstream.write(header)
            await upstream.drain()
            if identity.pid != os.getpid():
                self.app_connected.set()

            async def pump(src, dst):
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()

            pumps = [asyncio.create_task(pump(reader, upstream)), asyncio.create_task(pump(source, writer))]
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        except (OSError, ValueError, UnicodeError, TimeoutError, LaunchError,
                subprocess.SubprocessError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            # Deliberately no RPC, headers, environment or transcript logging.
            pass
        finally:
            for pending in pumps:
                pending.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            for stream in (upstream, writer):
                if stream:
                    stream.close()
                    try:
                        await asyncio.wait_for(stream.wait_closed(), 2)
                    except (OSError, TimeoutError):
                        pass
            self.active.discard(task)

    async def close(self) -> None:
        tasks = list(self.active)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def preflight(self) -> None:
        self.preflighting = True
        try:
            async with asyncio.timeout(15):
                async with connect(
                    self.endpoint, compression=None, proxy=None,
                    user_agent_header=None, open_timeout=8, close_timeout=1, max_size=2**20,
                ) as ws:
                    await ws.send(json.dumps({"id": 1, "method": "initialize", "params": {
                        "clientInfo": {"name": "cc-remote-desktop-launcher", "version": "1.0"},
                        "capabilities": {"experimentalApi": True},
                    }}))
                    while True:
                        message = json.loads(await ws.recv())
                        if message.get("id") == 1:
                            if "error" in message or "result" not in message:
                                raise LaunchError("共享 daemon 握手失败；未启动独立服务。")
                            break
                    await ws.send(json.dumps({"method": "initialized"}))
        finally:
            self.preflighting = False


async def supervise(profile: Path, app: Path, root: Path, ready) -> None:
    profile, app = profile.resolve(strict=True), app.resolve(strict=True)
    executable, _, _, _ = desktop.app_paths(app)
    await asyncio.to_thread(desktop._signed, app)
    daemon_socket(profile)
    if await reuse_app(app, executable, profile):
        ready({"ok": True, "state": "reused"})
        return
    deadline = asyncio.get_running_loop().time() + _START_TIMEOUT
    while asyncio.get_running_loop().time() < deadline:
        async with launch_lock(app, root) as acquired:
            if acquired:
                await start_app(profile, app, executable, ready)
                return
            if await reuse_app(app, executable, profile):
                ready({"ok": True, "state": "reused"})
                return
        # The previous App may have just quit, before its supervisor releases
        # the lock. Retry acquisition as well as checking for a new App.
        await asyncio.sleep(0.25)
    raise LaunchError("共享入口仍在切换，请稍后重试；现有 App 和 daemon 未被重启。")


async def start_app(profile: Path, app: Path, executable: Path, ready) -> None:
    """Called only while holding the bundle's launch lock for App lifetime."""
    if await reuse_app(app, executable, profile):
        ready({"ok": True, "state": "reused"})
        return
    bridge = Bridge(profile, executable)
    try:
        async with await asyncio.start_server(bridge.handle, "127.0.0.1", 0, limit=16384) as server:
            bridge.port = server.sockets[0].getsockname()[1]
            await bridge.preflight()
            if await reuse_app(app, executable, profile):
                ready({"ok": True, "state": "reused"})
                return
            await command(
                "/usr/bin/open", "-a", str(app),
                "--env", f"CODEX_APP_SERVER_WS_URL={bridge.endpoint}",
                "--env", f"CODEX_HOME={profile}",
                "--env", "CODEX_APP_SERVER_FORCE_CLI=0",
            )
            async with asyncio.timeout(_START_TIMEOUT):
                while (identity := await asyncio.to_thread(app_identity, executable)) is None:
                    await asyncio.sleep(0.25)
            if not matches_launch(identity, profile, bridge.endpoint):
                raise LaunchError("另一启动方式抢先打开了 App。请完全退出 App 后，从共享入口重开。")
            try:
                await asyncio.wait_for(bridge.app_connected.wait(), _START_TIMEOUT)
                ready({"ok": True, "state": "connected"})
            except TimeoutError:
                # Do not strand an App that is merely slow to initialize.
                ready({"ok": False, "message": "App 已启动，但共享连接尚未就绪。请稍后重试共享入口。"})
            while process_identity(identity.pid) == identity:
                await asyncio.sleep(1)
    finally:
        await bridge.close()


def _report(fd: int, result: dict) -> None:
    try:
        os.write(fd, json.dumps(result, ensure_ascii=False).encode() + b"\n")
    except BrokenPipeError:
        pass
    finally:
        os.close(fd)


def launch(profile: Path, app: Path, root: Path) -> dict:
    """Detach only our supervisor; return readiness to a Dock/Finder click."""
    read_fd, write_fd = os.pipe()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "cc_remote.codex_desktop", "run",
             "--profile", str(profile), "--app", str(app), "--state-dir", str(root),
             "--ready-fd", str(write_fd)],
            cwd=_REPO, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, pass_fds=(write_fd,), start_new_session=True,
        )
    except BaseException:
        os.close(read_fd)
        raise
    finally:
        os.close(write_fd)
    try:
        if not select.select([read_fd], [], [], 60)[0]:
            return {"ok": False, "message": "共享入口仍在启动，请稍后再点；没有重启任何现有服务。"}
        raw = os.read(read_fd, 8192)
        proc.poll()  # Reap an already-finished reuse/error worker.
        return json.loads(raw) if raw else {"ok": False, "message": "共享入口未能启动，请检查运行环境。"}
    finally:
        os.close(read_fd)


def install(profile: Path, app: Path, target: Path) -> dict:
    """Build a separate local app. Never write to the signed official bundle."""
    profile, app = profile.resolve(strict=True), app.resolve(strict=True)
    desktop.app_paths(app)
    desktop._signed(app)
    daemon_socket(profile)
    target = target.absolute()
    if target.suffix != ".app" or target.exists() or target.is_symlink():
        raise LaunchError("安装目标必须是尚不存在的 .app；不会覆盖已有应用。")
    target.parent.mkdir(parents=True, exist_ok=True)
    root = state_root()
    private_directory(root)
    stage = Path(tempfile.mkdtemp(prefix=".codex-shared-", dir=target.parent))
    try:
        contents = stage / "Contents"
        (contents / "MacOS").mkdir(parents=True)
        resources = contents / "Resources"
        resources.mkdir()
        config = {"python": sys.executable, "cwd": str(_REPO), "app": str(app),
                  "profile": str(profile), "state_dir": str(root)}
        (resources / "launcher.json").write_text(json.dumps(config), encoding="utf-8")
        with (app / "Contents/Info.plist").open("rb") as stream:
            original = plistlib.load(stream)
        icon = original.get("CFBundleIconFile", "")
        if not icon or Path(icon).name != icon:
            raise LaunchError("未找到官方 App 图标。")
        if not icon.endswith(".icns"):
            icon += ".icns"
        shutil.copyfile(app / "Contents/Resources" / icon, resources / "Shared.icns")
        identifier = "local.cc-remote.codex-shared." + hashlib.sha256(os.fsencode(profile)).hexdigest()[:12]
        info = {"CFBundleIdentifier": identifier, "CFBundleName": target.stem,
                "CFBundleDisplayName": target.stem, "CFBundleExecutable": "CodexSharedLauncher",
                "CFBundlePackageType": "APPL", "CFBundleVersion": "1",
                "CFBundleShortVersionString": "1.0", "CFBundleIconFile": "Shared.icns",
                "LSUIElement": True, "NSHighResolutionCapable": True}
        (contents / "Info.plist").write_bytes(plistlib.dumps(info))
        subprocess.run(
            ["/usr/bin/swiftc", str(Path(__file__).with_suffix(".swift")), "-o",
             str(contents / "MacOS/CodexSharedLauncher")], check=True, timeout=120,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(["/usr/bin/codesign", "--sign", "-", str(stage)], check=True, timeout=20)
        # The unique stage is owned by this invocation, never an existing app.
        os.rename(stage, target)
        return {"ok": True, "application": str(target)}
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def pin_dock(target: Path) -> dict:
    target = target.resolve(strict=True)
    with (target / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    if not info.get("CFBundleIdentifier", "").startswith("local.cc-remote.codex-shared."):
        raise LaunchError("只能固定共享启动器；未修改 Dock。")
    raw = subprocess.check_output(["/usr/bin/defaults", "export", "com.apple.dock", "-"], timeout=5)
    entries = plistlib.loads(raw).get("persistent-apps", [])
    url = target.as_uri() + "/"
    if any(e.get("tile-data", {}).get("file-data", {}).get("_CFURLString") == url for e in entries):
        return {"ok": True, "state": "already_pinned"}
    root = state_root()
    private_directory(root)
    backup = root / f"dock-apps-before-{uuid.uuid4().hex}.plist"
    backup.write_bytes(plistlib.dumps(entries))
    backup.chmod(0o600)
    tile = {"tile-type": "file-tile", "tile-data": {
        "file-data": {"_CFURLString": url, "_CFURLStringType": 15},
        "file-label": target.stem, "bundle-identifier": info["CFBundleIdentifier"],
        "file-type": 41,
    }}
    subprocess.run(
        ["/usr/bin/defaults", "write", "com.apple.dock", "persistent-apps", "-array-add",
         plistlib.dumps(tile).decode()], check=True, timeout=5,
    )
    return {"ok": True, "state": "pinned", "backup": str(backup)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["launch", "run", "install", "pin-dock"])
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--app", type=Path)
    parser.add_argument("--target", type=Path)
    parser.add_argument("--state-dir", type=Path, default=state_root())
    parser.add_argument("--ready-fd", type=int)
    args = parser.parse_args()
    if args.action != "pin-dock" and (not args.profile or not args.app):
        parser.error("--profile and --app are required")
    if args.action in {"install", "pin-dock"} and not args.target:
        parser.error("--target is required")
    sent = False

    def report(result):
        nonlocal sent
        if not sent:
            sent = True
            if args.ready_fd is not None:
                _report(args.ready_fd, result)
            else:
                print(json.dumps(result, ensure_ascii=False), flush=True)

    try:
        if sys.platform != "darwin":
            raise LaunchError("共享 Desktop 入口目前仅支持 macOS。")
        if args.action == "run":
            asyncio.run(supervise(args.profile, args.app, args.state_dir, report))
        elif args.action == "launch":
            result = launch(args.profile, args.app, args.state_dir)
            report(result)
            if not result.get("ok"):
                sys.exit(1)
        elif args.action == "install":
            report(install(args.profile, args.app, args.target))
        else:
            report(pin_dock(args.target))
    except Exception as exc:
        report({"ok": False, "message": str(exc) if isinstance(exc, LaunchError)
                else "共享入口检查失败；未启动独立服务，也未重启现有 App。", "kind": type(exc).__name__})
        sys.exit(1)


if __name__ == "__main__":
    main()
