"""An interactive /resume changes ownership without changing process argv."""
from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest

from cc_remote.wrapper import claude_external, claude_registry
from cc_remote.wrapper.process_scan import ProcessIdentity
from tests.test_claude_external import _fake_process


@pytest.fixture
def registry(tmp_path, monkeypatch):
    root = tmp_path / "account"
    (root / "sessions").mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    proc = tmp_path / "proc"
    first, second, third = (str(uuid4()) for _ in range(3))
    identity = ProcessIdentity(401, 4001)
    _fake_process(proc, identity.pid, identity.start_ticks, cwd=project,
                  cmdline=("claude", "--resume", first))
    monkeypatch.setattr(claude_registry, "_native_process_start", lambda _id: "start")
    path = root / "sessions" / "401.json"

    def write(sid, **extra):
        path.write_text(json.dumps({"pid": 401, "procStart": "start", "sessionId": sid,
                                    "kind": "interactive", **extra}))

    def scan():
        return claude_external.claude_session_holders(
            {first: "first.jsonl", second: "second.jsonl"},
            {first: str(project), second: str(project)}, wrapper_pid=900,
            proc_root=str(proc), default_config_dir=str(root))

    return root, proc, path, first, second, third, identity, write, scan


def test_registry_tracks_internal_resume_and_unwatched_target(registry):
    _root, _proc, _path, first, second, third, identity, write, scan = registry
    write(first)
    assert scan().holders == {first: {identity}, second: set()}
    write(second)
    assert scan().holders == {first: set(), second: {identity}}
    write(third)
    result = scan()
    assert result.complete and result.holders == {first: set(), second: set()}


def test_bare_cli_with_multiple_same_cwd_sessions_uses_registry(registry):
    _root, proc, _path, first, second, _third, identity, write, scan = registry
    (proc / "401" / "cmdline").write_bytes(b"claude\x00")
    write(second)
    assert scan().holders == {first: set(), second: {identity}}


def test_missing_registry_preserves_older_cli_fallback(registry):
    _root, _proc, _path, first, second, _third, identity, _write, scan = registry
    result = scan()
    assert result.complete and result.holders == {first: {identity}, second: set()}


@pytest.mark.parametrize("invalid", ["bad-json", "wrong-start", "wrong-pid", "symlink"])
def test_invalid_record_never_falls_back_to_old_launch_target(registry, invalid):
    root, _proc, path, first, second, _third, _identity, write, scan = registry
    write(second)
    if invalid == "bad-json":
        path.write_text('{')
    elif invalid == "wrong-start":
        write(second, procStart="a previous process")
    elif invalid == "wrong-pid":
        write(second, pid=402)
    else:
        outside = root / "another.json"
        path.rename(outside)
        path.symlink_to(outside)
    result = scan()
    assert not result.complete
    assert result.holders == {first: set(), second: set()}


def test_registry_cannot_claim_reused_or_exited_pid(registry):
    root, proc, _path, _first, second, _third, identity, write, _scan = registry
    write(second)
    stat_path = proc / "401" / "stat"
    stat_path.write_text(stat_path.read_text().replace(" 4001 ", " 9999 "))
    assert claude_registry.registered_session(identity, str(root), proc_root=str(proc)) == (False, None)
    (proc / "401" / "stat").unlink()
    assert claude_registry.registered_session(identity, str(root), proc_root=str(proc)) == (False, None)


def test_registry_is_account_scoped_even_with_identical_session_ids(registry, monkeypatch):
    root, proc, _path, first, second, _third, identity, write, _scan = registry
    other = root.parent / "other"
    (other / "sessions").mkdir(parents=True)
    write(second)
    (other / "sessions" / "401.json").write_text(json.dumps({
        "pid": 401, "sessionId": first, "procStart": "start"}))
    (proc / "401" / "environ").write_bytes(f"CLAUDE_CONFIG_DIR={root}\x00".encode())
    paths = {f"a@{second}": "a.jsonl", f"b@{second}": "b.jsonl"}
    result = claude_external.claude_session_holders(
        paths, {sid: str(root.parent / "project") for sid in paths}, wrapper_pid=900,
        proc_root=str(proc), config_dirs={f"a@{second}": str(root), f"b@{second}": str(other)},
        native_session_ids={sid: second for sid in paths}, default_config_dir=str(other))
    assert result.complete
    assert result.holders == {f"a@{second}": {identity}, f"b@{second}": set()}


def test_owned_service_descendant_is_not_an_external_registry_holder(registry):
    _root, proc, _path, first, second, _third, identity, write, scan = registry
    write(second)
    stat_path = proc / "401" / "stat"
    stat_path.write_text(stat_path.read_text().replace("(claude) S 1 ", "(claude) S 900 "))
    assert scan().holders == {first: set(), second: set()}


@pytest.mark.parametrize("version_named_binary", [False, True])
def test_darwin_registry_takes_precedence_over_argv(registry, monkeypatch, version_named_binary):
    root, _proc, _path, first, second, _third, identity, write, _scan = registry
    write(second)
    command = (b"claude", b"--resume", first.encode())
    monkeypatch.setattr(claude_external, "darwin_process_snapshot", lambda: (
        [(identity, 1, 1, () if version_named_binary else command)], True))
    monkeypatch.setattr(claude_external, "process_command", lambda _identity: command)
    monkeypatch.setattr(claude_external, "_darwin_process_cwds", lambda _pids: ({}, True))
    monkeypatch.setattr(claude_registry, "process_identity", lambda *_a, **_k: identity)
    monkeypatch.setattr(claude_registry, "process_owner_uid", lambda *_a, **_k: os.getuid())
    result = claude_external._darwin_claude_session_holders(
        {first: "a", second: "b"}, {}, wrapper_pid=900,
        continue_bindings={}, continue_candidates={}, continue_resolver=None,
        config_dirs=None, native_session_ids=None, default_config_dir=str(root))
    assert result.complete and result.holders == {first: set(), second: {identity}}


def test_linux_registry_accepts_proc_start_ticks(registry, monkeypatch):
    _root, _proc, _path, first, second, _third, identity, write, scan = registry

    def no_ps(_identity):
        raise OSError("ps unavailable")

    monkeypatch.setattr(claude_registry.sys, "platform", "linux")
    monkeypatch.setattr(claude_registry, "_native_process_start", no_ps)
    write(second, procStart=str(identity.start_ticks))
    result = scan()
    assert result.complete and result.holders == {first: set(), second: {identity}}
    write(second, procStart=str(identity.start_ticks + 1))
    assert not scan().complete


def test_start_ticks_are_not_a_fingerprint_off_linux(registry, monkeypatch):
    root, proc, _path, _first, second, _third, identity, write, _scan = registry
    monkeypatch.setattr(claude_registry.sys, "platform", "darwin")
    write(second, procStart=str(identity.start_ticks))
    assert claude_registry.registered_session(
        identity, str(root), proc_root=str(proc)) == (False, None)
