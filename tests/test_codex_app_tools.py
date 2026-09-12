"""Zero-model-turn coverage for the opt-in Desktop tools transport."""
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import tomllib

import pytest

from cc_remote import codex_app_tools as tools
from cc_remote.wrapper.process_scan import ProcessIdentity


def test_private_socket_rejects_world_access_and_symlinks():
    # Darwin sockaddr_un cannot hold pytest's long per-test directory paths.
    with tempfile.TemporaryDirectory(prefix="cc-app-", dir="/tmp") as root:
        path = Path(root) / "app.sock"
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(str(path))
            path.chmod(0o600)
            assert tools._private_socket(path).st_ino
            link = Path(root) / "alias.sock"
            link.symlink_to(path)
            with pytest.raises(ValueError):
                tools._private_socket(link)
            path.chmod(0o666)
            with pytest.raises(ValueError):
                tools._private_socket(path)


@pytest.mark.parametrize("home,endpoint,connected,expected", [
    ("same", "ws://127.0.0.1:1234/rpc", True, True),
    ("other", "ws://127.0.0.1:1234/rpc", True, False),
    (None, "ws://127.0.0.1:1234/rpc", True, False),
    ("same", None, True, False),
    ("same", "ws://127.0.0.1:1234/rpc", False, False),
    ("same", "ws://example.com:1234/rpc", True, False),
    ("same", "ws://127.0.0.1:1234/rpc?token=x", True, False),
    ("same", "ws://user:pass@127.0.0.1:1234/rpc", True, False),
    ("same", "ws://127.0.0.1:bad/rpc", True, False),
    ("same", "ws://127.0.0.1:1234/private", True, False),
    ("same", "ws://127.0.0.1:1234", True, False),
])
def test_discovery_requires_exact_shared_profile(monkeypatch, tmp_path, home, endpoint, connected, expected):
    profile = tmp_path.resolve()
    env = {
        "CODEX_HOME": str(profile) if home == "same" else (str(profile / "other") if home else None),
        "CODEX_APP_SERVER_WS_URL": endpoint,
    }
    monkeypatch.setattr(tools, "process_environment_value", lambda _pid, key: (True, env[key]))
    monkeypatch.setattr(tools, "_command", lambda *args: (
        "n127.0.0.1:1234->127.0.0.1:999\n" if "43" in args
        else "n127.0.0.1:999->127.0.0.1:1234\n"
    ) if connected else "")
    monkeypatch.setattr(tools, "_shared_bridge", lambda *_args: ProcessIdentity(43, 100))
    monkeypatch.setattr(tools, "process_identity", lambda pid: ProcessIdentity(pid, 100))
    monkeypatch.setattr(tools, "process_owner_uid", lambda _pid: tools.os.getuid())
    assert tools._shared_app(ProcessIdentity(42, 100), profile) is expected


@pytest.mark.parametrize("failure", [
    None, "private_listener", "wrong_profile", "duplicate_profile", "wrong_path",
    "missing_listener", "ambiguous_listener", "foreign_owner", "wrong_peer",
    "bridge_reused", "app_reused", "missing_daemon", "linked_daemon",
])
def test_shared_app_requires_profile_bound_bridge_and_both_connection_ends(monkeypatch, failure):
    with tempfile.TemporaryDirectory(prefix="cc-bridge-", dir="/tmp") as root:
        profile = Path(root).resolve()
        directory = profile / "app-server-control"
        directory.mkdir()
        upstream = directory / "app-server-control.sock"
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(str(upstream))
            upstream.chmod(0o600)
            if failure == "missing_daemon":
                upstream.unlink()
            elif failure == "linked_daemon":
                actual = directory / "actual.sock"
                upstream.rename(actual)
                upstream.symlink_to(actual)
            env = {
                "CODEX_HOME": str(profile),
                "CODEX_APP_SERVER_WS_URL": "ws://127.0.0.1:1234/" + (
                    "private" if failure == "wrong_path" else "rpc"),
            }
            argv = ["/python", "-m", "cc_remote.codex_desktop", "run",
                    "--profile", str(profile), "--app", "/Official.app",
                    "--state-dir", root, "--ready-fd", "4"]
            if failure == "private_listener":
                argv = ["/codex", "app-server", "--listen", "ws://127.0.0.1:1234"]
            elif failure == "wrong_profile":
                argv[5] = str(profile.parent)
            elif failure == "duplicate_profile":
                argv += ["--profile", str(profile.parent)]
            owner = tools.os.getuid() + (failure == "foreign_owner")
            calls = {}

            def identity(pid):
                calls[pid] = calls.get(pid, 0) + 1
                reused = ((failure == "bridge_reused" and pid == 43 and calls[pid] > 1)
                          or (failure == "app_reused" and pid == 42))
                return ProcessIdentity(pid, 200 if reused else 100)

            def command(*args):
                if "-sTCP:LISTEN" in args:
                    if failure == "missing_listener":
                        return ""
                    result = f"p43\nu{owner}\nn127.0.0.1:1234\n"
                    if failure == "ambiguous_listener":
                        result += f"p44\nu{owner}\nn127.0.0.1:1234\n"
                    return result
                if "43" in args:
                    peer = 888 if failure == "wrong_peer" else 999
                    return f"n127.0.0.1:1234->127.0.0.1:{peer}\n"
                return "n127.0.0.1:999->127.0.0.1:1234\n"

            monkeypatch.setattr(tools, "process_environment_value", lambda _id, key: (True, env[key]))
            monkeypatch.setattr(tools, "process_command", lambda _id: tuple(arg.encode() for arg in argv))
            monkeypatch.setattr(tools, "process_owner_uid", lambda _pid: owner)
            monkeypatch.setattr(tools, "process_identity", identity)
            monkeypatch.setattr(tools, "_command", command)
            assert tools._shared_app(ProcessIdentity(42, 100), profile) is (failure is None)


def test_incomplete_profile_read_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "process_environment_value", lambda *args: (False, str(tmp_path)))
    assert not tools._shared_app(ProcessIdentity(42, 100), tmp_path)


def test_discovery_without_macos_is_optional(monkeypatch, tmp_path):
    monkeypatch.setattr(tools.sys, "platform", "linux")
    assert tools.discover(tmp_path, tmp_path)["state"] == "unavailable"


def test_changed_official_policy_cannot_use_old_approval_rules(monkeypatch, tmp_path):
    monkeypatch.setattr(tools.sys, "platform", "darwin")
    monkeypatch.setattr(tools, "_private_socket", lambda _path: None)
    monkeypatch.setattr(tools, "app_paths", lambda _app: (tmp_path, tmp_path, tmp_path, "test"))
    (tmp_path / "desktop-mcp.json").write_text(json.dumps({"mcpServers": {"codex_app": {"tools": {"new_write": {"approval_mode": "prompt"}}}}}))
    assert tools.discover(tmp_path, tmp_path, "old-manifest")["reason"] == "official_policy_changed"


def test_discovery_rejects_two_matching_apps_and_pid_reuse(monkeypatch, tmp_path):
    monkeypatch.setattr(tools.sys, "platform", "darwin")
    monkeypatch.setattr(tools, "app_paths", lambda _app: (tmp_path, tmp_path, tmp_path, "test"))
    monkeypatch.setattr(tools, "_signed", lambda _path: None)
    monkeypatch.setattr(tools, "_matching_app_pids", lambda _exe: [41, 42])
    monkeypatch.setattr(tools, "process_identity", lambda pid: ProcessIdentity(pid, 1))
    monkeypatch.setattr(tools, "process_owner_uid", lambda _pid: tools.os.getuid())
    monkeypatch.setattr(tools, "_shared_app", lambda *_args: True)
    monkeypatch.setattr(tools, "_pipe_from_open_logs", lambda *_args: tmp_path)
    monkeypatch.setattr(tools, "_private_socket", lambda _path: tmp_path.stat())
    assert tools.discover(tmp_path, tmp_path)["reason"] == "no_unique_shared_app"
    monkeypatch.setattr(tools, "_matching_app_pids", lambda _exe: [42])
    assert tools.discover(tmp_path, tmp_path)["state"] == "ready"
    ids = iter([ProcessIdentity(42, 1), ProcessIdentity(42, 2)])
    monkeypatch.setattr(tools, "process_identity", lambda _pid: next(ids))
    assert tools.discover(tmp_path, tmp_path)["state"] == "unavailable"


def test_open_log_marker_must_be_an_app_owned_socket(monkeypatch, tmp_path):
    root = tmp_path / "Library/Logs/com.openai.codex"
    root.mkdir(parents=True)
    log = root / "codex-desktop-test-42-t0-i1.log"
    pipe = "/tmp/codex-browser-use/11111111-1111-1111-1111-111111111111.sock"
    log.write_text(f"2026-09-09T00:00:00Z info [dynamic-app-tools-native-pipe] dynamic_app_tools_listening pipePath={pipe}\n")
    log.chmod(0o600)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(tools, "_private_socket", lambda _path: None)
    monkeypatch.setattr(tools, "_command", lambda *args: f"n{log}\n")
    assert tools._pipe_from_open_logs(42, "com.openai.codex") is None
    monkeypatch.setattr(tools, "_command", lambda *args: f"n{pipe}\n" if "-U" in args else f"n{log}\n")
    assert str(tools._pipe_from_open_logs(42, "com.openai.codex")) == pipe
    log.write_text(f'2026-09-09T00:00:00Z info [electron-message-handler] summary="dynamic_app_tools_listening pipePath={pipe}"\n')
    assert tools._pipe_from_open_logs(42, "com.openai.codex") is None
    log.chmod(0o666)
    assert tools._pipe_from_open_logs(42, "com.openai.codex") is None


def test_config_preserves_official_tool_approval_policy(monkeypatch, tmp_path):
    original = {
        "command": "official", "args": ["server.mjs"], "enabled": True,
        "default_tools_approval_mode": "approve",
        "tools": {"send_message_to_thread": {"approval_mode": "prompt"}},
        "env_vars": ["HOME", "CODEX_APP_TOOLS_PIPE_PATH"],
    }
    manifest = tmp_path / "desktop-mcp.json"
    manifest.write_text(json.dumps({"mcpServers": {"codex_app": original}}))
    monkeypatch.setattr(tools, "app_paths", lambda _app: (tmp_path, tmp_path / "node", tmp_path, "test"))
    monkeypatch.setattr(tools, "_signed", lambda _path: None)
    result = tools.mcp_config(tmp_path, tmp_path)
    assert result["required"] is False
    assert result["tools"] == original["tools"]
    assert result["default_tools_approval_mode"] == "approve"
    assert "enabled_tools" not in result
    assert result["env_vars"] == ["HOME"]
    assert result["args"][-2:] == ["--manifest-sha256", tools._manifest_hash(original)]
    assert tomllib.loads(tools.config_toml(result))["mcp_servers"]["codex_app"] == result
    assert json.loads(manifest.read_text())["mcpServers"]["codex_app"] == original


def _ready_app(monkeypatch, tmp_path):
    node = tmp_path / "node"
    node.write_text("old runtime")
    monkeypatch.setattr(tools.sys, "platform", "darwin")
    monkeypatch.setattr(tools, "app_paths", lambda _app: (tmp_path, node, tmp_path, "test"))
    monkeypatch.setattr(tools, "_signed", lambda _path: None)
    monkeypatch.setattr(tools, "_matching_app_pids", lambda _exe: [42])
    monkeypatch.setattr(tools, "process_identity", lambda pid: ProcessIdentity(pid, 1))
    monkeypatch.setattr(tools, "process_owner_uid", lambda _pid: tools.os.getuid())
    monkeypatch.setattr(tools, "_shared_app", lambda *_args: True)
    monkeypatch.setattr(tools, "_pipe_from_open_logs", lambda *_args: tmp_path)
    socket_info = tmp_path.stat()
    monkeypatch.setattr(tools, "_private_socket", lambda _path: socket_info)
    return node


def test_runtime_update_changes_generation_without_app_or_pipe_restart(monkeypatch, tmp_path):
    node = _ready_app(monkeypatch, tmp_path)
    before = tools.discover(tmp_path, tmp_path)
    replacement = tmp_path / "new-node"
    replacement.write_text("new runtime")
    replacement.replace(node)
    after = tools.discover(tmp_path, tmp_path)
    assert before["state"] == after["state"] == "ready"
    assert before["pid"] == after["pid"]
    assert before["pipe"] == after["pipe"]
    assert before["generation"] != after["generation"]


def test_runtime_replaced_during_signature_validation_stays_unavailable(monkeypatch, tmp_path):
    node = _ready_app(monkeypatch, tmp_path)
    monkeypatch.setattr(tools, "_signed", lambda _path: node.write_text("replacement"))
    assert tools.discover(tmp_path, tmp_path)["state"] == "unavailable"


def test_new_runtime_signature_failure_stays_unavailable(monkeypatch, tmp_path):
    _ready_app(monkeypatch, tmp_path)

    def reject(_path):
        raise ValueError("invalid official signature")

    monkeypatch.setattr(tools, "_signed", reject)
    assert tools.discover(tmp_path, tmp_path)["state"] == "unavailable"


def test_desktop_mcp_adapter_node_suite():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the optional Desktop MCP adapter")
    subprocess.run(
        [node, "--test", str(Path(__file__).with_name("codex_app_tools.test.mjs"))],
        check=True, timeout=30,
    )
