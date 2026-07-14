"""Zero-token regressions for Claude terminal ownership and takeover."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from claude_agent_sdk.types import ResultMessage, SystemMessage

from cc_remote.protocol import Query, Takeover
from cc_remote.wrapper.claude_external import claude_session_holders
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
    return proc


def test_claude_process_scan_tracks_exact_and_cwd_owners(tmp_path):
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
        proc_root, 102, 1002, cwd=project,
        cmdline=("/usr/local/bin/claude",),
    )
    _fake_process(
        proc_root, 103, 1003, parent_pid=900, cwd=project,
        cmdline=("claude", "--resume", sibling),
    )
    paths = {sid: "a", sibling: "b", other: "c"}
    scan = claude_session_holders(
        paths,
        {sid: str(project), sibling: str(project), other: str(elsewhere)},
        wrapper_pid=900,
        proc_root=str(proc_root),
    )

    assert scan.complete is True
    assert scan.holders[sid] == {
        ProcessIdentity(101, 1001), ProcessIdentity(102, 1002)}
    assert scan.holders[sibling] == {ProcessIdentity(102, 1002)}
    assert scan.holders[other] == set()


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


class _ClaudeRunSdk:
    effort = "max"
    applied_effort = "max"
    next_turn_id = None

    def __init__(self):
        self.queries = 0
        self.reconnects = 0

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

    async def force_reconnect(self, **_kwargs):
        self.reconnects += 1

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


def test_growth_after_a_finished_wrapper_turn_is_never_hidden_by_a_ttl(
    tmp_path,
):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "session.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
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


def test_claude_takeover_waits_for_the_terminal_process_to_exit(
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

        monkeypatch.setattr(
            machine, "_probe_claude_holders", probe, raising=False)
        result = await machine._handle_takeover(Takeover(
            sid="sid", cmd_id="takeover-1"))

        assert result is None
        assert watch["takeover_pending"] is True
        assert machine._is_external("sid") is True
        pending = [event for event in transport.sent
                   if getattr(event, "type", None) == "takeover_state"]
        assert pending and pending[-1].pending is True

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
        path.write_bytes(b'{"type":"external-user"}\n')

        await machine._run_turn(ctx, "hello")

        assert sdk.reconnects == 1
        assert sdk.queries == 1
        assert ctx.state == "idle"
        assert ctx.needs_reload is False

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
