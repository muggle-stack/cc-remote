"""Installer ownership and rollback without touching a real service manager."""
from __future__ import annotations

import json
import os
import plistlib
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from cc_remote.config import wrapper_config
from deploy import install_gemini_service as installer


def test_unit_keeps_the_release_and_nonsecret_service_scope(tmp_path):
    release = tmp_path / "releases/immutable build"
    state = tmp_path / "gemini"
    argv = [str(release / ".venv/bin/python"), "-m", "cc_remote.antigravity_service",
            "--state-dir", str(state)]
    env = {"CC_REMOTE_ANTIGRAVITY": "1", "CC_REMOTE_ANTIGRAVITY_BIN": "/path/with spaces/agy"}
    plist = plistlib.loads(installer.render_unit("darwin", release, state, argv, env))
    assert plist["ProgramArguments"] == argv and plist["WorkingDirectory"] == str(release)
    assert plist["EnvironmentVariables"] == env
    unit = installer.render_unit("linux", release, state, argv, env).decode()
    assert f'ExecStart="{argv[0]}"' in unit and f"WorkingDirectory={release}" in unit
    assert '"CC_REMOTE_ANTIGRAVITY_BIN=/path/with spaces/agy"' in unit


def test_unit_names_are_scoped_to_the_wrapper(tmp_path, monkeypatch):
    for platform in ("darwin", "linux"):
        monkeypatch.setattr(installer.sys, "platform", platform)
        first = installer.unit_destination(tmp_path / "one")
        assert first == installer.unit_destination(tmp_path / "one")
        assert first != installer.unit_destination(tmp_path / "two")


def test_macos_waits_for_the_old_unit_to_disappear_after_bootout(tmp_path, monkeypatch):
    monkeypatch.setattr(installer.sys, "platform", "darwin")
    calls = []
    exits = iter((0, 0, 0, 3))

    def manager(argv, **kwargs):
        calls.append(argv[1])
        return SimpleNamespace(returncode=next(exits))

    monkeypatch.setattr(installer.subprocess, "run", manager)
    monkeypatch.setattr(installer.time, "sleep", lambda *_: None)
    installer.manage_unit(tmp_path / "fixture.plist", start=False)
    assert calls == ["print", "bootout", "print", "print"]


@pytest.mark.asyncio
async def test_registration_waits_for_ready_and_explicit_override_wins(tmp_path, monkeypatch):
    state, wrapper = tmp_path / "worker", tmp_path / "wrapper"
    for directory in (state, wrapper):
        directory.mkdir(mode=0o700)
    unit = tmp_path / "systemd" / installer.UNIT
    release = tmp_path / "release"
    registration = wrapper / "gemini-service.json"
    actions = []
    monkeypatch.setattr(installer, "manage_unit", lambda path, *, start: actions.append((path, start)))

    async def ready(socket, machine, wrapper_state, expected):
        assert not registration.exists()
        assert (state / installer.GEMINI_SWITCH_FENCE).exists()
        assert (socket, machine, wrapper_state, expected) == (state / "service.sock", "fixture", wrapper, release)

    monkeypatch.setattr(installer, "wait_ready", ready)
    await installer.install_unit(unit, b"unit", state, wrapper, "fixture", release)
    assert actions == [(unit, True)]
    assert not (state / installer.GEMINI_SWITCH_FENCE).exists()
    assert os.stat(registration).st_mode & 0o077 == 0
    assert json.loads(registration.read_text())["machine"] == "fixture"
    monkeypatch.setenv("CC_REMOTE_STATE_DIR", str(wrapper))
    monkeypatch.delenv("CC_REMOTE_ANTIGRAVITY_SERVICE_SOCKET", raising=False)
    assert wrapper_config().antigravity_service_socket == str(state / "service.sock")
    monkeypatch.setenv("CC_REMOTE_ANTIGRAVITY_SERVICE_SOCKET", "")
    assert wrapper_config().antigravity_service_socket == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [True, False])
async def test_upgrade_respects_busy_worker_and_restores_failed_candidate(tmp_path, monkeypatch, busy):
    state, wrapper = tmp_path / "worker", tmp_path / "wrapper"
    for directory in (state, wrapper):
        directory.mkdir(mode=0o700)
    unit = tmp_path / "systemd" / installer.UNIT
    installer.write_unit(unit, b"known good")
    installer.write_json(wrapper / "gemini-service.json", {
        "socket": str(state / "service.sock"), "machine": "fixture",
    }, mode=0o600)
    release = tmp_path / "candidate"
    actions = []

    @asynccontextmanager
    async def control(socket, machine, op):
        assert (socket, machine, op) == (state / "service.sock", "fixture", "drain")
        yield {"error": "busy"} if busy else {"draining": True, "state_dir": str(wrapper)}

    async def ready(socket, machine, wrapper_state, expected):
        assert (state / installer.GEMINI_SWITCH_FENCE).exists()
        if expected == release:
            raise TimeoutError("candidate not ready")
        assert expected is None and unit.read_bytes() == b"known good"

    def manage(path, *, start):
        assert path == unit
        actions.append((start, path.read_bytes()))

    monkeypatch.setattr(installer, "service_control", control)
    monkeypatch.setattr(installer, "manage_unit", manage)
    monkeypatch.setattr(installer, "wait_ready", ready)
    with pytest.raises(RuntimeError if busy else TimeoutError, match="busy" if busy else "not ready"):
        await installer.install_unit(unit, b"candidate", state, wrapper, "fixture", release, upgrade=True)
    assert unit.read_bytes() == b"known good"
    assert not (state / installer.GEMINI_SWITCH_FENCE).exists()
    if busy:
        assert actions == []
    else:
        assert actions == [(False, b"known good"), (True, b"candidate"),
                           (False, b"candidate"), (True, b"known good")]
