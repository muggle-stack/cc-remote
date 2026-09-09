"""No model calls or real App lifecycle changes: desktop launch regressions."""
import asyncio
import json
import os
from pathlib import Path
import plistlib
import tempfile

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import unix_serve

from cc_remote import codex_desktop as launcher
from cc_remote.wrapper.process_scan import ProcessIdentity


def upgrade(port=1234, extra="", path="/rpc", host=None):
    return (f"GET {path} HTTP/1.1\r\nHost: {host or f'127.0.0.1:{port}'}\r\n"
            f"Upgrade: websocket\r\nConnection: keep-alive, Upgrade\r\n{extra}\r\n").encode()


@pytest.mark.parametrize("header,allowed", [
    (upgrade(), True),
    (upgrade(extra="Origin: https://example.test\r\n"), False),
    (upgrade(extra="oRiGiN:\r\n"), False),
    (upgrade(host="localhost:1234"), False),
    (upgrade(host="evil.test:1234"), False),
    (upgrade(extra="Host: 127.0.0.1:1234\r\n"), False),
    (upgrade(path="/rpc?token=x"), False),
    (upgrade(path="/other"), False),
    (upgrade(extra="Transfer-Encoding: chunked\r\n"), False),
    (upgrade(extra="Content-Length: 1\r\n"), False),
    (upgrade(extra=" Origin: https://example.test\r\n"), False),
    (upgrade(extra="X-Test: " + "a" * 16384 + "\r\n"), False),
    (b"garbage\r\n\r\n", False),
])
def test_upgrade_is_loopback_only_and_not_a_browser_proxy(header, allowed):
    assert launcher.valid_upgrade(header, 1234) is allowed


@pytest.mark.asyncio
async def test_lock_survives_stale_inode_and_serializes_profiles(tmp_path):
    app = tmp_path / "Official.app"
    root = tmp_path / "state"
    async with launcher.launch_lock(app, root) as first:
        assert first
        async with launcher.launch_lock(app, root) as second:
            assert not second
    inode = next(root.iterdir()).stat().st_ino
    async with launcher.launch_lock(app, root) as third:
        assert third
        assert next(root.iterdir()).stat().st_ino == inode


@pytest.mark.asyncio
async def test_lock_rejects_symlink_and_writable_file(tmp_path):
    app = tmp_path / "Official.app"
    root = tmp_path / "state"
    async with launcher.launch_lock(app, root):
        pass
    lock = next(root.iterdir())
    lock.chmod(0o666)
    with pytest.raises(launcher.LaunchError):
        async with launcher.launch_lock(app, root):
            pass
    lock.unlink()
    lock.symlink_to(tmp_path / "unrelated")
    with pytest.raises(OSError):
        async with launcher.launch_lock(app, root):
            pass


@pytest.mark.parametrize("uid,exe,home,url,reused,expected", [
    ("self", "app", "same", "same", False, True),
    ("other", "app", "same", "same", False, False),
    ("self", "other", "same", "same", False, False),
    ("self", "app", "other", "same", False, False),
    ("self", "app", "same", "other", False, False),
    ("self", "app", "same", "same", True, False),
])
@pytest.mark.asyncio
async def test_bridge_peer_requires_exact_app_profile_and_process(
    monkeypatch, tmp_path, uid, exe, home, url, reused, expected,
):
    executable = tmp_path / "App"
    bridge = launcher.Bridge(tmp_path, executable)
    bridge.port = 1234
    identity = ProcessIdentity(42, 100)
    owner = os.getuid() if uid == "self" else os.getuid() + 1

    async def command(*_args):
        return f"p42\nu{owner}\nn127.0.0.1:4567->127.0.0.1:1234\n"

    monkeypatch.setattr(launcher, "command", command)
    monkeypatch.setattr(launcher, "process_owner_uid", lambda _pid: owner)
    calls = []

    def process_identity(_pid):
        calls.append(True)
        return ProcessIdentity(42, 200) if reused and len(calls) > 1 else identity

    monkeypatch.setattr(launcher, "process_identity", process_identity)
    monkeypatch.setattr(launcher, "process_command", lambda _id: (os.fsencode(executable if exe == "app" else "/other"),))
    env = {"CODEX_HOME": str(tmp_path if home == "same" else tmp_path / "other"),
           "CODEX_APP_SERVER_WS_URL": bridge.endpoint if url == "same" else "ws://127.0.0.1:5555/rpc"}
    monkeypatch.setattr(launcher, "process_environment_value", lambda _id, key: (True, env[key]))
    assert (await bridge.peer_identity(4567) is not None) is expected


@pytest.mark.asyncio
async def test_real_proxy_roundtrip_and_reject_self_after_preflight(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="cc-launch-", dir="/tmp") as root:
        profile = Path(root).resolve()
        directory = profile / "app-server-control"
        directory.mkdir(mode=0o700)
        path = directory / "app-server-control.sock"
        frames = []

        async def echo(ws):
            async for data in ws:
                frames.append(data)
                if json.loads(data).get("method") == "initialize":
                    await ws.send('{"id":1,"result":{}}')
                elif json.loads(data).get("method") != "initialized":
                    await ws.send(data)

        bridge = launcher.Bridge(profile, Path("/official/App"))
        # Cross-platform tests model the kernel's exact endpoint/PID lookup.
        # The authorization logic itself stays real.
        async def command(*args):
            peer_port = next(v.split(":")[1] for v in args if v.startswith("-iTCP:"))
            return f"p{os.getpid()}\nu{os.getuid()}\nn127.0.0.1:{peer_port}->127.0.0.1:{bridge.port}\n"

        monkeypatch.setattr(launcher, "command", command)
        async with unix_serve(echo, str(path)):
            path.chmod(0o600)
            async with await asyncio.start_server(bridge.handle, "127.0.0.1", 0, limit=16384) as server:
                bridge.port = server.sockets[0].getsockname()[1]
                await bridge.preflight()
                assert not bridge.preflighting
                assert json.loads(frames[0])["method"] == "initialize"
                # Browser Origin is rejected even during a permitted self probe.
                bridge.preflighting = True
                reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
                writer.write(upgrade(bridge.port, extra="Origin: https://example.test\r\n"))
                await writer.drain()
                assert b"403" in await reader.readline()
                writer.close()
                await writer.wait_closed()
                async with connect(bridge.endpoint, proxy=None, compression=None) as ws:
                    raw = '{"id":9,"method":"test","params":{"text":"原样","_meta":{"turnId":"native"}}}'
                    await ws.send(raw)
                    assert await ws.recv() == raw
                bridge.preflighting = False
                reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)
                writer.write(upgrade(bridge.port))
                await writer.drain()
                assert b"403" in await reader.readline()
                writer.close()
                await writer.wait_closed()
                await bridge.close()
                assert not bridge.active
        # A missing/unreadable daemon never silently falls back to stdio.
        with pytest.raises(launcher.LaunchError):
            launcher.daemon_socket(profile)


@pytest.mark.asyncio
async def test_reuse_never_starts_another_bridge(monkeypatch, tmp_path):
    identity = ProcessIdentity(42, 100)
    commands = []

    async def command(*args):
        commands.append(args)
        return ""

    monkeypatch.setattr(launcher.desktop, "app_paths", lambda _app: (tmp_path, None, None, None))
    monkeypatch.setattr(launcher.desktop, "_signed", lambda _app: None)
    monkeypatch.setattr(launcher, "daemon_socket", lambda _profile: tmp_path)
    monkeypatch.setattr(launcher, "app_identity", lambda _exe: identity)
    monkeypatch.setattr(launcher, "process_identity", lambda _pid: identity)
    monkeypatch.setattr(launcher, "process_environment_value", lambda *_args: (True, None))
    monkeypatch.setattr(launcher.desktop, "_shared_app", lambda *_args: True)
    monkeypatch.setattr(launcher, "command", command)
    replies = []
    for _ in range(2):
        await launcher.supervise(tmp_path, tmp_path, tmp_path / "state", replies.append)
    assert replies == [{"ok": True, "state": "reused"}] * 2
    assert commands == [("/usr/bin/open", "-a", str(tmp_path))] * 2
    assert not (tmp_path / "state").exists()


@pytest.mark.asyncio
async def test_private_app_is_left_untouched(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "app_identity", lambda _exe: ProcessIdentity(42, 1))
    monkeypatch.setattr(launcher, "process_identity", lambda _pid: ProcessIdentity(42, 1))
    monkeypatch.setattr(launcher.desktop, "_shared_app", lambda *_args: False)
    monkeypatch.setattr(launcher, "process_environment_value", lambda *_args: (True, None))
    with pytest.raises(launcher.LaunchError, match="⌘Q"):
        await launcher.reuse_app(tmp_path, tmp_path, tmp_path)


@pytest.mark.asyncio
async def test_repeated_click_waits_for_first_connection(monkeypatch, tmp_path):
    identity = ProcessIdentity(42, 1)
    monkeypatch.setattr(launcher, "app_identity", lambda _exe: identity)
    monkeypatch.setattr(launcher, "process_identity", lambda _pid: identity)
    monkeypatch.setattr(launcher, "process_environment_value", lambda *_args: (True, "ws://127.0.0.1:2345/rpc"))
    monkeypatch.setattr(launcher, "matches_launch", lambda *_args: True)
    states = iter([False, False, True])
    monkeypatch.setattr(launcher.desktop, "_shared_app", lambda *_args: next(states))

    async def command(*_args):
        return ""

    monkeypatch.setattr(launcher, "command", command)
    assert await launcher.reuse_app(tmp_path, tmp_path, tmp_path) == identity


@pytest.mark.asyncio
async def test_fresh_launch_then_quit_then_reopen(monkeypatch, tmp_path):
    current = None
    env = {}
    opened = []
    bridges = []

    class FakeBridge(launcher.Bridge):
        async def preflight(self):
            bridges.append(self)

    monkeypatch.setattr(launcher, "Bridge", FakeBridge)
    monkeypatch.setattr(launcher.desktop, "app_paths", lambda _app: (tmp_path, None, None, None))
    monkeypatch.setattr(launcher.desktop, "_signed", lambda _app: None)
    monkeypatch.setattr(launcher, "daemon_socket", lambda _profile: tmp_path)
    monkeypatch.setattr(launcher, "app_identity", lambda _exe: current)
    monkeypatch.setattr(launcher, "process_identity", lambda _pid: current)
    monkeypatch.setattr(launcher, "process_owner_uid", lambda _pid: os.getuid())
    monkeypatch.setattr(launcher, "process_environment_value", lambda _id, key: (True, env.get(key)))

    async def command(*args):
        nonlocal current
        assert args[:2] == ("/usr/bin/open", "-a")
        opened.append(args)
        for i, item in enumerate(args):
            if item == "--env":
                key, value = args[i + 1].split("=", 1)
                env[key] = value
        current = ProcessIdentity(42, len(opened))
        bridges[-1].app_connected.set()
        return ""

    monkeypatch.setattr(launcher, "command", command)
    for _ in range(2):
        ready = asyncio.Queue()
        # The old supervisor is still releasing its lock just after App quit.
        # The new click must retry the lock, not wait forever for a missing App.
        async with launcher.launch_lock(tmp_path, tmp_path / "state"):
            task = asyncio.create_task(launcher.supervise(tmp_path, tmp_path, tmp_path / "state", ready.put_nowait))
            await asyncio.sleep(0.05)
            assert ready.empty()
        assert await asyncio.wait_for(ready.get(), 3) == {"ok": True, "state": "connected"}
        assert env["CODEX_HOME"] == str(tmp_path)
        assert env["CODEX_APP_SERVER_FORCE_CLI"] == "0"
        assert env["CODEX_APP_SERVER_WS_URL"] == bridges[-1].endpoint
        assert not task.done()
        current = None  # User quits. Only the launcher's own listener exits.
        await asyncio.wait_for(task, 3)
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", bridges[-1].port)
    assert len(opened) == 2


def test_install_separate_bundle_and_never_overwrites(monkeypatch, tmp_path):
    app = tmp_path / "Official.app"
    resources = app / "Contents/Resources"
    resources.mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIconFile": "official.icns"}))
    (resources / "official.icns").write_bytes(b"test icon")
    monkeypatch.setattr(launcher.desktop, "app_paths", lambda _app: None)
    monkeypatch.setattr(launcher.desktop, "_signed", lambda _app: None)
    monkeypatch.setattr(launcher, "daemon_socket", lambda _profile: tmp_path)
    monkeypatch.setattr(launcher, "state_root", lambda: tmp_path / "state")

    def command(args, **_kwargs):
        if args[0] == "/usr/bin/swiftc":
            Path(args[-1]).write_bytes(b"test binary")

    monkeypatch.setattr(launcher.subprocess, "run", command)
    target = tmp_path / "Applications/Codex Shared.app"
    assert launcher.install(tmp_path, app, target)["ok"]
    info = plistlib.loads((target / "Contents/Info.plist").read_bytes())
    assert info["LSUIElement"]
    assert info["CFBundleIdentifier"] != "com.openai.codex"
    config = json.loads((target / "Contents/Resources/launcher.json").read_text())
    assert config["profile"] == str(tmp_path)
    assert config["app"] == str(app)
    assert set(config) == {"profile", "app", "state_dir", "cwd", "python"}
    assert (resources / "official.icns").read_bytes() == b"test icon"
    with pytest.raises(launcher.LaunchError, match="不会覆盖"):
        launcher.install(tmp_path, app, target)
    with pytest.raises(launcher.LaunchError):
        launcher.install(tmp_path, app, app)


def test_dock_append_is_idempotent_and_preserves_other_preferences(monkeypatch, tmp_path):
    target = tmp_path / "Codex Shared.app"
    (target / "Contents").mkdir(parents=True)
    (target / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "local.cc-remote.codex-shared.test"}))
    preferences = {"persistent-apps": [{"tile-data": {"file-label": "User App"}}], "autohide": True}
    monkeypatch.setattr(launcher, "state_root", lambda: tmp_path / "state")
    monkeypatch.setattr(launcher.subprocess, "check_output", lambda *_args, **_kwargs: plistlib.dumps(preferences))
    commands = []

    def command(args, **_kwargs):
        commands.append(args)
        assert args[:5] == ["/usr/bin/defaults", "write", "com.apple.dock", "persistent-apps", "-array-add"]
        preferences["persistent-apps"].append(plistlib.loads(args[-1].encode()))

    monkeypatch.setattr(launcher.subprocess, "run", command)
    result = launcher.pin_dock(target)
    assert result["state"] == "pinned"
    assert preferences["persistent-apps"][0]["tile-data"]["file-label"] == "User App"
    assert preferences["autohide"]
    assert launcher.pin_dock(target)["state"] == "already_pinned"
    assert len(commands) == 1
    assert plistlib.loads(Path(result["backup"]).read_bytes()) == [{"tile-data": {"file-label": "User App"}}]
