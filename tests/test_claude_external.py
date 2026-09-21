"""Zero-token regressions for Claude terminal ownership and takeover."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from claude_agent_sdk.types import ResultMessage, SystemMessage

from cc_remote.wrapper import claude_external as claude_external_module
from cc_remote.wrapper import machine as machine_module
from cc_remote.protocol import Query, Takeover, TurnBinding
from cc_remote.wrapper.claude_controls import ClaudeControls
from cc_remote.wrapper.claude_external import (
    claude_session_holders,
    classify_claude_growth,
)
from cc_remote.wrapper.codex_external import HolderScan, ProcessIdentity
from tests.test_multisession import _mk_ctx, _mk_machine


def _watch(path) -> dict:
    stat = path.stat()
    return {
        "path": str(path),
        "size": stat.st_size,
        "file_id": (stat.st_dev, stat.st_ino),
        "engine": "claude",
        "external_ts": 0.0,
        "external": False,
        "holders": set(),
        "takeover_pending": False,
        "scan_complete": True,
        "file_available": True,
    }


def _fake_process(
    root: Path,
    pid: int,
    start: int,
    *,
    parent_pid: int = 1,
    cwd: Path | None = None,
    cmdline: tuple[str, ...] = (),
    environ: tuple[str, ...] | None = None,
) -> Path:
    proc = root / str(pid)
    proc.mkdir(parents=True)
    fields = ["S"] + ["0"] * 18 + [str(start)] + ["0"] * 4
    fields[1] = str(parent_pid)
    (proc / "stat").write_text(f"{pid} (claude) " + " ".join(fields))
    (proc / "cmdline").write_bytes(
        b"\0".join(arg.encode() for arg in cmdline) + (b"\0" if cmdline else b""))
    if cwd is not None:
        (proc / "cwd").symlink_to(cwd)
    if environ is not None:
        (proc / "environ").write_bytes(
            b"\0".join(item.encode() for item in environ)
            + (b"\0" if environ else b"")
        )
    return proc


def test_claude_process_scan_scopes_identical_uuid_by_config_dir(tmp_path):
    native_id = "11111111-1111-4111-8111-111111111111"
    personal_sid = f"personal@{native_id}"
    company_sid = f"company@{native_id}"
    project = tmp_path / "project"
    project.mkdir()
    personal_root = tmp_path / "personal"
    company_root = tmp_path / "company"
    proc_root = tmp_path / "proc"
    _fake_process(
        proc_root,
        105,
        1005,
        cwd=project,
        cmdline=("/usr/local/bin/claude", "--resume", native_id),
        environ=(f"CLAUDE_CONFIG_DIR={company_root}",),
    )

    scan = claude_session_holders(
        {personal_sid: "personal.jsonl", company_sid: "company.jsonl"},
        {personal_sid: str(project), company_sid: str(project)},
        wrapper_pid=900,
        proc_root=str(proc_root),
        config_dirs={
            personal_sid: str(personal_root),
            company_sid: str(company_root),
        },
        native_session_ids={
            personal_sid: native_id,
            company_sid: native_id,
        },
        default_config_dir=str(personal_root),
    )

    assert scan.complete is True
    assert scan.holders[personal_sid] == set()
    assert scan.holders[company_sid] == {ProcessIdentity(105, 1005)}


def test_claude_profile_scan_fails_closed_when_environment_is_unreadable(
    tmp_path,
):
    native_id = "11111111-1111-4111-8111-111111111111"
    sid = f"personal@{native_id}"
    project = tmp_path / "project"
    project.mkdir()
    proc_root = tmp_path / "proc"
    _fake_process(
        proc_root,
        106,
        1006,
        cwd=project,
        cmdline=("claude", "--resume", native_id),
    )

    scan = claude_session_holders(
        {sid: "personal.jsonl"},
        {sid: str(project)},
        wrapper_pid=900,
        proc_root=str(proc_root),
        config_dirs={sid: str(tmp_path / "personal")},
        native_session_ids={sid: native_id},
        default_config_dir=str(tmp_path / "personal"),
    )

    assert scan.complete is False
    assert scan.holders[sid] == set()


def test_claude_process_scan_tracks_explicit_owner_only(tmp_path):
    sid = "11111111-1111-4111-8111-111111111111"
    sibling = "22222222-2222-4222-8222-222222222222"
    other = "33333333-3333-4333-8333-333333333333"
    project = tmp_path / "project"
    elsewhere = tmp_path / "elsewhere"
    project.mkdir()
    elsewhere.mkdir()
    proc_root = tmp_path / "proc"
    _fake_process(
        proc_root, 101, 1001, cwd=elsewhere,
        cmdline=("claude", "--resume", sid),
    )
    _fake_process(
        proc_root, 103, 1003, parent_pid=900, cwd=project,
        cmdline=("claude", "--resume", sibling),
    )
    _fake_process(
        proc_root, 104, 1004, cwd=elsewhere,
        cmdline=("claude", f"--session-id={sibling}"),
    )
    paths = {sid: "a", sibling: "b", other: "c"}
    scan = claude_session_holders(
        paths,
        {sid: str(project), sibling: str(project), other: str(elsewhere)},
        wrapper_pid=900,
        proc_root=str(proc_root),
    )

    assert scan.complete is True
    assert scan.holders[sid] == {ProcessIdentity(101, 1001)}
    assert scan.holders[sibling] == {ProcessIdentity(104, 1004)}
    assert scan.holders[other] == set()


def test_claude_process_scan_assigns_unqualified_owner_for_unique_cwd(tmp_path):
    sid = "11111111-1111-4111-8111-111111111111"
    project = tmp_path / "project"
    project.mkdir()
    proc_root = tmp_path / "proc"
    _fake_process(
        proc_root, 102, 1002, cwd=project,
        cmdline=("/usr/local/bin/claude",),
    )

    scan = claude_session_holders(
        {sid: "transcript"}, {sid: str(project)}, wrapper_pid=900,
        proc_root=str(proc_root),
    )

    assert scan.complete is True
    assert scan.holders[sid] == {ProcessIdentity(102, 1002)}


def test_claude_process_scan_bare_resume_uses_unique_cwd_fallback(tmp_path):
    sid = "11111111-1111-4111-8111-111111111111"
    project = tmp_path / "project"
    project.mkdir()
    proc_root = tmp_path / "proc"
    commands = (
        ("/usr/local/bin/claude", "--resume"),
        ("/usr/local/bin/claude", "-r"),
        ("/usr/local/bin/claude", "--resume="),
    )
    for offset, command in enumerate(commands):
        _fake_process(
            proc_root, 102 + offset, 1002 + offset,
            cwd=project, cmdline=command,
        )

    scan = claude_session_holders(
        {sid: "transcript"}, {sid: str(project)}, wrapper_pid=900,
        proc_root=str(proc_root),
    )

    assert scan.complete is True
    assert scan.holders[sid] == {
        ProcessIdentity(102, 1002),
        ProcessIdentity(103, 1003),
        ProcessIdentity(104, 1004),
    }


def test_claude_process_scan_never_reassigns_explicit_unknown_session(tmp_path):
    sid = "11111111-1111-4111-8111-111111111111"
    unknown = "99999999-9999-4999-8999-999999999999"
    project = tmp_path / "project"
    project.mkdir()
    proc_root = tmp_path / "proc"
    _fake_process(
        proc_root, 102, 1002, cwd=project,
        cmdline=("/usr/local/bin/claude", "--resume", unknown),
    )

    scan = claude_session_holders(
        {sid: "transcript"}, {sid: str(project)}, wrapper_pid=900,
        proc_root=str(proc_root),
    )

    assert scan.complete is True
    assert scan.holders[sid] == set()


def test_claude_process_scan_does_not_fan_out_ambiguous_cwd_owner(tmp_path):
    sid = "11111111-1111-4111-8111-111111111111"
    sibling = "22222222-2222-4222-8222-222222222222"
    project = tmp_path / "project"
    project.mkdir()
    proc_root = tmp_path / "proc"
    _fake_process(
        proc_root, 102, 1002, cwd=project,
        cmdline=("/usr/local/bin/claude",),
    )

    scan = claude_session_holders(
        {sid: "a", sibling: "b"},
        {sid: str(project), sibling: str(project)},
        wrapper_pid=900,
        proc_root=str(proc_root),
    )

    assert scan.complete is True
    assert scan.holders == {sid: set(), sibling: set()}


def test_claude_continue_uses_native_latest_and_sticks_until_process_exit(
    tmp_path,
):
    older = "11111111-1111-4111-8111-111111111111"
    latest = "22222222-2222-4222-8222-222222222222"
    project = tmp_path / "project"
    project.mkdir()
    older_path = tmp_path / "older.jsonl"
    latest_path = tmp_path / "latest.jsonl"
    older_path.write_bytes(b"older\n")
    latest_path.write_bytes(b"latest\n")
    proc_root = tmp_path / "proc"
    _fake_process(
        proc_root, 150, 1500, cwd=project,
        cmdline=("/usr/local/bin/claude", "-c"),
    )
    identity = ProcessIdentity(150, 1500)
    bindings: dict[ProcessIdentity, str] = {}
    paths = {older: str(older_path), latest: str(latest_path)}
    cwds = {older: str(project), latest: str(project)}
    resolved = []

    def resolver(cwd):
        resolved.append(cwd)
        return latest

    first = claude_session_holders(
        paths, cwds, wrapper_pid=900, proc_root=str(proc_root),
        continue_bindings=bindings,
        continue_resolver=resolver,
    )
    assert first.complete is True
    assert first.holders == {older: set(), latest: {identity}}
    assert bindings == {identity: latest}
    assert resolved == [str(project)]

    # Native catalog changes do not move a still-running process after its
    # startup selection has been recorded.
    second = claude_session_holders(
        paths, cwds, wrapper_pid=900, proc_root=str(proc_root),
        continue_bindings=bindings,
        continue_resolver=lambda _cwd: older,
    )
    assert second.holders == {older: set(), latest: {identity}}
    assert bindings == {identity: latest}
    assert resolved == [str(project)]

    empty_proc = tmp_path / "empty-proc"
    empty_proc.mkdir()
    exited = claude_session_holders(
        paths, cwds, wrapper_pid=900, proc_root=str(empty_proc),
        continue_bindings=bindings,
        continue_resolver=resolver,
    )
    assert exited.complete is True
    assert bindings == {}


def test_claude_continue_never_guesses_from_old_watched_subset(tmp_path):
    older = "11111111-1111-4111-8111-111111111111"
    newer = "22222222-2222-4222-8222-222222222222"
    project = tmp_path / "project"
    project.mkdir()
    proc_root = tmp_path / "proc"
    _fake_process(
        proc_root, 151, 1501, cwd=project,
        cmdline=("/usr/local/bin/claude", "--continue"),
    )
    identity = ProcessIdentity(151, 1501)
    bindings: dict[ProcessIdentity, str] = {}
    candidates: dict[ProcessIdentity, str] = {}
    calls = []

    def resolver(cwd):
        calls.append(cwd)
        return newer

    old_only = claude_session_holders(
        {older: "old.jsonl"}, {older: str(project)}, wrapper_pid=900,
        proc_root=str(proc_root), continue_bindings=bindings,
        continue_candidates=candidates,
        continue_resolver=resolver,
    )
    assert old_only.complete is True
    assert old_only.holders == {older: set()}
    assert bindings == {}
    assert candidates == {identity: newer}

    both = claude_session_holders(
        {older: "old.jsonl", newer: "new.jsonl"},
        {older: str(project), newer: str(project)}, wrapper_pid=900,
        proc_root=str(proc_root), continue_bindings=bindings,
        continue_candidates=candidates,
        continue_resolver=resolver,
    )
    assert both.complete is True
    assert both.holders == {older: set(), newer: {identity}}
    assert bindings == {identity: newer}
    assert calls == [str(project)]


def test_claude_continue_catalog_failure_is_incomplete_not_old_owner(tmp_path):
    older = "11111111-1111-4111-8111-111111111111"
    project = tmp_path / "project"
    project.mkdir()
    proc_root = tmp_path / "proc"
    _fake_process(
        proc_root, 152, 1502, cwd=project,
        cmdline=("/usr/local/bin/claude", "-c"),
    )
    bindings: dict[ProcessIdentity, str] = {}
    candidates: dict[ProcessIdentity, str] = {}

    def failed_catalog(_cwd):
        raise RuntimeError("catalog unavailable")

    scan = claude_session_holders(
        {older: "old.jsonl"}, {older: str(project)}, wrapper_pid=900,
        proc_root=str(proc_root), continue_bindings=bindings,
        continue_candidates=candidates,
        continue_resolver=failed_catalog,
    )
    assert scan.complete is False
    assert scan.holders == {older: set()}
    assert bindings == {}
    assert candidates == {}


def test_claude_continue_empty_catalog_retries_without_caching(tmp_path):
    session_id = "11111111-1111-4111-8111-111111111111"
    project = tmp_path / "project"
    project.mkdir()
    proc_root = tmp_path / "proc"
    _fake_process(
        proc_root, 153, 1503, cwd=project,
        cmdline=("/usr/local/bin/claude", "--continue"),
    )
    identity = ProcessIdentity(153, 1503)
    bindings: dict[ProcessIdentity, str] = {}
    candidates: dict[ProcessIdentity, str] = {}
    results = iter((None, session_id))
    calls = []

    def racing_catalog(cwd):
        calls.append(cwd)
        return next(results)

    first = claude_session_holders(
        {session_id: "session.jsonl"}, {session_id: str(project)},
        wrapper_pid=900, proc_root=str(proc_root),
        continue_bindings=bindings,
        continue_candidates=candidates,
        continue_resolver=racing_catalog,
    )
    assert first.complete is False
    assert first.holders == {session_id: set()}
    assert bindings == {}
    assert candidates == {}

    second = claude_session_holders(
        {session_id: "session.jsonl"}, {session_id: str(project)},
        wrapper_pid=900, proc_root=str(proc_root),
        continue_bindings=bindings,
        continue_candidates=candidates,
        continue_resolver=racing_catalog,
    )
    assert second.complete is True
    assert second.holders == {session_id: {identity}}
    assert bindings == {identity: session_id}
    assert candidates == {identity: session_id}
    assert calls == [str(project), str(project)]


def test_prime_claude_ownership_resolves_against_all_watched_sessions(
    tmp_path, monkeypatch,
):
    async def go():
        machine, _ = _mk_machine()
        project = tmp_path / "project"
        project.mkdir()
        first_path = tmp_path / "first.jsonl"
        second_path = tmp_path / "second.jsonl"
        first_path.write_bytes(b"")
        second_path.write_bytes(b"")
        machine._watch["first"] = {
            **_watch(first_path), "cwd": str(project),
        }
        machine._watch["second"] = {
            **_watch(second_path), "cwd": str(project),
        }
        observed = []

        async def probe(paths, cwds):
            observed.append((dict(paths), dict(cwds)))
            return HolderScan(
                {"first": set(), "second": {ProcessIdentity(160, 1600)}},
                True,
            )

        monkeypatch.setattr(machine, "_probe_claude_holders", probe)

        assert await machine._prime_claude_ownership("first") is False
        assert set(observed[0][0]) == {"first", "second"}
        assert set(observed[0][1]) == {"first", "second"}

    asyncio.run(go())


def test_claude_process_scan_recognizes_native_installer_and_ignores_daemon(
    tmp_path,
):
    sid = "11111111-1111-4111-8111-111111111111"
    project = tmp_path / "project"
    project.mkdir()
    proc_root = tmp_path / "proc"
    native = "/home/nancy/.local/share/claude/versions/2.1.205"
    _fake_process(
        proc_root, 201, 2001, cwd=project, cmdline=(native,),
    )
    _fake_process(
        proc_root, 202, 2002, cwd=project,
        cmdline=("/home/nancy/.local/bin/claude", "daemon", "run"),
    )
    _fake_process(
        proc_root, 203, 2003, cwd=project,
        cmdline=(native, "--bg-spare", "/tmp/spare.claim.sock"),
    )

    scan = claude_session_holders(
        {sid: "transcript"}, {sid: str(project)}, wrapper_pid=900,
        proc_root=str(proc_root),
    )

    assert scan.complete is True
    assert scan.holders[sid] == {ProcessIdentity(201, 2001)}


def test_claude_process_scan_fails_closed_without_proc(tmp_path):
    scan = claude_session_holders(
        {"sid": "unused"}, {"sid": str(tmp_path)}, wrapper_pid=900,
        proc_root=str(tmp_path / "missing-proc"),
    )
    assert scan.complete is False and scan.holders == {"sid": set()}


def _darwin_scan(monkeypatch, processes, cwds, identities=None,
                 cwd_complete=True):
    monkeypatch.setattr(claude_external_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        claude_external_module, "darwin_process_snapshot",
        lambda: (processes, True),
    )
    monkeypatch.setattr(
        claude_external_module, "_darwin_process_cwds",
        lambda _pids: (cwds, cwd_complete),
    )
    stable = identities or {info[0].pid: info[0] for info in processes}
    monkeypatch.setattr(
        claude_external_module, "process_identity",
        lambda pid: stable.get(pid),
    )


def test_claude_scan_on_darwin_binds_explicit_session_before_growth(
        monkeypatch):
    identity = ProcessIdentity(201, 2001)
    sid = "session-explicit"
    _darwin_scan(monkeypatch, [
        (identity, 1, 1, (b"/Applications/Claude.app/Contents/MacOS/claude",
                          b"--resume", sid.encode())),
    ], {}, cwd_complete=False)
    scan = claude_session_holders(
        {sid: "unused"}, {sid: "/tmp/project"},
        wrapper_pid=900, proc_root="/proc",
    )
    assert scan.complete is True
    assert scan.holders[sid] == {identity}


def test_claude_scan_on_darwin_unknown_explicit_sid_never_uses_cwd(
        monkeypatch):
    identity = ProcessIdentity(202, 2002)
    _darwin_scan(monkeypatch, [
        (identity, 1, 1, (b"claude", b"--resume", b"not-watched")),
    ], {202: "/tmp/project"})
    scan = claude_session_holders(
        {"watched": "unused"}, {"watched": "/tmp/project"},
        wrapper_pid=900, proc_root="/proc",
    )
    assert scan.complete is True
    assert scan.holders == {"watched": set()}


def test_claude_scan_on_darwin_does_not_guess_same_cwd_siblings(monkeypatch):
    identity = ProcessIdentity(203, 2003)
    _darwin_scan(monkeypatch, [
        (identity, 1, 1, (b"claude",)),
    ], {203: "/tmp/project"})
    scan = claude_session_holders(
        {"first": "a", "second": "b"},
        {"first": "/tmp/project", "second": "/tmp/project"},
        wrapper_pid=900, proc_root="/proc",
    )
    assert scan.complete is True
    assert scan.holders == {"first": set(), "second": set()}


def test_claude_scan_on_darwin_excludes_sdk_grandchild_and_background_role(
        monkeypatch):
    child = ProcessIdentity(210, 2010)
    grandchild = ProcessIdentity(211, 2011)
    background = ProcessIdentity(212, 2012)
    sid = "owned-by-wrapper"
    _darwin_scan(monkeypatch, [
        (child, 900, 0, (b"python", b"sdk-host")),
        (grandchild, 210, 1, (b"claude", b"--resume", sid.encode())),
        (background, 1, 0, (b"claude", b"daemon", b"run")),
    ], {211: "/tmp/project", 212: "/tmp/project"})
    scan = claude_session_holders(
        {sid: "unused"}, {sid: "/tmp/project"},
        wrapper_pid=900, proc_root="/proc",
    )
    assert scan.complete is True
    assert scan.holders == {sid: set()}


def test_claude_scan_on_darwin_fails_closed_when_ps_or_lsof_is_unavailable(
        monkeypatch):
    monkeypatch.setattr(claude_external_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        claude_external_module, "darwin_process_snapshot",
        lambda: ([], False),
    )
    unavailable_ps = claude_session_holders(
        {"sid": "unused"}, {"sid": "/tmp/project"},
        wrapper_pid=900, proc_root="/proc",
    )
    assert unavailable_ps.complete is False

    identity = ProcessIdentity(220, 2020)
    _darwin_scan(monkeypatch, [
        (identity, 1, 1, (b"claude",)),
    ], {}, cwd_complete=False)
    unavailable_lsof = claude_session_holders(
        {"sid": "unused"}, {"sid": "/tmp/project"},
        wrapper_pid=900, proc_root="/proc",
    )
    assert unavailable_lsof.complete is False


def test_claude_scan_on_darwin_rejects_reused_pid(monkeypatch):
    identity = ProcessIdentity(230, 2030)
    replacement = ProcessIdentity(230, 9999)
    _darwin_scan(monkeypatch, [
        (identity, 1, 1, (b"claude", b"--resume", b"sid")),
    ], {}, identities={230: replacement})
    scan = claude_session_holders(
        {"sid": "unused"}, {"sid": "/tmp/project"},
        wrapper_pid=900, proc_root="/proc",
    )
    assert scan.complete is False
    assert scan.holders == {"sid": set()}


def test_claude_takeover_signals_only_same_identity_and_uid(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        identity = ProcessIdentity(240, 2040)
        calls = 0

        def current(_pid):
            nonlocal calls
            calls += 1
            return identity if calls <= 2 else None

        signals = []
        monkeypatch.setattr(machine_module, "process_identity", current)
        monkeypatch.setattr(
            machine_module, "process_owner_uid", lambda _pid: machine_module.os.getuid())
        monkeypatch.setattr(
            machine_module.os, "kill", lambda pid, sig: signals.append((pid, sig)))

        remaining = await machine._terminate_external_claude_holders(
            {identity}, timeout=0.1)
        assert remaining == set()
        assert signals == [(identity.pid, machine_module.signal.SIGTERM)]

    asyncio.run(run())


def test_claude_takeover_rejects_pid_reuse_and_unknown_uid(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        identity = ProcessIdentity(241, 2041)
        replacement = ProcessIdentity(241, 9999)
        signals = []
        monkeypatch.setattr(
            machine_module.os, "kill", lambda pid, sig: signals.append((pid, sig)))

        monkeypatch.setattr(
            machine_module, "process_identity", lambda _pid: replacement)
        reused = await machine._terminate_external_claude_holders(
            {identity}, timeout=0.1)
        assert reused == set()

        monkeypatch.setattr(
            machine_module, "process_identity", lambda _pid: identity)
        monkeypatch.setattr(
            machine_module, "process_owner_uid", lambda _pid: None)
        unknown_uid = await machine._terminate_external_claude_holders(
            {identity}, timeout=0.1)
        assert unknown_uid == {identity}
        assert signals == []

    asyncio.run(run())


class _ClaudeRunSdk:
    effort = "max"
    applied_effort = "max"
    model = "claude-mythos-5"
    permission_mode = "bypassPermissions"
    next_turn_id = None

    def __init__(self):
        self.queries = 0
        self.reconnects = 0
        self.reconnect_args = []
        self.auto_compact_mode = "custom"
        self.auto_compact_threshold_tokens = 500_000
        self.applied_auto_compact_mode = "custom"
        self.applied_auto_compact_threshold_tokens = 500_000

    async def query(self, _prompt):
        self.queries += 1

    async def receive_response(self):
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="sid",
        )

    async def force_reconnect(self, **kwargs):
        self.reconnects += 1
        self.reconnect_args.append(kwargs)
        self.applied_effort = self.effort

    async def refresh_goal(self, _sid):
        return None

    def observe_goal_message(self, _message, _sid):
        return False, None

    def release_background_messages(self):
        return None


class _RejectAfterWriteSdk(_ClaudeRunSdk):
    def __init__(self, path: Path):
        super().__init__()
        self.path = path

    async def query(self, _prompt):
        self.queries += 1
        self.path.write_bytes(b'{"type":"ambiguous-write"}\n')
        raise RuntimeError("query rejected after a possible partial send")


class _FailedMessagePumpSdk(_ClaudeRunSdk):
    def __init__(self):
        super().__init__()
        self.message_pump_failed = False
        self.fail_response = True

    async def receive_response(self):
        if self.fail_response:
            self.message_pump_failed = True
            raise RuntimeError("Claude SDK message pump stopped")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="sid",
        )

    async def force_reconnect(self, **kwargs):
        await super().force_reconnect(**kwargs)
        self.message_pump_failed = False


class _FailedControlPlaneSdk(_ClaudeRunSdk):
    def __init__(self):
        super().__init__()
        self.control_plane_failed = True

    async def force_reconnect(self, **kwargs):
        await super().force_reconnect(**kwargs)
        self.control_plane_failed = False


def test_claude_init_upstream_model_never_overwrites_selected_alias():
    class GatewaySdk(_ClaudeRunSdk):
        model = "claude-mythos-5[1m]"

        async def receive_response(self):
            yield SystemMessage(subtype="init", data={
                "session_id": "sid",
                "model": "claude-opus-4-8",
            })
            yield ResultMessage(
                subtype="success", duration_ms=1, duration_api_ms=1,
                is_error=False, num_turns=1, session_id="sid")

    async def go():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.sdk = GatewaySdk()
        ctx.state = "running"
        ctx.active_msg_id = "message-1"
        ctx.announced_model = ctx.sdk.model
        machine.sessions[ctx.key] = ctx

        await machine._run_turn(ctx, "hello")

        assert ctx.sdk.model == "claude-mythos-5[1m]"
        assert all(getattr(event, "model", None) != "claude-opus-4-8"
                   for event in transport.sent)

    asyncio.run(go())


def test_claude_owner_does_not_expire_on_an_activity_ttl(tmp_path):
    machine, _ = _mk_machine()
    path = tmp_path / "session.jsonl"
    path.write_bytes(b"")
    watch = _watch(path)
    watch["external"] = True
    watch["holders"] = {ProcessIdentity(101, 1001)}
    machine._watch["sid"] = watch

    machine.EXTERNAL_TTL = 0

    assert machine._is_external("sid") is True


def test_external_holder_wins_over_running_wrapper_write_heuristic(tmp_path):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "session.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.state = "running"
        machine.sessions["sid"] = ctx
        watch = _watch(path)
        machine._watch["sid"] = watch
        machine._push_mirrored_history = lambda _sid: asyncio.sleep(0)

        holder = ProcessIdentity(102, 1002)
        path.write_bytes(b'{"type":"user"}\n')
        await machine._poll_claude_watch(
            "sid", watch, {holder}, 1000.0,
            ownership_scan_complete=True,
        )

        assert machine._is_external("sid") is True
        assert watch["holders"] == {holder}
        assert ctx.needs_reload is True

    asyncio.run(go())


def test_only_an_active_wrapper_query_can_attribute_growth_as_own(tmp_path):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "session.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.state = "running"
        ctx.claude_write_active = True
        machine.sessions["sid"] = ctx
        watch = _watch(path)
        machine._watch["sid"] = watch
        mirrored = []

        async def mirror(sid):
            mirrored.append(sid)

        machine._push_mirrored_history = mirror
        path.write_bytes(b'{"type":"user"}\n')
        await machine._poll_claude_watch(
            "sid", watch, set(), 1000.0,
            ownership_scan_complete=True,
        )

        assert machine._is_external("sid") is False
        assert ctx.needs_reload is False
        assert mirrored == []

    asyncio.run(go())


def test_initial_claude_owner_scan_does_not_mirror_active_managed_turn(
    tmp_path,
):
    """The first fail-closed scan may unlock control, not replay partial history."""
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "session.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.state = "running"
        ctx.claude_write_active = True
        machine.sessions["sid"] = ctx
        watch = _watch(path)
        watch["scan_complete"] = False
        machine._watch["sid"] = watch
        mirrored = []

        async def mirror(sid):
            mirrored.append(sid)

        machine._push_mirrored_history = mirror
        await machine._poll_claude_watch(
            "sid", watch, set(), 1000.0,
            ownership_scan_complete=True,
        )

        assert watch["scan_complete"] is True
        assert machine._is_external("sid") is False
        assert mirrored == []

    asyncio.run(go())


def test_initial_claude_owner_scan_still_mirrors_idle_unlock(tmp_path):
    """An idle browser must still receive the ownership transition promptly."""
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "session.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        machine.sessions["sid"] = ctx
        watch = _watch(path)
        watch["scan_complete"] = False
        machine._watch["sid"] = watch
        mirrored = []

        async def mirror(sid):
            mirrored.append(sid)

        machine._push_mirrored_history = mirror
        await machine._poll_claude_watch(
            "sid", watch, set(), 1000.0,
            ownership_scan_complete=True,
        )

        assert machine._is_external("sid") is False
        assert mirrored == ["sid"]

    asyncio.run(go())


def test_delayed_sdk_rows_remain_owned_after_result(tmp_path):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "session.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        machine.sessions["sid"] = ctx
        watch = _watch(path)
        machine._watch["sid"] = watch
        mirrored = []

        async def mirror(sid):
            mirrored.append(sid)

        machine._push_mirrored_history = mirror
        assistant_id = "11111111-1111-4111-8111-111111111111"
        with path.open("ab") as stream:
            stream.write(
                ("{\"type\":\"assistant\",\"entrypoint\":\"sdk-py\","
                 f"\"uuid\":\"{assistant_id}\"}}\n").encode()
            )
        await machine._poll_claude_watch(
            "sid", watch, set(), 1000.0,
            ownership_scan_complete=True,
        )

        # Claude flushes these after ResultMessage and after the wrapper has
        # already cleared claude_write_active. The leaf UUID still proves that
        # the metadata belongs to the SDK-authored assistant row above.
        with path.open("ab") as stream:
            stream.write(
                (
                    "{\"type\":\"last-prompt\","
                    f"\"leafUuid\":\"{assistant_id}\"}}\n"
                    "{\"type\":\"ai-title\","
                    "\"aiTitle\":\"Owned title\"}\n"
                    "{\"type\":\"mode\",\"mode\":\"normal\"}\n"
                ).encode()
            )
        await machine._poll_claude_watch(
            "sid", watch, set(), 1001.0,
            ownership_scan_complete=True,
        )

        assert ctx.claude_write_active is False
        assert ctx.needs_reload is False
        assert machine._is_external("sid") is False
        assert mirrored == []

    asyncio.run(go())


def test_explicit_cli_row_wins_during_wrapper_write(tmp_path):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "session.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.claude_write_active = True
        machine.sessions["sid"] = ctx
        watch = _watch(path)
        machine._watch["sid"] = watch
        machine._push_mirrored_history = lambda _sid: asyncio.sleep(0)

        path.write_bytes(
            b'{"type":"user","entrypoint":"cli","uuid":"external"}\n')
        await machine._poll_claude_watch(
            "sid", watch, set(), 1000.0,
            ownership_scan_complete=True,
        )

        assert ctx.needs_reload is True

    asyncio.run(go())


def test_managed_claude_transcript_user_binds_before_sdk_replay(
    monkeypatch, tmp_path,
):
    async def go():
        session_id = "f6cd73f7-86d7-4115-8512-3cf357fbd542"
        native_id = "759d1121-1009-4882-8218-b31296d7e20b"
        client_id = "6b09ee37-f861-4422-b98a-21f509c951b0"
        path = tmp_path / f"{session_id}.jsonl"
        path.write_bytes(b"")
        machine, transport = _mk_machine()
        ctx = _mk_ctx(session_id, session_id)
        ctx.state = "running"
        ctx.active_msg_id = client_id
        ctx.claude_write_active = True
        machine.sessions[ctx.key] = ctx
        watch = _watch(path)
        machine._watch[session_id] = watch
        monkeypatch.setattr(
            machine_module, "transcript_path", lambda _sid: str(path))
        machine._start_claude_client_alias_probe(ctx)

        rows = [
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "content": "same prompt",
                "sessionId": session_id,
            },
            {
                "type": "user",
                "uuid": native_id,
                "sessionId": session_id,
                "entrypoint": "sdk-py",
                "promptSource": "sdk",
                "isSidechain": False,
                "message": {"role": "user", "content": "same prompt"},
            },
        ]
        path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )
        await machine._poll_claude_watch(
            session_id,
            watch,
            set(),
            1000.0,
            ownership_scan_complete=True,
        )

        assert machine._claude_client_messages.get(
            session_id, path) == {native_id: client_id}
        bindings = [
            message for message in transport.sent
            if isinstance(message, TurnBinding)
        ]
        assert [(item.msg_id, item.turn_id) for item in bindings] == [
            (client_id, native_id)
        ]
        assert ctx.state == "running"

    asyncio.run(go())


def test_managed_claude_alias_probe_waits_for_complete_jsonl_row(
    monkeypatch, tmp_path,
):
    async def go():
        session_id = "f6cd73f7-86d7-4115-8123-111111111111"
        native_id = "759d1121-1009-4882-8218-222222222222"
        client_id = "6b09ee37-f861-4422-b98a-333333333333"
        path = tmp_path / f"{session_id}.jsonl"
        path.write_bytes(b"")
        machine, transport = _mk_machine()
        ctx = _mk_ctx(session_id, session_id)
        ctx.state = "running"
        ctx.active_msg_id = client_id
        ctx.claude_write_active = True
        machine.sessions[ctx.key] = ctx
        watch = _watch(path)
        machine._watch[session_id] = watch
        monkeypatch.setattr(
            machine_module, "transcript_path", lambda _sid: str(path))
        machine._start_claude_client_alias_probe(ctx)

        row = (json.dumps({
            "type": "user",
            "uuid": native_id,
            "sessionId": session_id,
            "entrypoint": "sdk-py",
            "message": {"role": "user", "content": "hello"},
        }) + "\n").encode()
        split = len(row) // 2
        path.write_bytes(row[:split])
        await machine._poll_claude_watch(
            session_id, watch, set(), 1000.0,
            ownership_scan_complete=True,
        )
        assert machine._claude_client_messages.get(session_id, path) == {}
        assert not [
            message for message in transport.sent
            if isinstance(message, TurnBinding)
        ]

        with path.open("ab") as stream:
            stream.write(row[split:])
        await machine._poll_claude_watch(
            session_id, watch, set(), 1001.0,
            ownership_scan_complete=True,
        )
        assert machine._claude_client_messages.get(
            session_id, path) == {native_id: client_id}
        assert [
            (message.msg_id, message.turn_id)
            for message in transport.sent
            if isinstance(message, TurnBinding)
        ] == [(client_id, native_id)]

    asyncio.run(go())


def test_managed_claude_alias_probe_skips_tool_result_user_envelope(
    monkeypatch, tmp_path,
):
    async def go():
        session_id = "f6cd73f7-86d7-4115-8123-444444444444"
        native_id = "759d1121-1009-4882-8218-555555555555"
        client_id = "6b09ee37-f861-4422-b98a-666666666666"
        path = tmp_path / f"{session_id}.jsonl"
        path.write_bytes(b"")
        machine, transport = _mk_machine()
        ctx = _mk_ctx(session_id, session_id)
        ctx.state = "running"
        ctx.active_msg_id = client_id
        ctx.claude_write_active = True
        machine.sessions[ctx.key] = ctx
        watch = _watch(path)
        machine._watch[session_id] = watch
        monkeypatch.setattr(
            machine_module, "transcript_path", lambda _sid: str(path))
        machine._start_claude_client_alias_probe(ctx)

        tool_result = {
            "type": "user",
            "uuid": "tool-result-envelope",
            "entrypoint": "sdk-py",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "tool-1"}],
            },
        }
        path.write_text(json.dumps(tool_result) + "\n", encoding="utf-8")
        await machine._poll_claude_watch(
            session_id, watch, set(), 1000.0,
            ownership_scan_complete=True,
        )
        assert ctx.claude_client_alias_probe is not None
        assert not [
            message for message in transport.sent
            if isinstance(message, TurnBinding)
        ]

        real_user = {
            "type": "user",
            "uuid": native_id,
            "promptSource": "sdk",
            "message": {"role": "user", "content": "hello"},
        }
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(real_user) + "\n")
        await machine._poll_claude_watch(
            session_id, watch, set(), 1001.0,
            ownership_scan_complete=True,
        )
        assert machine._claude_client_messages.get(
            session_id, path) == {native_id: client_id}
        assert [
            (message.msg_id, message.turn_id)
            for message in transport.sent
            if isinstance(message, TurnBinding)
        ] == [(client_id, native_id)]

    asyncio.run(go())


def test_managed_claude_alias_probe_rejects_transcript_inode_replacement(
    monkeypatch, tmp_path,
):
    async def go():
        session_id = "f6cd73f7-86d7-4115-8123-777777777777"
        native_id = "759d1121-1009-4882-8218-888888888888"
        client_id = "6b09ee37-f861-4422-b98a-999999999999"
        path = tmp_path / f"{session_id}.jsonl"
        path.write_bytes(b"")
        machine, transport = _mk_machine()
        ctx = _mk_ctx(session_id, session_id)
        ctx.state = "running"
        ctx.active_msg_id = client_id
        ctx.claude_write_active = True
        machine.sessions[ctx.key] = ctx
        watch = _watch(path)
        machine._watch[session_id] = watch
        machine._push_mirrored_history = lambda _sid: asyncio.sleep(0)
        monkeypatch.setattr(
            machine_module, "transcript_path", lambda _sid: str(path))
        machine._start_claude_client_alias_probe(ctx)

        replacement = tmp_path / "replacement.jsonl"
        replacement.write_text(json.dumps({
            "type": "user",
            "uuid": native_id,
            "entrypoint": "sdk-py",
            "message": {"role": "user", "content": "hello"},
        }) + "\n", encoding="utf-8")
        replacement.replace(path)
        await machine._poll_claude_watch(
            session_id, watch, set(), 1000.0,
            ownership_scan_complete=True,
        )

        assert ctx.claude_client_alias_probe is None
        assert machine._claude_client_messages.get(session_id, path) == {}
        assert not [
            message for message in transport.sent
            if isinstance(message, TurnBinding)
        ]

    asyncio.run(go())


def test_claude_growth_classifier_rejects_partial_jsonl():
    assert classify_claude_growth(
        b'{"type":"assistant","entrypoint":"sdk-py"'
    ) == ("unknown", ())


@pytest.mark.parametrize("entrypoint, expected", [("sdk-py", "sdk"), ("cli", "external"), (None, "external")])
def test_injected_system_prompt_keeps_its_native_process_provenance(entrypoint, expected):
    row = {"type": "user", "uuid": "injected", "entrypoint": entrypoint,
           "promptSource": "system", "origin": {"kind": "task-notification"}}
    origin, owned = classify_claude_growth((json.dumps(row) + "\n").encode())
    assert origin == expected
    assert owned == (("injected",) if expected == "sdk" else ())


def test_claude_growth_classifier_treats_atis_latch_as_neutral_metadata():
    assistant_id = "11111111-1111-4111-8111-111111111111"
    origin, owned = classify_claude_growth(
        (
            '{"type":"assistant","entrypoint":"sdk-py",'
            f'"uuid":"{assistant_id}"}}\n'
            '{"type":"atis-latch","atis":"","sessionId":"sid"}\n'
        ).encode()
    )

    assert origin == "sdk"
    assert owned == (assistant_id,)


def test_claude_growth_classifier_treats_ai_title_as_neutral_metadata():
    assistant_id = "11111111-1111-4111-8111-111111111111"
    origin, owned = classify_claude_growth(
        (
            '{"type":"assistant","entrypoint":"sdk-py",'
            f'"uuid":"{assistant_id}"}}\n'
            '{"type":"ai-title","aiTitle":"Generated title",'
            '"sessionId":"sid"}\n'
        ).encode()
    )

    assert origin == "sdk"
    assert owned == (assistant_id,)
    assert classify_claude_growth(
        b'{"type":"ai-title","aiTitle":"Unattributed"}\n'
    ) == ("unknown", ())


def test_growth_after_a_finished_wrapper_turn_is_never_hidden_by_a_ttl(
    tmp_path,
):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "session.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        invalidations = 0

        def invalidate_context_usage_cache():
            nonlocal invalidations
            invalidations += 1

        ctx.sdk = SimpleNamespace(
            invalidate_context_usage_cache=invalidate_context_usage_cache)
        # Recreate the removed legacy grace marker: even if a future change
        # restores it, recent turn completion must not hide unknown growth.
        ctx.last_turn_end = time.time()
        machine.sessions["sid"] = ctx
        watch = _watch(path)
        machine._watch["sid"] = watch
        machine._push_mirrored_history = lambda _sid: asyncio.sleep(0)

        path.write_bytes(b'{"type":"external-user"}\n')
        await machine._poll_claude_watch(
            "sid", watch, set(), 1000.0,
            ownership_scan_complete=True,
        )

        # The short-lived terminal has already exited, so Remote need not stay
        # read-only; it must still reload the externally-advanced transcript.
        assert machine._is_external("sid") is False
        assert ctx.needs_reload is True
        assert invalidations == 1

    asyncio.run(go())


def test_incomplete_claude_scan_is_fail_closed(tmp_path, monkeypatch):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "session.jsonl"
        path.write_bytes(b"")
        machine._watch["sid"] = _watch(path)

        async def probe(_paths, _cwds):
            return HolderScan({"sid": set()}, False)

        monkeypatch.setattr(
            machine, "_probe_claude_holders", probe, raising=False)

        assert await machine._prime_claude_ownership("sid") is True
        assert machine._watch["sid"]["scan_complete"] is False

    asyncio.run(go())


def test_missing_claude_transcript_is_fail_closed(tmp_path, monkeypatch):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "session.jsonl"
        path.write_bytes(b"")
        watch = _watch(path)
        machine._watch["sid"] = watch
        path.unlink()

        async def probe(_paths, _cwds):
            return HolderScan({"sid": set()}, True)

        monkeypatch.setattr(
            machine, "_probe_claude_holders", probe, raising=False)

        assert await machine._prime_claude_ownership("sid") is True
        assert watch["file_available"] is False

    asyncio.run(go())


def test_query_is_rejected_before_state_claim_when_claude_is_external(monkeypatch):
    async def go():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.sdk = _ClaudeRunSdk()
        machine.sessions["sid"] = ctx
        monkeypatch.setattr(machine, "_watch_session", lambda _sid: None)

        async def occupied(_sid):
            return True

        monkeypatch.setattr(
            machine, "_prime_claude_ownership", occupied, raising=False)
        result = await machine._handle_query(Query(
            sid="sid", prompt="hello", msg_id="msg-1"))
        if ctx.turn_task is not None:
            ctx.turn_task.cancel()
            await asyncio.gather(ctx.turn_task, return_exceptions=True)

        assert result.code == "busy" and result.msg_id == "msg-1"
        assert ctx.state == "idle" and ctx.turn_task is None
        assert transport.sent[-1] is result

    asyncio.run(go())


def test_final_preflight_closes_the_watcher_poll_window(monkeypatch):
    async def go():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.state = "running"
        ctx.active_msg_id = "msg-final"
        sdk = _ClaudeRunSdk()
        ctx.sdk = sdk
        machine.sessions["sid"] = ctx

        async def occupied(_sid):
            return True

        monkeypatch.setattr(
            machine, "_prime_claude_ownership", occupied, raising=False)
        await machine._run_turn(ctx, "hello")

        assert sdk.queries == 0 and ctx.state == "idle"
        assert any(getattr(event, "code", None) == "busy"
                   and event.msg_id == "msg-final" for event in transport.sent)

    asyncio.run(go())


def test_claude_takeover_explicitly_migrates_the_terminal_process(
    tmp_path, monkeypatch,
):
    async def go():
        machine, transport = _mk_machine()
        path = tmp_path / "session.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.sdk = _ClaudeRunSdk()
        machine.sessions["sid"] = ctx
        holder = ProcessIdentity(103, 1003)
        watch = _watch(path)
        watch.update({"external": True, "holders": {holder}})
        machine._watch["sid"] = watch

        async def probe(_paths, _cwds):
            return HolderScan({"sid": {holder}}, True)

        async def terminate(holders):
            assert holders == {holder}
            return set()

        monkeypatch.setattr(
            machine, "_probe_claude_holders", probe, raising=False)
        monkeypatch.setattr(
            machine, "_terminate_external_claude_holders", terminate,
            raising=False)
        result = await machine._handle_takeover(Takeover(
            sid="sid", cmd_id="takeover-1"))

        assert result is None
        assert watch["takeover_pending"] is False
        assert machine._is_external("sid") is False
        assert ctx.needs_reload is False
        assert ctx.sdk.reconnects == 1
        pending = [event for event in transport.sent
                   if getattr(event, "type", None) == "takeover_state"]
        assert [event.pending for event in pending[-2:]] == [True, False]

    asyncio.run(go())


def test_claude_takeover_adopts_only_completed_native_controls(
    tmp_path, monkeypatch,
):
    class UpstreamReportingSdk(_ClaudeRunSdk):
        async def force_reconnect(self, **kwargs):
            await super().force_reconnect(**kwargs)
            self.model = "glm-5.2"

    async def go():
        machine, transport = _mk_machine()
        session_id = "11111111-1111-4111-8111-111111111111"
        path = tmp_path / "session.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx(session_id, session_id)
        ctx.cwd = str(tmp_path)
        ctx.sdk = UpstreamReportingSdk()
        ctx.announced_model = ctx.sdk.model
        ctx.announced_effort = ctx.sdk.effort
        machine.sessions[session_id] = ctx
        holder = ProcessIdentity(203, 2003)
        watch = _watch(path)
        watch.update({"external": True, "holders": {holder}})
        machine._watch[session_id] = watch

        async def probe(_paths, _cwds):
            return HolderScan({session_id: {holder}}, True)

        async def terminate(_holders):
            return set()

        async def controls(_ctx):
            return ClaudeControls(
                model="claude-opus-4-6[1m]", effort="high")

        monkeypatch.setattr(machine, "_probe_claude_holders", probe)
        monkeypatch.setattr(
            machine, "_terminate_external_claude_holders", terminate)
        monkeypatch.setattr(
            machine, "_read_claude_handoff_controls", controls)

        result = await machine._handle_takeover(Takeover(
            sid=session_id, cmd_id="takeover-controls"))

        assert result is None
        assert ctx.sdk.model == "claude-opus-4-6[1m]"
        assert ctx.sdk.effort == "high"
        assert ctx.sdk.reconnect_args[-1]["preserve_model"] is True
        assert ctx.needs_reload is False
        assert machine._claude_controls.get(session_id) == ClaudeControls(
            model="claude-opus-4-6[1m]",
            effort="high",
            permission_mode="bypassPermissions",
            auto_compact_mode="custom",
            auto_compact_threshold_tokens=500_000,
            applied_auto_compact_mode="custom",
            applied_auto_compact_threshold_tokens=500_000,
        )
        assert any(getattr(event, "type", None) == "model"
                   and event.model == "claude-opus-4-6[1m]"
                   for event in transport.sent)
        assert any(getattr(event, "type", None) == "effort"
                   and event.effort == "high" for event in transport.sent)

    asyncio.run(go())


def test_claude_takeover_reload_failure_never_opens_remote_writes(
    tmp_path, monkeypatch,
):
    class FailingSdk(_ClaudeRunSdk):
        async def force_reconnect(self, **kwargs):
            self.reconnect_args.append(kwargs)
            raise RuntimeError("resume failed")

    async def go():
        machine, transport = _mk_machine()
        session_id = "22222222-2222-4222-8222-222222222222"
        path = tmp_path / "session.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx(session_id, session_id)
        ctx.cwd = str(tmp_path)
        ctx.sdk = FailingSdk()
        machine.sessions[session_id] = ctx
        holder = ProcessIdentity(204, 2004)
        watch = _watch(path)
        watch.update({"external": True, "holders": {holder}})
        machine._watch[session_id] = watch

        async def probe(_paths, _cwds):
            return HolderScan({session_id: {holder}}, True)

        async def terminate(_holders):
            return set()

        monkeypatch.setattr(machine, "_probe_claude_holders", probe)
        monkeypatch.setattr(
            machine, "_terminate_external_claude_holders", terminate)
        result = await machine._handle_takeover(Takeover(
            sid=session_id, cmd_id="takeover-failed-reload"))

        assert result is not None and result.code == "cc_crash"
        assert ctx.needs_reload is True
        assert ctx.write_state == "read_only"
        assert ctx.control_can_takeover is False
        assert not any(getattr(event, "type", None) == "history"
                       for event in transport.sent)

    asyncio.run(go())


def test_claude_takeover_never_force_kills_a_process_that_does_not_exit(
    tmp_path, monkeypatch,
):
    async def go():
        machine, transport = _mk_machine()
        path = tmp_path / "session.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.sdk = _ClaudeRunSdk()
        machine.sessions["sid"] = ctx
        holder = ProcessIdentity(104, 1004)
        watch = _watch(path)
        watch.update({"external": True, "holders": {holder}})
        machine._watch["sid"] = watch

        async def probe(_paths, _cwds):
            return HolderScan({"sid": {holder}}, True)

        async def terminate(holders):
            assert holders == {holder}
            return {holder}

        monkeypatch.setattr(
            machine, "_probe_claude_holders", probe, raising=False)
        monkeypatch.setattr(
            machine, "_terminate_external_claude_holders", terminate,
            raising=False)

        result = await machine._handle_takeover(Takeover(
            sid="sid", cmd_id="takeover-refused"))

        assert result is not None and result.code == "busy"
        assert watch["takeover_pending"] is False
        assert machine._is_external("sid") is True
        assert ctx.needs_reload is False
        assert not any(getattr(event, "type", None) == "history"
                       for event in transport.sent)

    asyncio.run(go())


def test_claude_takeover_stays_locked_when_owner_scan_is_incomplete(
    tmp_path, monkeypatch,
):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "session.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.sdk = _ClaudeRunSdk()
        machine.sessions["sid"] = ctx
        watch = _watch(path)
        watch["external"] = True
        machine._watch["sid"] = watch

        async def probe(_paths, _cwds):
            return HolderScan({"sid": set()}, False)

        monkeypatch.setattr(
            machine, "_probe_claude_holders", probe, raising=False)
        result = await machine._handle_takeover(Takeover(
            sid="sid", cmd_id="takeover-incomplete"))

        assert result.code == "busy"
        assert machine._is_external("sid") is True
        assert watch["takeover_pending"] is False

    asyncio.run(go())


def test_native_claude_owner_blocks_remote_control_mutations(monkeypatch):
    async def go():
        machine, transport = _mk_machine()
        session_id = "33333333-3333-4333-8333-333333333333"
        ctx = _mk_ctx(session_id, session_id)
        ctx.sdk = _ClaudeRunSdk()
        machine.sessions[session_id] = ctx

        async def external(_sid):
            return True

        monkeypatch.setattr(
            machine, "_prime_claude_ownership", external)
        result = await machine._handle_set_effort(SimpleNamespace(
            sid=session_id, effort="low"))

        assert result.code == "busy"
        assert ctx.sdk.effort == "max"
        assert "点击『接管』" in result.message
        assert "claude-remote" not in result.message
        assert any(getattr(event, "code", None) == "busy"
                   for event in transport.sent)

    asyncio.run(go())


def test_queued_claude_takeover_unlocks_only_after_exact_owner_exits(
    tmp_path,
):
    async def go():
        machine, transport = _mk_machine()
        path = tmp_path / "session.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        machine.sessions["sid"] = ctx
        holder = ProcessIdentity(103, 1003)
        watch = _watch(path)
        watch.update({
            "external": True,
            "holders": {holder},
            "takeover_pending": True,
        })
        machine._watch["sid"] = watch
        machine._push_mirrored_history = lambda _sid: asyncio.sleep(0)

        await machine._poll_claude_watch(
            "sid", watch, set(), 1000.0,
            ownership_scan_complete=True,
        )

        assert watch["takeover_pending"] is False
        assert machine._is_external("sid") is False
        states = [event for event in transport.sent
                  if getattr(event, "type", None) == "takeover_state"]
        assert states and states[-1].pending is False

    asyncio.run(go())


def test_short_external_append_is_reloaded_before_claude_query(
    tmp_path, monkeypatch,
):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "session.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.state = "running"
        ctx.active_msg_id = "msg-reload"
        sdk = _ClaudeRunSdk()
        ctx.sdk = sdk
        machine.sessions["sid"] = ctx
        watch = _watch(path)
        machine._watch["sid"] = watch
        machine._push_mirrored_history = lambda _sid: asyncio.sleep(0)

        async def probe(_paths, _cwds):
            return HolderScan({"sid": set()}, True)

        monkeypatch.setattr(
            machine, "_probe_claude_holders", probe, raising=False)

        async def completed_controls(_ctx):
            return ClaudeControls(
                model="claude-fable-5-1", effort="high")

        monkeypatch.setattr(
            machine, "_read_claude_handoff_controls", completed_controls)
        path.write_bytes(
            b'{"type":"assistant","entrypoint":"cli"}\n')

        await machine._run_turn(ctx, "hello")

        assert sdk.reconnects == 1
        assert sdk.reconnect_args == [{
            "resume_id": "sid",
            "cwd": ctx.cwd,
            "reason": "external transcript change at final preflight",
            "preserve_model": True,
        }]
        assert sdk.model == "claude-fable-5-1[1m]"
        assert sdk.effort == "high"
        assert sdk.queries == 1
        assert ctx.state == "idle"
        assert ctx.needs_reload is False

    asyncio.run(go())


def test_unknown_growth_does_not_restore_stale_native_controls(
    tmp_path, monkeypatch,
):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "session.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.state = "running"
        ctx.active_msg_id = "msg-metadata"
        sdk = _ClaudeRunSdk()
        sdk.model = "claude-mythos-5-1[1m]"
        ctx.sdk = sdk
        machine.sessions["sid"] = ctx
        watch = _watch(path)
        machine._watch["sid"] = watch
        machine._push_mirrored_history = lambda _sid: asyncio.sleep(0)

        async def probe(_paths, _cwds):
            return HolderScan({"sid": set()}, True)

        stale_control_reads = 0

        async def stale_completed_controls(_ctx):
            nonlocal stale_control_reads
            stale_control_reads += 1
            return ClaudeControls(model="claude-fable-5-1")

        monkeypatch.setattr(
            machine, "_probe_claude_holders", probe, raising=False)
        monkeypatch.setattr(
            machine,
            "_read_claude_handoff_controls",
            stale_completed_controls,
        )
        path.write_bytes(b'{"type":"future-metadata"}\n')

        await machine._run_turn(ctx, "hello")

        assert sdk.reconnects == 1
        assert sdk.reconnect_args[0]["preserve_model"] is True
        assert sdk.model == "claude-mythos-5-1[1m]"
        assert stale_control_reads == 0
        assert ctx.claude_native_controls_dirty is False
        assert sdk.queries == 1

    asyncio.run(go())


def test_work_handoff_does_not_promote_native_base_model(monkeypatch):
    async def go():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.space = "work"
        sdk = _ClaudeRunSdk()
        sdk.model = "claude-fable-5-1"
        ctx.sdk = sdk
        ctx.claude_native_controls_dirty = True

        async def completed_controls(_ctx):
            return ClaudeControls(model="claude-fable-5-1")

        monkeypatch.setattr(
            machine, "_read_claude_handoff_controls", completed_controls)

        model, _effort = await machine._stage_claude_handoff_controls(ctx)

        assert model == "claude-fable-5-1"
        assert sdk.model == "claude-fable-5-1"
        assert ctx.claude_native_controls_dirty is False

    asyncio.run(go())


def test_second_external_append_cancels_query_after_reload(monkeypatch):
    async def go():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.state = "running"
        ctx.active_msg_id = "msg-raced"
        sdk = _ClaudeRunSdk()
        ctx.sdk = sdk
        machine.sessions["sid"] = ctx
        probes = 0

        async def changed_again(_sid):
            nonlocal probes
            probes += 1
            ctx.needs_reload = True
            return False

        monkeypatch.setattr(
            machine, "_prime_claude_ownership", changed_again, raising=False)
        await machine._run_turn(ctx, "hello")

        assert probes == 2
        assert sdk.reconnects == 1
        assert sdk.queries == 0
        assert any(getattr(event, "code", None) == "busy"
                   and event.msg_id == "msg-raced" for event in transport.sent)

    asyncio.run(go())


def test_failed_claude_query_never_rebaselines_an_ambiguous_append(
    tmp_path, monkeypatch,
):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "session.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.state = "running"
        ctx.active_msg_id = "msg-failed"
        ctx.sdk = _RejectAfterWriteSdk(path)
        machine.sessions["sid"] = ctx
        watch = _watch(path)
        machine._watch["sid"] = watch
        machine._push_mirrored_history = lambda _sid: asyncio.sleep(0)

        async def probe(_paths, _cwds):
            return HolderScan({"sid": set()}, True)

        monkeypatch.setattr(
            machine, "_probe_claude_holders", probe, raising=False)
        await machine._run_turn(ctx, "hello")

        assert watch["size"] == 0
        assert path.stat().st_size > 0
        assert await machine._prime_claude_ownership("sid") is False
        assert ctx.needs_reload is True

    asyncio.run(go())


def test_failed_claude_message_pump_forces_resume_before_next_query(monkeypatch):
    async def go():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.state = "running"
        ctx.active_msg_id = "msg-pump-failed"
        sdk = _FailedMessagePumpSdk()
        ctx.sdk = sdk
        machine.sessions["sid"] = ctx

        async def no_external_owner(_sid):
            return False

        monkeypatch.setattr(
            machine, "_prime_claude_ownership", no_external_owner,
            raising=False,
        )

        await machine._run_turn(ctx, "first")

        assert ctx.needs_reload is False
        assert sdk.reconnects == 0
        assert any(
            getattr(event, "code", None) == "cc_crash"
            and event.msg_id == "msg-pump-failed"
            for event in transport.sent
        )

        sdk.fail_response = False
        ctx.state = "running"
        ctx.active_msg_id = "msg-after-recovery"
        await machine._run_turn(ctx, "second")

        assert sdk.reconnects == 1
        assert sdk.reconnect_args == [{
            "resume_id": "sid",
            "cwd": ctx.cwd,
            "reason": "message pump failure",
            "preserve_model": True,
        }]
        assert sdk.queries == 2
        assert sdk.message_pump_failed is False
        assert ctx.needs_reload is False
        assert ctx.state == "idle"

    asyncio.run(go())


def test_failed_claude_control_plane_forces_resume_before_next_query(
    monkeypatch,
):
    async def go():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.state = "running"
        ctx.active_msg_id = "msg-control-failed"
        sdk = _FailedControlPlaneSdk()
        ctx.sdk = sdk
        machine.sessions["sid"] = ctx

        async def no_external_owner(_sid):
            return False

        monkeypatch.setattr(
            machine, "_prime_claude_ownership", no_external_owner,
            raising=False,
        )

        await machine._run_turn(ctx, "继续")

        assert sdk.reconnects == 1
        assert sdk.reconnect_args == [{
            "resume_id": "sid",
            "cwd": ctx.cwd,
            "reason": "control plane failure",
            "preserve_model": True,
        }]
        assert sdk.queries == 1
        assert ctx.state == "idle"

    asyncio.run(go())


def test_first_claude_session_id_capture_starts_owner_watch(monkeypatch):
    async def go():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("tmp-first", None)
        ctx.sdk = _ClaudeRunSdk()
        machine.sessions[ctx.key] = ctx
        watched = []
        monkeypatch.setattr(machine, "_watch_session", watched.append)

        await machine._capture_session_id(
            ctx, "11111111-1111-4111-8111-111111111111")

        assert watched == ["11111111-1111-4111-8111-111111111111"]

    asyncio.run(go())
