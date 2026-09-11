"""Local TUI login is automatic and cannot export service credentials."""

import asyncio
import builtins
from types import SimpleNamespace

import pytest

from cc_remote import tui, tui_local
from cc_remote.tui_local import LocalRelay


@pytest.mark.parametrize("module", sorted(tui.OPTIONAL_TUI_MODULES))
def test_missing_optional_dependency_has_install_guidance(monkeypatch, capsys, module):
    original = builtins.__import__

    def importing(name, *args, **kwargs):
        if name == "cc_remote.tui_app":
            raise ModuleNotFoundError(f"No module named {module}", name=module)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", importing)
    monkeypatch.setattr(tui.sys, "argv", ["tui", "--demo"])
    with pytest.raises(SystemExit) as exc:
        tui.main()
    assert exc.value.code == 1
    assert "Install requirements-tui.txt, or use --line-mode" in capsys.readouterr().err


@pytest.mark.parametrize("module", ["cc_remote.typo", "unrelated", "textual.missing"])
def test_unrelated_import_error_is_not_hidden(monkeypatch, module):
    original = builtins.__import__

    def importing(name, *args, **kwargs):
        if name == "cc_remote.tui_app":
            raise ModuleNotFoundError(f"No module named {module}", name=module)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", importing)
    monkeypatch.setattr(tui.sys, "argv", ["tui", "--demo"])
    with pytest.raises(ModuleNotFoundError) as exc:
        tui.main()
    assert exc.value.name == module


@pytest.mark.parametrize("args,env,scope,explicit", [
    (["--engine", "claude", "--space", "work"], None,
     ("claude", "work"), (True, True)),
    (["--space", "code"], "claude", ("claude", "code"), (True, True)),
    ([], "claude", ("claude", "code"), (True, False)),
    ([], None, ("codex", "code"), (False, False)),
])
def test_main_marks_only_explicit_startup_scope(
    monkeypatch, args, env, scope, explicit,
):
    from cc_remote import tui_app

    monkeypatch.delenv("ENGINE", raising=False)
    if env:
        monkeypatch.setenv("ENGINE", env)
    monkeypatch.setattr(tui, "ENGINE", "codex")
    monkeypatch.setattr(tui.sys, "argv", ["tui", "--demo", *args])
    clients = []
    monkeypatch.setattr(tui_app, "run_workspace", lambda c, **kw: clients.append(c))
    tui.main()
    c = clients[0]
    assert c.scope == scope
    assert (c.explicit_engine, c.explicit_space) == explicit


@pytest.mark.parametrize("host,authority", [
    ("0.0.0.0", "127.0.0.1"), ("127.0.0.1", "127.0.0.1"),
    ("localhost", "127.0.0.1"), ("::", "[::1]"),
])
def test_discovery_uses_real_port_without_exposing_password(monkeypatch, host, authority):
    monkeypatch.setattr(tui_local, "_service_environment", lambda: {
        "RELAY_HOST": host, "RELAY_PORT": "9876", "LOGIN_PASSWORD": "private-value",
        "ALLOW_PRIVATE_ORIGINS": "1", "PUBLIC_ORIGIN": "https://relay.example",
    })
    local = tui_local.discover_local_relay()
    assert local.url == f"ws://{authority}:9876/ws"
    assert local.password == "private-value"
    assert local.origin == f"http://{authority}:9876"
    assert "private-value" not in repr(local)


@pytest.mark.parametrize("overrides", [
    {"RELAY_HOST": "10.0.0.2"}, {"RELAY_HOST": "untrusted.example"},
    {"RELAY_PORT": "0"}, {"RELAY_PORT": "65536"}, {"RELAY_PORT": "bad"},
    {"LOGIN_PASSWORD": ""}, {"LOGIN_USERS_JSON": '{"accounts":[]}'},
])
def test_discovery_fails_closed(monkeypatch, overrides):
    monkeypatch.setattr(tui_local, "_service_environment", lambda: {
        "LOGIN_PASSWORD": "private-value", "ALLOW_PRIVATE_ORIGINS": "1", **overrides,
    })
    assert tui_local.discover_local_relay() is None


@pytest.mark.parametrize("target", [
    "wss://remote.example/ws", "ws://127.0.0.1:8765/ws",
    "ws://127.0.0.2:9876/ws", "ws://127.0.0.1:9876/other",
    "ws://user@127.0.0.1:9876/ws", "ws://127.0.0.1:9876/ws?token=x",
    "ws://127.0.0.1:9876/ws#x", "wss://127.0.0.1:9876/ws",
])
def test_local_secret_is_not_reused_for_other_targets(target):
    assert not tui_local.same_local_endpoint("ws://127.0.0.1:9876/ws", target)


def test_unavailable_service_does_not_read_process_environment(monkeypatch):
    monkeypatch.setattr(tui_local.subprocess, "run", lambda *a, **k: SimpleNamespace(
        stdout="MainPID=0\nActiveState=inactive\n"))
    monkeypatch.setattr(tui_local.os, "open", lambda *a, **k: pytest.fail("opened proc"))
    assert tui_local._service_environment() == {}


def test_different_user_service_is_not_read(monkeypatch):
    monkeypatch.setattr(tui_local.subprocess, "run", lambda *a, **k: SimpleNamespace(
        stdout="MainPID=123\nActiveState=active\n"))
    opened = []
    monkeypatch.setattr(tui_local.os, "open", lambda *a, **k: opened.append(a) or 42)
    monkeypatch.setattr(tui_local.os, "fstat", lambda fd: SimpleNamespace(st_uid=-1))
    monkeypatch.setattr(tui_local.os, "close", lambda fd: None)
    assert tui_local._service_environment() == {}
    assert len(opened) == 1


def test_local_login_never_prompts_and_refreshes_on_reauthentication(monkeypatch):
    local = LocalRelay("ws://127.0.0.1:9876/ws", "first", "https://relay.example")
    monkeypatch.setattr(tui, "discover_local_relay", lambda: local)
    monkeypatch.setattr(tui.getpass, "getpass", lambda *a: pytest.fail("prompted"))
    calls = []
    monkeypatch.setattr(tui, "_login_cookie", lambda *a: calls.append(a) or "cookie")
    client = tui.Tui(local.url, "", "", "codex", None)
    asyncio.run(client._authenticate())
    assert client.local_login and client.origin == local.origin
    local = LocalRelay(local.url, "rotated", local.origin)
    asyncio.run(client._authenticate())
    assert [call[1] for call in calls] == ["first", "rotated"]


def test_missing_local_service_fails_without_password_prompt(monkeypatch):
    monkeypatch.setattr(tui, "discover_local_relay", lambda: None)
    monkeypatch.setattr(tui.getpass, "getpass", lambda *a: pytest.fail("prompted"))
    client = tui.Tui("ws://127.0.0.1:9876/ws", "", "", "codex", None)
    with pytest.raises(ValueError, match="Local relay authentication is unavailable"):
        asyncio.run(client._authenticate())


def test_remote_login_never_discovers_local_credentials(monkeypatch):
    monkeypatch.setattr(tui, "discover_local_relay", lambda: pytest.fail("discovered"))
    monkeypatch.setattr(tui.getpass, "getpass", lambda *a: "remote-password")
    calls = []
    monkeypatch.setattr(tui, "_login_cookie", lambda *a: calls.append(a) or "cookie")
    client = tui.Tui("wss://remote.example/ws", "", "", "codex", None)
    asyncio.run(client._authenticate())
    assert calls == [(client.url, "remote-password", "")]


def test_bare_command_discovers_local_port_before_starting_ui(monkeypatch):
    from cc_remote import tui_app
    local = LocalRelay("ws://127.0.0.1:9876/ws", "local-password")
    monkeypatch.delenv("RELAY_URL", raising=False)
    for name in ("LOGIN_PASSWORD", "LOGIN_USERNAME", "PUBLIC_ORIGIN"):
        monkeypatch.setattr(tui, name, "")
    monkeypatch.setattr(tui, "discover_local_relay", lambda: local)
    monkeypatch.setattr(tui.sys, "argv", ["cc_remote.tui"])
    clients = []
    monkeypatch.setattr(tui_app, "run_workspace", lambda client, **kw: clients.append(client))
    tui.main()
    assert clients[0].url == local.url
    assert clients[0].password == local.password
    assert clients[0].local_login
