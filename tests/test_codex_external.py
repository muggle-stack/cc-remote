"""Zero-token tests for Codex external-terminal ownership detection."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

from cc_remote.protocol import History, Query, Takeover
from cc_remote.wrapper import machine as machine_module
from cc_remote.wrapper.codex_external import (
    HolderScan, ProcessIdentity, parse_turn_markers, writable_rollout_holders,
)
from tests.test_multisession import _mk_ctx, _mk_machine


def _event(kind: str, turn_id: str) -> bytes:
    return (json.dumps({
        "type": "event_msg",
        "payload": {"type": kind, "turn_id": turn_id},
    }) + "\n").encode()


def _fake_process(root: Path, pid: int, start: int, *, tty: int = 0,
                  cmdline: tuple[str, ...] = ()) -> Path:
    proc = root / str(pid)
    (proc / "fd").mkdir(parents=True)
    (proc / "fdinfo").mkdir()
    # /proc/<pid>/stat fields after comm start at field 3; starttime is field 22.
    fields = ["S"] + ["0"] * 18 + [str(start)] + ["0"] * 4
    fields[4] = str(tty)
    (proc / "stat").write_text(f"{pid} (codex) " + " ".join(fields))
    if cmdline:
        (proc / "cmdline").write_bytes(
            b"\0".join(arg.encode() for arg in cmdline) + b"\0")
    return proc


def _fake_fd(proc: Path, fd: int, target: Path, flags: int) -> None:
    (proc / "fd" / str(fd)).symlink_to(target)
    (proc / "fdinfo" / str(fd)).write_text(f"flags:\t0{flags:o}\n")


def _watch(path: Path) -> dict:
    st = path.stat()
    return {
        "path": str(path), "size": st.st_size,
        "file_id": (st.st_dev, st.st_ino), "engine": "codex",
        "external_ts": 0.0, "flagged": False, "external": False,
        "holders": set(), "writers": set(), "active_external_turns": {},
        "pending_wrapper_turns": {}, "takeover_holders": set(),
        "takeover_interactive_holders": set(),
        "partial": b"",
    }


class _CodexSdk:
    def __init__(self, owned=()):
        self._owned = set(owned)
        self.turn_start_pending = False
        self.turn_active = False
        self.proc = None

    @property
    def owned_turn_ids(self):
        return frozenset(self._owned)

    def remember_owned_turn_id(self, turn_id):
        self._owned.add(turn_id)


def test_writable_holder_matches_inode_and_excludes_own_process(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(b"")
    proc_root = tmp_path / "proc"
    writer = _fake_process(proc_root, 101, 1001)
    reader = _fake_process(proc_root, 102, 1002)
    own = _fake_process(proc_root, 103, 1003)
    _fake_fd(writer, 7, rollout, os.O_WRONLY | os.O_APPEND)
    _fake_fd(reader, 8, rollout, os.O_RDONLY)
    _fake_fd(own, 9, rollout, os.O_RDWR)

    scan = writable_rollout_holders(
        {"sid": str(rollout)}, {ProcessIdentity(103, 1003)},
        proc_root=str(proc_root),
    )
    assert scan.complete is True
    assert scan.holders == {"sid": {ProcessIdentity(101, 1001)}}


def test_writable_holder_uses_inode_not_path_text(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(b"")
    alias = tmp_path / "alias.jsonl"
    alias.symlink_to(rollout)
    proc_root = tmp_path / "proc"
    writer = _fake_process(proc_root, 201, 2001)
    _fake_fd(writer, 3, alias, os.O_WRONLY)

    scan = writable_rollout_holders(
        {"sid": str(rollout)}, proc_root=str(proc_root))
    assert scan.complete is True
    assert scan.holders["sid"] == {ProcessIdentity(201, 2001)}


def test_headless_app_server_holder_is_classified_passive(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(b"")
    proc_root = tmp_path / "proc"
    passive = _fake_process(
        proc_root, 211, 2101,
        cmdline=("codex", "-c", "features.code_mode_host=true",
                 "app-server", "--listen", "unix://"),
    )
    interactive = _fake_process(
        proc_root, 212, 2102, tty=34817,
        cmdline=("codex", "app-server", "--stdio"),
    )
    _fake_fd(passive, 4, rollout, os.O_WRONLY | os.O_APPEND)
    _fake_fd(interactive, 5, rollout, os.O_WRONLY | os.O_APPEND)

    scan = writable_rollout_holders(
        {"sid": str(rollout)}, proc_root=str(proc_root))
    assert scan.complete is True
    assert scan.holders["sid"] == {
        ProcessIdentity(211, 2101), ProcessIdentity(212, 2102)}
    assert scan.passive_holders["sid"] == {ProcessIdentity(211, 2101)}


def test_idle_codex_resume_tui_is_a_logical_interactive_holder(tmp_path):
    sid = "019f49bc-f146-70b3-bfcb-1b7f2a50901d"
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(b"")
    proc_root = tmp_path / "proc"
    passive = _fake_process(
        proc_root, 221, 2201,
        cmdline=("codex", "-c", "features.code_mode_host=true",
                 "app-server", "--listen", "unix://"),
    )
    tui = _fake_process(
        proc_root, 222, 2202, tty=34818,
        cmdline=("codex", "resume", sid, "--no-alt-screen"),
    )
    _fake_fd(passive, 4, rollout, os.O_WRONLY | os.O_APPEND)

    scan = writable_rollout_holders(
        {sid: str(rollout)}, proc_root=str(proc_root))

    assert scan.complete is True
    assert scan.holders[sid] == {
        ProcessIdentity(221, 2201), ProcessIdentity(222, 2202)}
    assert scan.passive_holders[sid] == {ProcessIdentity(221, 2201)}
    assert tui.exists()


def test_holder_scan_reports_missing_proc_as_incomplete(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(b"")
    scan = writable_rollout_holders(
        {"sid": str(rollout)}, proc_root=str(tmp_path / "missing-proc"))
    assert scan.complete is False and scan.holders == {"sid": set()}


def test_turn_marker_parser_preserves_partial_and_skips_malformed():
    start = _event("task_started", "external-1")
    split = len(start) // 2
    first = parse_turn_markers(start[:split])
    assert not first.started and first.partial == start[:split]
    second = parse_turn_markers(
        start[split:] + b"not-json\n" + _event("task_complete", "external-1"),
        first.partial,
    )
    assert second.started == {"external-1"}
    assert second.finished == {"external-1"}
    assert second.partial == b""
    assert second.ordered == (
        ("task_started", "external-1"),
        ("task_complete", "external-1"),
    )


def test_codex_own_delayed_flush_does_not_mirror_or_lock(tmp_path):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.sdk = _CodexSdk({"own-1"})
        machine.sessions["sid"] = ctx
        watch = _watch(path)
        machine._watch["sid"] = watch
        pushed = []

        async def push(sid):
            pushed.append((sid, machine._is_external(sid)))

        machine._push_mirrored_history = push
        path.write_bytes(_event("task_started", "own-1")
                         + _event("task_complete", "own-1"))
        await machine._poll_codex_watch("sid", watch, set(), 1000.0)
        assert pushed == []
        assert machine._is_external("sid") is False
        assert ctx.needs_reload is False

    asyncio.run(go())


def test_codex_holder_locks_and_unlocks_without_ttl(tmp_path):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.sdk = _CodexSdk()
        machine.sessions["sid"] = ctx
        watch = _watch(path)
        machine._watch["sid"] = watch
        pushed = []

        async def push(sid):
            pushed.append(machine._is_external(sid))

        machine._push_mirrored_history = push
        holder = ProcessIdentity(301, 3001)
        await machine._poll_codex_watch("sid", watch, {holder}, 1000.0)
        await machine._poll_codex_watch("sid", watch, set(), 1001.0)
        assert pushed == [True, False]
        assert ctx.needs_reload is True

    asyncio.run(go())


def test_idle_passive_app_server_does_not_lock_or_stale_context(tmp_path, monkeypatch):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.sdk = _CodexSdk()
        machine.sessions["sid"] = ctx
        watch = _watch(path)
        machine._watch["sid"] = watch
        holder = ProcessIdentity(351, 3501)

        async def passive(_paths):
            return HolderScan(
                {"sid": {holder}}, True, {"sid": {holder}})

        pushed = []
        monkeypatch.setattr(machine, "_probe_codex_holders", passive)
        machine._push_mirrored_history = lambda sid: _record_async(pushed, sid)
        assert await machine._prime_codex_ownership("sid") is False
        assert watch["holders"] == set()
        assert ctx.needs_reload is False and pushed == []

    asyncio.run(go())


def test_passive_app_server_locks_only_for_active_external_turn(tmp_path):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.sdk = _CodexSdk()
        machine.sessions["sid"] = ctx
        watch = _watch(path)
        machine._watch["sid"] = watch
        pushed = []
        async def push(sid):
            pushed.append(machine._is_external(sid))
            return History(
                session_id=sid, sid=sid, events=[], has_more=False,
                external=machine._is_external(sid))

        machine._push_mirrored_history = push

        path.write_bytes(_event("task_started", "passive-turn"))
        await machine._poll_codex_watch("sid", watch, set(), 1000.0)
        assert pushed == [True] and machine._is_external("sid") is True
        path.write_bytes(path.read_bytes() + _event("task_complete", "passive-turn"))
        await machine._poll_codex_watch("sid", watch, set(), 1001.0)
        assert pushed == [True, False]
        assert machine._is_external("sid") is False
        assert ctx.needs_reload is True

    asyncio.run(go())


def test_active_passive_writer_prevents_orphan_timeout_until_writer_exits(tmp_path):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.sdk = _CodexSdk()
        machine.sessions["sid"] = ctx
        watch = _watch(path)
        machine._watch["sid"] = watch
        writer = ProcessIdentity(361, 3601)
        machine._push_mirrored_history = lambda sid: _record_async([], sid)

        path.write_bytes(_event("task_started", "long-passive-turn"))
        await machine._poll_codex_watch(
            "sid", watch, set(), 1000.0, writers={writer})
        assert machine._is_external("sid") is True
        await machine._poll_codex_watch(
            "sid", watch, set(), 1061.0, writers={writer})
        assert set(watch["active_external_turns"]) == {"long-passive-turn"}
        assert machine._is_external("sid") is True
        await machine._poll_codex_watch(
            "sid", watch, set(), 1122.0, writers=set())
        assert watch["active_external_turns"] == {}
        assert machine._is_external("sid") is False

    asyncio.run(go())


def test_external_turn_mirrors_and_marks_context_stale(tmp_path):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.sdk = _CodexSdk()
        machine.sessions["sid"] = ctx
        watch = _watch(path)
        machine._watch["sid"] = watch
        pushed = []

        async def push(sid):
            pushed.append(machine._is_external(sid))

        machine._push_mirrored_history = push
        path.write_bytes(_event("task_started", "foreign-1"))
        holder = ProcessIdentity(401, 4001)
        await machine._poll_codex_watch("sid", watch, {holder}, 1000.0)
        assert pushed == [True]
        assert ctx.needs_reload is True
        path.write_bytes(path.read_bytes() + _event("task_complete", "foreign-1"))
        await machine._poll_codex_watch("sid", watch, {holder}, 1001.0)
        assert pushed == [True, True]
        await machine._poll_codex_watch("sid", watch, set(), 1002.0)
        assert pushed == [True, True, False]

    asyncio.run(go())


def test_short_lived_external_turn_mirrors_without_stale_lock(tmp_path):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.sdk = _CodexSdk()
        machine.sessions["sid"] = ctx
        watch = _watch(path)
        machine._watch["sid"] = watch
        pushed = []
        machine._push_mirrored_history = lambda sid: _record_async(pushed, sid)
        path.write_bytes(_event("task_started", "foreign-2")
                         + _event("task_complete", "foreign-2"))
        await machine._poll_codex_watch("sid", watch, set(), 1000.0)
        assert pushed == ["sid"]
        assert machine._is_external("sid") is False
        assert ctx.needs_reload is True

    asyncio.run(go())


async def _record_async(items: list, item) -> None:
    items.append(item)


def test_turn_start_race_is_attributed_to_wrapper(tmp_path):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.state = "running"
        sdk = _CodexSdk()
        sdk.turn_start_pending = True
        sdk.turn_active = True
        ctx.sdk = sdk
        machine.sessions["sid"] = ctx
        watch = _watch(path)
        machine._watch["sid"] = watch
        pushed = []
        machine._push_mirrored_history = lambda sid: _record_async(pushed, sid)
        path.write_bytes(_event("task_started", "racing-own"))
        await machine._poll_codex_watch("sid", watch, set(), 1000.0)
        assert sdk.owned_turn_ids == set()
        assert "racing-own" in watch["pending_wrapper_turns"]
        # A loaded app-server can take longer than the automatic-continuation
        # grace to return turn/start. The still-pending RPC remains authoritative.
        await machine._poll_codex_watch(
            "sid", watch, set(),
            1000.0 + machine.CODEX_TURN_ATTRIBUTION_GRACE + 0.01,
        )
        assert "racing-own" in watch["pending_wrapper_turns"]
        assert watch["active_external_turns"] == {}
        assert machine._is_external("sid") is False
        sdk.remember_owned_turn_id("racing-own")
        sdk.turn_start_pending = False
        await machine._poll_codex_watch("sid", watch, set(), 1004.0)
        assert sdk.owned_turn_ids == {"racing-own"}
        assert watch["pending_wrapper_turns"] == {}
        assert pushed == [] and ctx.needs_reload is False

    asyncio.run(go())


def test_foreign_marker_during_turn_start_is_not_claimed_by_local_response(tmp_path):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.state = "running"
        sdk = _CodexSdk()
        sdk.turn_start_pending = True
        sdk.turn_active = True
        ctx.sdk = sdk
        machine.sessions["sid"] = ctx
        watch = _watch(path)
        machine._watch["sid"] = watch
        pushed = []
        machine._push_mirrored_history = lambda sid: _record_async(pushed, sid)

        path.write_bytes(_event("task_started", "foreign-during-rpc")
                         + _event("task_complete", "foreign-during-rpc"))
        await machine._poll_codex_watch("sid", watch, set(), 1000.0)
        assert sdk.owned_turn_ids == set()
        assert "foreign-during-rpc" in watch["pending_wrapper_turns"]

        # The authoritative turn/start response names a different, local turn.
        sdk.remember_owned_turn_id("actual-local")
        sdk.turn_start_pending = False
        await machine._poll_codex_watch(
            "sid", watch, set(),
            1000.0 + machine.CODEX_TURN_ATTRIBUTION_GRACE + 0.01,
        )
        assert sdk.owned_turn_ids == {"actual-local"}
        assert watch["pending_wrapper_turns"] == {}
        assert ctx.needs_reload is True and pushed == ["sid"]

    asyncio.run(go())


def test_running_before_turn_start_does_not_hide_external_short_turn(tmp_path):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.state = "running"  # claimed by _handle_query, but turn/start not sent
        ctx.sdk = _CodexSdk()
        machine.sessions["sid"] = ctx
        watch = _watch(path)
        machine._watch["sid"] = watch
        pushed = []
        machine._push_mirrored_history = lambda sid: _record_async(pushed, sid)
        path.write_bytes(_event("task_started", "foreign-race")
                         + _event("task_complete", "foreign-race"))
        await machine._poll_codex_watch("sid", watch, set(), 1000.0)
        assert pushed == ["sid"] and ctx.needs_reload is True

    asyncio.run(go())


def test_completed_wrapper_turn_does_not_hide_external_short_turn(tmp_path):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.state = "running"
        sdk = _CodexSdk()
        # A stale diagnostic flag must not attribute an unknown marker to us;
        # turn_start_pending is the only unavoidable pre-response race window.
        sdk.turn_active = True
        ctx.sdk = sdk
        machine.sessions["sid"] = ctx
        watch = _watch(path)
        machine._watch["sid"] = watch
        pushed = []
        machine._push_mirrored_history = lambda sid: _record_async(pushed, sid)
        path.write_bytes(_event("task_started", "foreign-after-own")
                         + _event("task_complete", "foreign-after-own"))
        await machine._poll_codex_watch("sid", watch, set(), 1000.0)
        assert sdk.owned_turn_ids == set()
        assert pushed == [] and ctx.needs_reload is False
        await machine._poll_codex_watch(
            "sid", watch, set(),
            1000.0 + machine.CODEX_TURN_ATTRIBUTION_GRACE + 0.01,
        )
        assert pushed == ["sid"] and ctx.needs_reload is True

    asyncio.run(go())


def test_automatic_own_marker_waits_for_turn_started_notification(tmp_path):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        sdk = _CodexSdk()
        sdk.turn_active = True
        ctx.sdk = sdk
        machine.sessions["sid"] = ctx
        watch = _watch(path)
        machine._watch["sid"] = watch
        pushed = []
        machine._push_mirrored_history = lambda sid: _record_async(pushed, sid)

        path.write_bytes(_event("task_started", "automatic-own"))
        await machine._poll_codex_watch("sid", watch, set(), 1000.0)
        assert ctx.needs_reload is False and pushed == []
        assert "automatic-own" in watch["pending_wrapper_turns"]

        # Simulate the authoritative app-server turn/started notification arriving
        # after the rollout writer. The next poll reconciles it without a mirror.
        sdk.remember_owned_turn_id("automatic-own")
        await machine._poll_codex_watch("sid", watch, set(), 1001.0)
        assert watch["pending_wrapper_turns"] == {}
        assert ctx.needs_reload is False and pushed == []

    asyncio.run(go())


def test_unknown_marker_during_active_turn_becomes_external_after_grace(tmp_path):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        sdk = _CodexSdk()
        sdk.turn_active = True
        ctx.sdk = sdk
        machine.sessions["sid"] = ctx
        watch = _watch(path)
        machine._watch["sid"] = watch
        pushed = []
        machine._push_mirrored_history = lambda sid: _record_async(pushed, sid)

        path.write_bytes(_event("task_started", "foreign-no-notification")
                         + _event("task_complete", "foreign-no-notification"))
        await machine._poll_codex_watch("sid", watch, set(), 1000.0)
        assert ctx.needs_reload is False and pushed == []
        await machine._poll_codex_watch(
            "sid", watch, set(),
            1000.0 + machine.CODEX_TURN_ATTRIBUTION_GRACE + 0.01,
        )
        assert watch["pending_wrapper_turns"] == {}
        assert ctx.needs_reload is True and pushed == ["sid"]

    asyncio.run(go())


def test_pending_unknown_marker_resolves_external_when_wrapper_turn_ends(tmp_path):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        sdk = _CodexSdk()
        sdk.turn_active = True
        ctx.sdk = sdk
        machine.sessions["sid"] = ctx
        watch = _watch(path)
        machine._watch["sid"] = watch
        pushed = []
        machine._push_mirrored_history = lambda sid: _record_async(pushed, sid)

        path.write_bytes(_event("task_started", "foreign-turn-ended")
                         + _event("task_complete", "foreign-turn-ended"))
        await machine._poll_codex_watch("sid", watch, set(), 1000.0)
        sdk.turn_active = False
        await machine._poll_codex_watch("sid", watch, set(), 1000.1)
        assert watch["pending_wrapper_turns"] == {}
        assert ctx.needs_reload is True and pushed == ["sid"]

    asyncio.run(go())


def test_pending_attribution_cap_fails_stale(tmp_path):
    async def go():
        machine, _ = _mk_machine()
        machine.CODEX_TURN_TRACK_MAX = 1
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        sdk = _CodexSdk()
        sdk.turn_active = True
        ctx.sdk = sdk
        machine.sessions["sid"] = ctx
        watch = _watch(path)
        machine._watch["sid"] = watch
        pushed = []
        machine._push_mirrored_history = lambda sid: _record_async(pushed, sid)

        path.write_bytes(_event("task_started", "unknown-1")
                         + _event("task_started", "unknown-2"))
        await machine._poll_codex_watch("sid", watch, set(), 1000.0)
        assert len(watch["pending_wrapper_turns"]) == 1
        assert ctx.needs_reload is True and pushed == ["sid"]

    asyncio.run(go())


def test_query_is_rejected_before_state_claim_when_codex_is_external(monkeypatch):
    async def go():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.sdk = _CodexSdk()
        machine.sessions["sid"] = ctx
        monkeypatch.setattr(machine, "_watch_session", lambda sid: None)

        async def occupied(sid):
            return True

        monkeypatch.setattr(machine, "_prime_codex_ownership", occupied)
        result = await machine._handle_query(Query(
            sid="sid", prompt="hello", msg_id="msg-1"))
        assert result.code == "busy" and result.msg_id == "msg-1"
        assert ctx.state == "idle" and ctx.turn_task is None
        assert transport.sent[-1] is result

    asyncio.run(go())


def test_codex_takeover_ignores_exact_current_holder_and_unlocks(tmp_path, monkeypatch):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.sdk = _CodexSdk()
        machine.sessions["sid"] = ctx
        holder = ProcessIdentity(451, 4501)
        watch = _watch(path)
        watch.update({"external": True, "holders": {holder}})
        machine._watch["sid"] = watch

        scans = [
            HolderScan({"sid": {holder}}, True),
            HolderScan({"sid": {holder}}, True),
        ]

        async def probe(_paths):
            return scans.pop(0)

        pushed = []
        monkeypatch.setattr(machine, "_probe_codex_holders", probe)
        async def push(sid):
            pushed.append(machine._is_external(sid))
            return History(
                session_id=sid, sid=sid, events=[], has_more=False,
                external=machine._is_external(sid))

        machine._push_mirrored_history = push
        result = await machine._handle_takeover(Takeover(
            sid="sid", cmd_id="takeover-direct-1"))
        assert result is None
        assert watch["takeover_holders"] == {holder}
        assert watch["external"] is False and ctx.needs_reload is True
        assert pushed[-1] is False
        assert await machine._prime_codex_ownership("sid") is False
        assert watch["takeover_holders"] == {holder}

    asyncio.run(go())


def test_codex_takeover_relocks_for_new_holder(tmp_path, monkeypatch):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.sdk = _CodexSdk()
        machine.sessions["sid"] = ctx
        old = ProcessIdentity(461, 4601)
        new = ProcessIdentity(462, 4602)
        watch = _watch(path)
        watch["takeover_holders"] = {old}
        machine._watch["sid"] = watch

        async def probe(_paths):
            return HolderScan({"sid": {old, new}}, True)

        monkeypatch.setattr(machine, "_probe_codex_holders", probe)
        machine._push_mirrored_history = lambda sid: _record_async([], sid)
        assert await machine._prime_codex_ownership("sid") is True
        assert watch["holders"] == {new}

    asyncio.run(go())


def test_codex_takeover_relocks_when_ignored_holder_starts_new_turn(
        tmp_path, monkeypatch):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.sdk = _CodexSdk()
        machine.sessions["sid"] = ctx
        holder = ProcessIdentity(471, 4701)
        watch = _watch(path)
        watch["takeover_holders"] = {holder}
        watch["takeover_interactive_holders"] = {holder}
        machine._watch["sid"] = watch

        async def probe(_paths):
            return HolderScan({"sid": {holder}}, True)

        pushed = []
        monkeypatch.setattr(machine, "_probe_codex_holders", probe)
        machine._push_mirrored_history = lambda sid: _record_async(
            pushed, machine._is_external(sid))
        path.write_bytes(_event("task_started", "terminal-speaks-again"))
        assert await machine._prime_codex_ownership("sid") is True
        assert pushed == [True] and ctx.needs_reload is True
        assert watch["takeover_holders"] == set()
        path.write_bytes(
            path.read_bytes() + _event("task_complete", "terminal-speaks-again"))
        assert await machine._prime_codex_ownership("sid") is True
        assert machine._is_external("sid") is True

    asyncio.run(go())


def test_codex_takeover_relocks_when_captured_holder_turn_was_pending(
        tmp_path):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        sdk = _CodexSdk()
        sdk.turn_start_pending = True
        sdk.turn_active = True
        ctx.sdk = sdk
        machine.sessions["sid"] = ctx
        holder = ProcessIdentity(472, 4702)
        watch = _watch(path)
        watch["takeover_holders"] = {holder}
        watch["takeover_interactive_holders"] = {holder}
        machine._watch["sid"] = watch
        machine._push_mirrored_history = lambda sid: _record_async([], sid)

        path.write_bytes(
            _event("task_started", "captured-during-own-rpc")
            + _event("task_complete", "captured-during-own-rpc"))
        await machine._poll_codex_watch("sid", watch, set(), 1000.0)
        assert "captured-during-own-rpc" in watch["pending_wrapper_turns"]

        # The turn/start response proves the marker was not the wrapper's turn.
        sdk.remember_owned_turn_id("actual-local")
        sdk.turn_start_pending = False
        await machine._poll_codex_watch("sid", watch, set(), 1000.1)
        assert watch["pending_wrapper_turns"] == {}
        assert watch["takeover_holders"] == set()
        assert watch["holders"] == {holder}
        assert machine._is_external("sid") is True

    asyncio.run(go())


def test_codex_takeover_fails_closed_when_holder_scan_is_incomplete(
        tmp_path, monkeypatch):
    async def go():
        machine, transport = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.sdk = _CodexSdk()
        machine.sessions["sid"] = ctx
        holder = ProcessIdentity(481, 4801)
        watch = _watch(path)
        watch.update({"external": True, "holders": {holder}})
        machine._watch["sid"] = watch

        async def incomplete(_paths):
            return HolderScan({"sid": set()}, False)

        monkeypatch.setattr(machine, "_probe_codex_holders", incomplete)
        result = await machine._handle_takeover(Takeover(
            sid="sid", cmd_id="takeover-direct-2"))
        assert result.code == "busy"
        assert watch["external"] is True
        assert watch["takeover_holders"] == set()
        assert transport.sent[-1] is result

    asyncio.run(go())


def test_codex_takeover_queues_until_external_turn_finishes(
        tmp_path, monkeypatch):
    async def go():
        machine, transport = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.sdk = _CodexSdk()
        machine.sessions["sid"] = ctx
        holder = ProcessIdentity(491, 4901)
        watch = _watch(path)
        watch.update({
            "external": True,
            "holders": {holder},
            "active_external_turns": {"terminal-running": 1000.0},
            "external_ts": 1000.0,
        })
        machine._watch["sid"] = watch

        async def complete(_paths):
            return HolderScan({"sid": {holder}}, True)

        monkeypatch.setattr(machine, "_probe_codex_holders", complete)
        result = await machine._handle_takeover(Takeover(
            sid="sid", cmd_id="takeover-direct-3"))
        assert result is None
        assert watch["external"] is True
        assert watch["takeover_holders"] == set()
        assert watch["takeover_pending"]["writers"] == {holder}
        assert transport.sent[-1].type == "takeover_state"
        assert transport.sent[-1].pending is True

        path.write_bytes(_event("task_complete", "terminal-running"))
        assert await machine._prime_codex_ownership("sid") is False
        assert watch["takeover_pending"] is None
        assert watch["takeover_holders"] == {holder}
        assert watch["external"] is False

    asyncio.run(go())


def test_queued_takeover_never_captures_a_new_holder(tmp_path, monkeypatch):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.sdk = _CodexSdk()
        machine.sessions["sid"] = ctx
        owner = ProcessIdentity(492, 4902)
        newcomer = ProcessIdentity(493, 4903)
        watch = _watch(path)
        watch.update({
            "external": True,
            "holders": {owner},
            "active_external_turns": {"owner-turn": 1000.0},
            "external_ts": 1000.0,
        })
        machine._watch["sid"] = watch
        scans = [
            HolderScan({"sid": {owner}}, True),
            HolderScan({"sid": {owner, newcomer}}, True),
        ]

        async def probe(_paths):
            return scans.pop(0)

        monkeypatch.setattr(machine, "_probe_codex_holders", probe)
        machine._push_mirrored_history = lambda sid: _record_async([], sid)
        await machine._handle_takeover(Takeover(
            sid="sid", cmd_id="takeover-new-holder"))
        path.write_bytes(_event("task_complete", "owner-turn"))

        assert await machine._prime_codex_ownership("sid") is True
        assert watch["takeover_pending"] is None
        assert watch["takeover_holders"] == set()
        assert watch["holders"] == {owner, newcomer}

    asyncio.run(go())


def test_queued_takeover_is_not_cleared_when_rollout_stat_fails(tmp_path):
    async def go():
        machine, transport = _mk_machine()
        missing = tmp_path / "missing-rollout.jsonl"
        missing.write_bytes(b"")
        owner = ProcessIdentity(496, 4906)
        newcomer = ProcessIdentity(497, 4907)
        watch = _watch(missing)
        missing.unlink()
        watch["takeover_pending"] = {
            "writers": {owner},
            "interactive": {owner},
            "turn_ids": {"owner-turn"},
        }
        machine._watch["sid"] = watch

        await machine._poll_codex_watch(
            "sid", watch, {owner, newcomer}, 1000.0,
            writers={owner, newcomer})

        assert watch["takeover_pending"]["writers"] == {owner}
        assert transport.sent == []

    asyncio.run(go())


def test_pending_cancel_without_resident_context_pushes_authoritative_history(
        tmp_path):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        owner = ProcessIdentity(498, 4908)
        newcomer = ProcessIdentity(499, 4909)
        watch = _watch(path)
        watch.update({
            "external": True,
            "holders": {owner},
            "writers": {owner},
            "takeover_pending": {
                "writers": {owner},
                "interactive": {owner},
                "turn_ids": {"owner-turn"},
            },
        })
        machine._watch["sid"] = watch
        pushed = []
        machine._push_mirrored_history = lambda sid: _record_async(pushed, sid)

        await machine._poll_codex_watch(
            "sid", watch, {owner, newcomer}, 1000.0,
            writers={owner, newcomer})

        assert watch["takeover_pending"] is None
        assert watch["external"] is True
        assert pushed == ["sid"]

    asyncio.run(go())


def test_queued_takeover_cancels_when_terminal_starts_another_turn(
        tmp_path, monkeypatch):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.sdk = _CodexSdk()
        machine.sessions["sid"] = ctx
        holder = ProcessIdentity(494, 4904)
        watch = _watch(path)
        watch.update({
            "external": True,
            "holders": {holder},
            "active_external_turns": {"first-turn": 1000.0},
            "external_ts": 1000.0,
        })
        machine._watch["sid"] = watch

        async def probe(_paths):
            return HolderScan({"sid": {holder}}, True)

        monkeypatch.setattr(machine, "_probe_codex_holders", probe)
        machine._push_mirrored_history = lambda sid: _record_async([], sid)
        await machine._handle_takeover(Takeover(
            sid="sid", cmd_id="takeover-next-turn"))
        path.write_bytes(
            _event("task_complete", "first-turn")
            + _event("task_started", "second-turn")
            + _event("task_complete", "second-turn"))

        assert await machine._prime_codex_ownership("sid") is True
        assert watch["takeover_pending"] is None
        assert watch["takeover_holders"] == set()
        assert watch["holders"] == {holder}

    asyncio.run(go())


def test_queued_takeover_waits_for_complete_holder_scan(
        tmp_path, monkeypatch):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.sdk = _CodexSdk()
        machine.sessions["sid"] = ctx
        holder = ProcessIdentity(495, 4905)
        watch = _watch(path)
        watch.update({
            "external": True,
            "holders": {holder},
            "writers": {holder},
            "active_external_turns": {"scan-turn": 1000.0},
            "external_ts": 1000.0,
        })
        machine._watch["sid"] = watch
        scans = [
            HolderScan({"sid": {holder}}, True),
            HolderScan({"sid": set()}, False),
            HolderScan({"sid": {holder}}, True),
        ]

        async def probe(_paths):
            return scans.pop(0)

        monkeypatch.setattr(machine, "_probe_codex_holders", probe)
        machine._push_mirrored_history = lambda sid: _record_async([], sid)
        await machine._handle_takeover(Takeover(
            sid="sid", cmd_id="takeover-incomplete-scan"))
        path.write_bytes(_event("task_complete", "scan-turn"))

        assert await machine._prime_codex_ownership("sid") is True
        assert watch["takeover_pending"]
        assert watch["takeover_holders"] == set()
        assert await machine._prime_codex_ownership("sid") is False
        assert watch["takeover_pending"] is None
        assert watch["takeover_holders"] == {holder}

    asyncio.run(go())


def test_prime_publishes_holder_edges_and_marks_context_stale(tmp_path, monkeypatch):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.sdk = _CodexSdk()
        machine.sessions["sid"] = ctx
        watch = _watch(path)
        machine._watch["sid"] = watch
        holder = ProcessIdentity(501, 5001)
        scans = [HolderScan({"sid": {holder}}, True), HolderScan({"sid": set()}, True)]

        async def probe(_paths):
            return scans.pop(0)

        pushed = []
        monkeypatch.setattr(machine, "_probe_codex_holders", probe)
        machine._push_mirrored_history = lambda sid: _record_async(
            pushed, machine._is_external(sid))
        assert await machine._prime_codex_ownership("sid") is True
        assert await machine._prime_codex_ownership("sid") is False
        assert pushed == [True, False]
        assert ctx.needs_reload is True

    asyncio.run(go())


def test_prime_consumes_short_lived_external_growth_before_query(tmp_path, monkeypatch):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.sdk = _CodexSdk()
        machine.sessions["sid"] = ctx
        watch = _watch(path)
        machine._watch["sid"] = watch
        path.write_bytes(_event("task_started", "short-external")
                         + _event("task_complete", "short-external"))

        async def probe(_paths):
            return HolderScan({"sid": set()}, True)

        pushed = []
        monkeypatch.setattr(machine, "_probe_codex_holders", probe)
        machine._push_mirrored_history = lambda sid: _record_async(pushed, sid)
        assert await machine._prime_codex_ownership("sid") is False
        assert ctx.needs_reload is True and pushed == ["sid"]
        assert watch["size"] == path.stat().st_size

    asyncio.run(go())


def test_incomplete_prime_preserves_existing_lock(tmp_path, monkeypatch):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        watch = _watch(path)
        holder = ProcessIdentity(601, 6001)
        watch.update({"external": True, "holders": {holder}})
        machine._watch["sid"] = watch

        async def incomplete(_paths):
            return HolderScan({"sid": set()}, False)

        pushed = []
        monkeypatch.setattr(machine, "_probe_codex_holders", incomplete)
        machine._push_mirrored_history = lambda sid: _record_async(pushed, sid)
        assert await machine._prime_codex_ownership("sid") is True
        assert watch["holders"] == {holder} and pushed == []

    asyncio.run(go())


def test_incomplete_prime_keeps_new_positive_holder_evidence(tmp_path, monkeypatch):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"")
        watch = _watch(path)
        machine._watch["sid"] = watch
        new_holder = ProcessIdentity(611, 6101)

        async def incomplete_with_positive(_paths):
            return HolderScan({"sid": {new_holder}}, False)

        monkeypatch.setattr(machine, "_probe_codex_holders", incomplete_with_positive)
        machine._push_mirrored_history = lambda sid: _record_async([], sid)
        assert await machine._prime_codex_ownership("sid") is True
        assert watch["holders"] == {new_holder}

    asyncio.run(go())


def test_rotation_marks_resident_context_stale_and_mirrors(tmp_path):
    async def go():
        machine, _ = _mk_machine()
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(b"old\n")
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.sdk = _CodexSdk()
        machine.sessions["sid"] = ctx
        watch = _watch(path)
        machine._watch["sid"] = watch
        replacement = tmp_path / "replacement.jsonl"
        replacement.write_bytes(_event("task_started", "foreign-rotated")
                                + _event("task_complete", "foreign-rotated"))
        replacement.replace(path)
        pushed = []
        machine._push_mirrored_history = lambda sid: _record_async(pushed, sid)
        await machine._poll_codex_watch("sid", watch, set(), 1000.0)
        assert ctx.needs_reload is True and pushed == ["sid"]
        assert watch["file_id"] == (path.stat().st_dev, path.stat().st_ino)
        assert watch["size"] == path.stat().st_size

    asyncio.run(go())


def test_cold_codex_history_registers_codex_watcher(tmp_path, monkeypatch):
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(b"")
    machine, _ = _mk_machine()
    monkeypatch.setattr(
        machine_module, "codex_rollout_path",
        lambda sid: str(path) if sid == "cold-codex" else None)
    monkeypatch.setattr(machine_module, "transcript_path", lambda _sid: None)
    machine._watch_session("cold-codex")
    assert machine._watch["cold-codex"]["engine"] == "codex"
    assert machine._watch["cold-codex"]["path"] == str(path)


def test_cold_codex_watcher_seeds_unfinished_external_turn(tmp_path, monkeypatch):
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(
        _event("task_started", "finished")
        + _event("task_complete", "finished")
        + _event("task_started", "still-running"))
    machine, _ = _mk_machine()
    monkeypatch.setattr(
        machine_module, "codex_rollout_path",
        lambda sid: str(path) if sid == "cold-active" else None)
    monkeypatch.setattr(machine_module, "transcript_path", lambda _sid: None)
    machine._watch_session("cold-active")
    watch = machine._watch["cold-active"]
    assert set(watch["active_external_turns"]) == {"still-running"}
    assert watch["external"] is True


def test_cold_orphan_seed_unlocks_on_first_complete_empty_holder_scan(
        tmp_path, monkeypatch):
    async def go():
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(_event("task_started", "crashed-yesterday"))
        machine, _ = _mk_machine()
        ctx = _mk_ctx("cold-orphan", "cold-orphan")
        ctx.engine = "codex"
        ctx.sdk = _CodexSdk()
        machine.sessions["cold-orphan"] = ctx
        monkeypatch.setattr(
            machine_module, "codex_rollout_path",
            lambda sid: str(path) if sid == "cold-orphan" else None)
        monkeypatch.setattr(machine_module, "transcript_path", lambda _sid: None)
        machine._watch_session("cold-orphan")
        watch = machine._watch["cold-orphan"]
        assert watch["external"] is True

        async def empty_scan(_paths):
            return HolderScan({"cold-orphan": set()}, True)

        monkeypatch.setattr(machine, "_probe_codex_holders", empty_scan)
        machine._push_mirrored_history = lambda sid: _record_async([], sid)
        assert await machine._prime_codex_ownership("cold-orphan") is False
        assert watch["active_external_turns"] == {}
        assert watch["seeded_external_turns"] == set()

    asyncio.run(go())


def test_codex_tail_seed_ignores_old_orphan_after_later_completed_turn(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(
        _event("task_started", "old-orphan")
        + _event("task_started", "later")
        + _event("task_complete", "later"))
    active, partial = machine_module.WrapperMachine._codex_tail_state(
        str(path), path.stat().st_size)
    assert active == set() and partial == b""


def test_codex_tail_seed_finds_start_before_more_than_old_four_mib_window(tmp_path):
    path = tmp_path / "rollout.jsonl"
    # Regression: WATCH_READ_MAX is 4 MiB, but a single Codex output record in a
    # real rollout can exceed that. The cold seed has its own larger bounded tail.
    large_output = (
        b'{"type":"event_msg","payload":{"type":"agent_message_delta",'
        b'"delta":"' + (b"x" * (5 * 1024 * 1024)) + b'"}}\n')
    path.write_bytes(_event("task_started", "large-active") + large_output)
    active, partial = machine_module.WrapperMachine._codex_tail_state(
        str(path), path.stat().st_size)
    assert active == {"large-active"} and partial == b""


def test_codex_tail_seed_preserves_partial_record_for_next_poll(
        tmp_path, monkeypatch):
    async def go():
        complete = _event("task_started", "split-start")
        split = len(complete) // 2
        path = tmp_path / "rollout.jsonl"
        path.write_bytes(complete[:split])
        machine, _ = _mk_machine()
        monkeypatch.setattr(
            machine_module, "codex_rollout_path",
            lambda sid: str(path) if sid == "cold-partial" else None)
        monkeypatch.setattr(machine_module, "transcript_path", lambda _sid: None)
        machine._watch_session("cold-partial")
        watch = machine._watch["cold-partial"]
        assert watch["partial"] == complete[:split]
        with path.open("ab") as stream:
            stream.write(complete[split:])
        machine._push_mirrored_history = lambda sid: _record_async([], sid)
        await machine._poll_codex_watch("cold-partial", watch, set(), 1000.0)
        assert set(watch["active_external_turns"]) == {"split-start"}
        assert watch["external"] is True

    asyncio.run(go())


def test_final_turn_preflight_cancels_before_sdk_query(monkeypatch):
    class RunSdk(_CodexSdk):
        effort = "high"
        applied_effort = "high"
        tier_dirty = False

        def __init__(self):
            super().__init__()
            self.queries = 0

        async def query(self, _prompt, images=None):
            self.queries += 1

    async def go():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.state = "running"
        ctx.active_msg_id = "msg-final"
        sdk = RunSdk()
        ctx.sdk = sdk
        machine.sessions["sid"] = ctx

        async def occupied(_sid):
            return True

        monkeypatch.setattr(machine, "_prime_codex_ownership", occupied)
        await machine._run_turn(ctx, "hello")
        assert sdk.queries == 0 and ctx.state == "idle"
        assert any(getattr(event, "code", None) == "busy"
                   and event.msg_id == "msg-final" for event in transport.sent)

    asyncio.run(go())


class _RunTurnSdk(_CodexSdk):
    model = "gpt-test"
    effort = "high"
    applied_effort = "high"
    service_tier = None
    tier_dirty = False

    def __init__(self):
        super().__init__()
        self.queries = 0
        self.reconnects = 0

    async def force_reconnect(self, **_kwargs):
        self.reconnects += 1

    async def query(self, _prompt, images=None):
        self.queries += 1

    async def receive_response(self):
        yield {
            "method": "turn/completed",
            "params": {"turn": {"id": "remote", "status": "completed"}},
        }


def test_final_preflight_reloads_short_external_turn_before_query(monkeypatch):
    async def go():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.state = "running"
        ctx.active_msg_id = "msg-reload"
        sdk = _RunTurnSdk()
        ctx.sdk = sdk
        machine.sessions["sid"] = ctx
        probes = 0

        async def changed_then_clean(_sid):
            nonlocal probes
            probes += 1
            if probes == 1:
                ctx.needs_reload = True
            return False

        monkeypatch.setattr(machine, "_prime_codex_ownership", changed_then_clean)
        await machine._run_turn(ctx, "hello")
        assert probes == 2
        assert sdk.reconnects == 1 and sdk.queries == 1
        assert ctx.needs_reload is False and ctx.state == "idle"

    asyncio.run(go())


def test_final_preflight_cancels_if_transcript_changes_during_reload(monkeypatch):
    async def go():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.state = "running"
        ctx.active_msg_id = "msg-race"
        sdk = _RunTurnSdk()
        ctx.sdk = sdk
        machine.sessions["sid"] = ctx
        probes = 0

        async def changes_on_both_probes(_sid):
            nonlocal probes
            probes += 1
            ctx.needs_reload = True
            return False

        monkeypatch.setattr(machine, "_prime_codex_ownership", changes_on_both_probes)
        await machine._run_turn(ctx, "hello")
        assert probes == 2
        assert sdk.reconnects == 1 and sdk.queries == 0
        assert ctx.needs_reload is True and ctx.state == "idle"
        assert any(getattr(event, "code", None) == "busy"
                   and event.msg_id == "msg-race" for event in transport.sent)

    asyncio.run(go())


def test_interrupt_during_first_final_preflight_never_starts_query(monkeypatch):
    async def go():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.state = "running"
        ctx.active_msg_id = "msg-interrupt-first"
        sdk = _RunTurnSdk()
        ctx.sdk = sdk
        machine.sessions["sid"] = ctx

        async def interrupted(_sid):
            ctx.interrupt_event.set()
            ctx.state = "interrupting"
            return False

        monkeypatch.setattr(machine, "_prime_codex_ownership", interrupted)
        await machine._run_turn(ctx, "hello")
        assert sdk.reconnects == 0 and sdk.queries == 0
        assert ctx.state == "idle"

    asyncio.run(go())


def test_interrupt_during_second_final_preflight_never_starts_query(monkeypatch):
    async def go():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("sid", "sid")
        ctx.engine = "codex"
        ctx.state = "running"
        ctx.active_msg_id = "msg-interrupt-second"
        sdk = _RunTurnSdk()
        ctx.sdk = sdk
        machine.sessions["sid"] = ctx
        probes = 0

        async def dirty_then_interrupted(_sid):
            nonlocal probes
            probes += 1
            if probes == 1:
                ctx.needs_reload = True
            else:
                ctx.interrupt_event.set()
                ctx.state = "interrupting"
            return False

        monkeypatch.setattr(machine, "_prime_codex_ownership", dirty_then_interrupted)
        await machine._run_turn(ctx, "hello")
        assert probes == 2
        assert sdk.reconnects == 1 and sdk.queries == 0
        assert ctx.state == "idle"

    asyncio.run(go())
