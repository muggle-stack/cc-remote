"""Zero-token regressions for authoritative Codex thread deletion."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from cc_remote.config import WrapperConfig
from cc_remote.protocol import (
    DeleteSession,
    DeleteWorkSession,
    ERR_BUSY,
    ERR_INTERNAL,
    ERR_NOT_RUNNING,
    Error,
    Query,
)
from cc_remote.wrapper import machine as machine_module
from cc_remote.wrapper.codex_forks import ForkJournalError
from cc_remote.wrapper.codex_handle import CodexAppServerError
from cc_remote.wrapper.codex_external import HolderScan
from cc_remote.wrapper.codex_rpc import CodexRpcRejected
from cc_remote.wrapper.machine import WrapperMachine
from cc_remote.wrapper.process_scan import ProcessIdentity
from tests.test_multisession import _mk_ctx, _mk_machine


class _Transport:
    def __init__(self) -> None:
        self.sent: list[object] = []
        self.on_connected = None

    async def send(self, message: object) -> None:
        self.sent.append(message)


class _DeleteRecorder:
    def __init__(self, kind: str, calls: list[tuple]) -> None:
        self.kind = kind
        self.calls = calls

    def delete(self, sid: str) -> None:
        self.calls.append((self.kind, sid))


class _PresentationDeleteRecorder(_DeleteRecorder):
    def delete(self, engine: str, sid: str) -> None:
        assert engine == "codex"
        self.calls.append((self.kind, sid))


class _PinRecorder:
    def __init__(self, calls: list[tuple]) -> None:
        self.calls = calls

    def set_pinned(self, engine: str, sid: str, pinned: bool) -> None:
        self.calls.append(("pin", engine, sid, pinned))


class _DeleteHandle:
    def __init__(
        self,
        failure: Exception | None = None,
        deleted_ids: tuple[str, ...] | None = None,
    ):
        self.thread_id = "codex-thread"
        self.turn_active = False
        self.turn_start_pending = False
        self.shared_daemon_affinity = True
        self.using_daemon_proxy = True
        self.failure = failure
        self.deleted_ids = deleted_ids
        self.calls: list[tuple[str, str | None]] = []
        self.parents: dict[str, str | None] = {}
        self.parent_calls: list[str] = []
        self.delete_candidates: tuple[tuple[str, bool], ...] = ()
        self.thread_delete_notifications_overflowed = False

    async def list_thread_delete_candidates(
        self,
    ) -> tuple[tuple[str, bool], ...]:
        return self.delete_candidates

    async def read_thread_parent(self, thread_id: str) -> str | None:
        self.parent_calls.append(thread_id)
        return self.parents.get(thread_id)

    async def delete_thread(
        self,
        expected_thread_id: str,
    ) -> tuple[str, ...]:
        self.calls.append(("delete", expected_thread_id))
        if self.failure is not None:
            raise self.failure
        self.thread_id = None
        return self.deleted_ids or (expected_thread_id,)

    async def disconnect(self) -> None:
        self.calls.append(("disconnect", None))


class _ColdDeleteHandle(_DeleteHandle):
    created: list[_ColdDeleteHandle] = []

    def __init__(
        self,
        _cfg,
        cwd=None,
        daemon_mode=None,
        daemon_manager=None,
        codex_home=None,
    ) -> None:
        super().__init__()
        self.thread_id = None
        self.cwd = cwd
        self.daemon_mode = daemon_mode
        self.daemon_manager = daemon_manager
        self.codex_home = codex_home
        self.using_daemon_proxy = False
        self.shared_daemon_affinity = False
        self.proc = SimpleNamespace(pid=4242, returncode=None)
        self.created.append(self)

    async def connect(self, **kwargs) -> None:
        self.calls.append(("connect", kwargs))
        assert kwargs.get("resume_id") is None
        assert kwargs["control_only"] is True
        self.using_daemon_proxy = True
        self.shared_daemon_affinity = True


class _FallbackColdDeleteHandle(_ColdDeleteHandle):
    async def connect(self, **kwargs) -> None:
        self.calls.append(("connect", kwargs))
        if self.daemon_mode == "auto":
            assert kwargs == {
                "cwd": self.cwd,
                "control_only": True,
            }
            raise RuntimeError("shared proxy unavailable")
        assert self.daemon_mode == "off"
        assert kwargs == {
            "resume_id": "codex-thread",
            "cwd": self.cwd,
        }
        self.thread_id = "codex-thread"


class _CheckpointRecorder:
    created: list[_CheckpointRecorder] = []

    def __init__(
        self,
        cwd,
        state_dir,
        sid,
        *,
        profile_revision,
    ) -> None:
        self.cwd = cwd
        self.state_dir = state_dir
        self.sid = sid
        self.profile_revision = profile_revision
        self.cleanup_calls: list[bool] = []
        self.created.append(self)

    def cleanup(self, *, force: bool = False) -> None:
        self.cleanup_calls.append(force)


class _FailingCheckpoint(_CheckpointRecorder):
    def cleanup(self, *, force: bool = False) -> None:
        self.cleanup_calls.append(force)
        raise OSError("checkpoint storage unavailable")


def _delete_command() -> DeleteSession:
    return DeleteSession(
        session_id="codex-thread",
        engine="codex",
        space="code",
        cmd_id="delete-1",
        client_id="client-1",
    )


def _resident(machine, handle: _DeleteHandle):
    ctx = _mk_ctx("codex-thread", "codex-thread")
    ctx.engine = "codex"
    ctx.sdk = handle
    ctx.codex_checkpoint = False
    machine.sessions = {ctx.key: ctx}
    machine.focused_sid = ctx.key
    return ctx


def _prepare(machine, monkeypatch, *, external: bool = False):
    async def is_codex(_sid):
        return True

    async def ensure_generation(*_args, **_kwargs):
        return True

    async def prime_ownership(_sid, **_kwargs):
        return external

    async def list_sessions(_cmd):
        return None

    async def forbidden_rpc(*_args, **_kwargs):
        raise AssertionError(
            "loaded delete must not start a private app-server"
        )

    monkeypatch.setattr(machine, "_is_codex_session", is_codex)
    monkeypatch.setattr(
        machine,
        "_ensure_codex_daemon_generation",
        ensure_generation,
    )
    monkeypatch.setattr(machine, "_prime_codex_ownership", prime_ownership)
    monkeypatch.setattr(machine, "_handle_list_sessions", list_sessions)
    monkeypatch.setattr(machine, "_codex_rpc_for_wire", forbidden_rpc)
    monkeypatch.setattr(machine, "_codex_rollout_for_wire", lambda _sid: None)
    monkeypatch.setattr(
        machine_module,
        "codex_thread_parent_maps",
        lambda **_kwargs: ({}, {}),
    )
    def archived_rollout_record(_sid, **kwargs):
        codex_home = Path(
            kwargs.get("codex_home", machine._codex_profile().home)
        )
        return (
            str(
                codex_home
                / "archived_sessions"
                / "rollout-codex-thread.jsonl"
            ),
            True,
        )

    monkeypatch.setattr(
        machine_module,
        "codex_thread_rollout_record",
        archived_rollout_record,
    )
    for ctx in machine.sessions.values():
        if ctx.engine != "codex" or ctx.space != "code":
            continue
        sid = machine._ctx_wire_sid(ctx)
        machine._watch.setdefault(sid, {
            "engine": "codex",
            "scan_complete": True,
            "active_external_turns": {},
        })


def test_active_codex_thread_must_be_archived_before_delete(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        handle = _DeleteHandle()
        ctx = _resident(machine, handle)
        _prepare(machine, monkeypatch)
        profile = machine._codex_profile()
        active_rollout = str(
            profile.home / "sessions" / "rollout-codex-thread.jsonl"
        )
        monkeypatch.setattr(
            machine_module,
            "codex_thread_rollout_record",
            lambda _sid, **_kwargs: (active_rollout, False),
        )

        result = await machine._handle_delete_session(_delete_command())

        assert isinstance(result, Error)
        assert result.code == ERR_BUSY
        assert "先归档" in result.message
        assert handle.calls == []
        assert machine.sessions[ctx.key] is ctx

    asyncio.run(run())


def test_archived_cross_home_codex_thread_is_not_deleted(monkeypatch, tmp_path):
    async def run():
        machine, _ = _mk_machine()
        handle = _DeleteHandle()
        ctx = _resident(machine, handle)
        _prepare(machine, monkeypatch)
        external_rollout = str(
            tmp_path / "old-codex" / "archived_sessions" / "rollout.jsonl"
        )
        monkeypatch.setattr(
            machine_module,
            "codex_thread_rollout_record",
            lambda _sid, **_kwargs: (external_rollout, True),
        )

        result = await machine._handle_delete_session(_delete_command())

        assert isinstance(result, Error)
        assert result.code == ERR_INTERNAL
        assert "迁移不完整" in result.message
        assert handle.calls == []
        assert machine.sessions[ctx.key] is ctx

    asyncio.run(run())


def test_ordinary_fork_tree_requires_deepest_child_first(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        handle = _DeleteHandle()
        handle.delete_candidates = (
            ("codex-thread", False),
            ("fork-child", False),
            ("fork-grandchild", False),
        )
        ctx = _resident(machine, handle)
        _prepare(machine, monkeypatch)
        parents = {
            "fork-child": "codex-thread",
            "fork-grandchild": "fork-child",
        }
        monkeypatch.setattr(
            machine_module,
            "codex_thread_parent_maps",
            lambda **_kwargs: (parents, parents),
        )

        result = await machine._handle_delete_session(_delete_command())

        assert isinstance(result, Error)
        assert result.code == ERR_BUSY
        assert "最深层派生会话" in result.message
        assert handle.calls == []
        assert machine.sessions[ctx.key] is ctx

    asyncio.run(run())


def test_codex_delete_waits_for_concurrent_fork_tree_lane(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        fork_entered = asyncio.Event()
        release_fork = asyncio.Event()
        delete_entered = asyncio.Event()

        async def is_codex(_sid):
            return True

        async def hold_fork(_cmd):
            fork_entered.set()
            await release_fork.wait()

        async def record_delete(_cmd):
            delete_entered.set()
            return "deleted"

        monkeypatch.setattr(machine, "_is_codex_session", is_codex)
        monkeypatch.setattr(
            machine,
            "_handle_fork_session_in_tree_lane",
            hold_fork,
        )
        monkeypatch.setattr(
            machine,
            "_handle_delete_session_locked",
            record_delete,
        )

        fork_task = asyncio.create_task(machine._handle_fork_session(
            SimpleNamespace(session_id="codex-thread")
        ))
        await fork_entered.wait()
        delete_task = asyncio.create_task(
            machine._handle_delete_session(_delete_command())
        )
        await asyncio.sleep(0)

        assert not delete_entered.is_set()
        assert not delete_task.done()

        release_fork.set()
        await fork_task
        assert await delete_task == "deleted"
        assert delete_entered.is_set()

    asyncio.run(run())


def test_resident_shared_thread_deletes_before_proxy_disconnect(monkeypatch):
    async def run():
        machine, transport = _mk_machine()
        handle = _DeleteHandle()
        ctx = _resident(machine, handle)
        _prepare(machine, monkeypatch)
        machine._codex_session_list_cache = (
            0.0,
            [{"session_id": "codex-thread"}],
            (),
        )

        result = await machine._handle_delete_session(_delete_command())

        assert result is None
        assert handle.calls == [
            ("delete", "codex-thread"),
            ("disconnect", None),
        ]
        assert ctx.key not in machine.sessions
        assert machine.focused_sid is None
        assert machine._codex_session_list_cache is None
        assert not [item for item in transport.sent if isinstance(item, Error)]

    asyncio.run(run())


def test_rejected_loaded_delete_preserves_resident_session(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        failure = CodexAppServerError({
            "code": -32600,
            "message": "thread already has an active writer",
        })
        handle = _DeleteHandle(failure)
        ctx = _resident(machine, handle)
        _prepare(machine, monkeypatch)

        result = await machine._handle_delete_session(_delete_command())

        assert isinstance(result, Error)
        assert result.code == ERR_INTERNAL
        assert handle.calls == [("delete", "codex-thread")]
        assert machine.sessions[ctx.key] is ctx
        assert machine.focused_sid == ctx.key

    asyncio.run(run())


def test_unknown_loaded_delete_preserves_thread_when_exact_read_finds_it(
    monkeypatch,
):
    async def run():
        machine, _ = _mk_machine()
        handle = _DeleteHandle(ConnectionError("proxy closed"))
        ctx = _resident(machine, handle)
        _prepare(machine, monkeypatch)
        machine._codex_forks.begin(
            "fork-request", "parent-thread", "parent-turn", "/repo")
        machine._codex_forks.complete("fork-request", "codex-thread")

        async def session_exists(_sid):
            return True

        monkeypatch.setattr(
            machine,
            "_codex_session_exists",
            session_exists,
        )

        result = await machine._handle_delete_session(_delete_command())

        assert isinstance(result, Error)
        assert result.code == ERR_INTERNAL
        assert handle.calls == [("delete", "codex-thread")]
        assert machine.sessions[ctx.key] is ctx
        assert machine._codex_forks.child_entry(
            "codex-thread")["status"] == "complete"

    asyncio.run(run())


def test_unknown_loaded_delete_commits_after_exact_absence(
    monkeypatch,
    tmp_path: Path,
):
    async def run():
        machine, _ = _mk_machine()
        handle = _DeleteHandle()
        ctx = _resident(machine, handle)
        _prepare(machine, monkeypatch)
        rollout = tmp_path / "rollout.jsonl"
        rollout.write_text("session\n", encoding="utf-8")

        async def delete_then_disconnect(expected_thread_id):
            handle.calls.append(("delete", expected_thread_id))
            rollout.unlink()
            raise ConnectionError("proxy closed")

        async def session_exists(_sid):
            return False

        handle.delete_thread = delete_then_disconnect
        monkeypatch.setattr(machine, "_codex_session_exists", session_exists)
        monkeypatch.setattr(
            machine,
            "_codex_rollout_for_wire",
            lambda _sid: str(rollout),
        )

        result = await machine._handle_delete_session(_delete_command())

        assert result is None
        assert handle.calls == [
            ("delete", "codex-thread"),
            ("disconnect", None),
        ]
        assert ctx.key not in machine.sessions

    asyncio.run(run())


def test_unknown_loaded_delete_preserves_unconfirmed_absence(
    monkeypatch,
    tmp_path: Path,
):
    async def run():
        machine, _ = _mk_machine()
        handle = _DeleteHandle(ConnectionError("proxy closed"))
        ctx = _resident(machine, handle)
        _prepare(machine, monkeypatch)
        machine._codex_forks.begin(
            "fork-request", "parent-thread", "parent-turn", "/repo")
        machine._codex_forks.complete("fork-request", "codex-thread")
        rollout = tmp_path / "rollout.jsonl"
        rollout.write_text("session\n", encoding="utf-8")

        async def session_exists(_sid):
            return False

        monkeypatch.setattr(machine, "_codex_session_exists", session_exists)
        monkeypatch.setattr(
            machine,
            "_codex_rollout_for_wire",
            lambda _sid: str(rollout),
        )

        result = await machine._handle_delete_session(_delete_command())

        assert isinstance(result, Error)
        assert result.code == ERR_INTERNAL
        assert machine.sessions[ctx.key] is ctx
        assert rollout.exists()
        assert machine._codex_forks.child_entry(
            "codex-thread")["status"] == "delete_pending"

    asyncio.run(run())


def test_exact_delete_reconciliation_does_not_use_bounded_catalog(
    monkeypatch,
):
    async def run():
        machine, _ = _mk_machine()
        calls = []

        async def read_thread(sid, method, params=None, **_kwargs):
            calls.append((sid, method, params))
            return {"thread": {"id": "codex-thread"}}

        monkeypatch.setattr(machine, "_codex_rpc_for_wire", read_thread)

        assert await machine._codex_session_exists("codex-thread") is True
        assert calls == [(
            "codex-thread",
            "thread/read",
            {
                "threadId": "codex-thread",
                "includeTurns": False,
            },
        )]

        async def missing_thread(*_args, **_kwargs):
            raise CodexRpcRejected(
                "codex app-server error -32600: "
                "thread not loaded: codex-thread",
                code=-32600,
            )

        monkeypatch.setattr(machine, "_codex_rpc_for_wire", missing_thread)
        assert await machine._codex_session_exists("codex-thread") is False

        async def other_rejection(*_args, **_kwargs):
            raise CodexRpcRejected(
                "codex app-server error -32600: invalid request",
                code=-32600,
            )

        monkeypatch.setattr(machine, "_codex_rpc_for_wire", other_rejection)
        with pytest.raises(CodexRpcRejected, match="invalid request"):
            await machine._codex_session_exists("codex-thread")

    asyncio.run(run())


def test_confirmed_descendant_delete_evicts_both_resident_contexts(
    monkeypatch,
    tmp_path: Path,
):
    async def run():
        machine, _ = _mk_machine()
        root_handle = _DeleteHandle(
            deleted_ids=("child-thread", "codex-thread"),
        )
        root = _resident(machine, root_handle)
        child_handle = _DeleteHandle()
        child_handle.thread_id = "child-thread"
        child = _mk_ctx("child-thread", "child-thread")
        child.engine = "codex"
        child.sdk = child_handle
        child_checkpoint = _CheckpointRecorder(
            child.cwd,
            tmp_path / "state",
            "child-thread",
            profile_revision=machine._codex_profile_revision,
        )
        child.codex_checkpoint = child_checkpoint
        machine.sessions[child.key] = child
        _prepare(machine, monkeypatch)
        cleanup_calls = []
        machine._codex_controls = _DeleteRecorder(
            "controls",
            cleanup_calls,
        )
        machine._session_plans = _DeleteRecorder("plan", cleanup_calls)
        machine._session_presentation = _PresentationDeleteRecorder(
            "presentation",
            cleanup_calls,
        )
        machine._session_pins = _PinRecorder(cleanup_calls)
        machine._watch = {
            "codex-thread": {
                "engine": "codex",
                "scan_complete": True,
            },
            "child-thread": {
                "engine": "codex",
                "scan_complete": True,
            },
        }

        async def session_exists(sid):
            assert sid == "child-thread"
            return False

        monkeypatch.setattr(machine, "_codex_session_exists", session_exists)

        result = await machine._handle_delete_session(_delete_command())

        assert result is None
        assert machine.sessions == {}
        assert root_handle.calls == [
            ("delete", "codex-thread"),
            ("disconnect", None),
        ]
        assert child_handle.calls == [("disconnect", None)]
        assert child_checkpoint.cleanup_calls == [True]
        assert root.key == "codex-thread"
        assert cleanup_calls == [
            ("controls", "codex-thread"),
            ("plan", "codex-thread"),
            ("presentation", "codex-thread"),
            ("pin", "codex", "codex-thread", False),
            ("controls", "child-thread"),
            ("plan", "child-thread"),
            ("presentation", "child-thread"),
            ("pin", "codex", "child-thread", False),
        ]
        assert machine._watch == {}

    asyncio.run(run())


def test_resumed_child_delete_cleans_checkpoint_after_cwd_removal(
    monkeypatch,
    tmp_path: Path,
):
    async def run():
        machine, _ = _mk_machine()
        machine.cfg.state_dir = tmp_path / "state"
        root_handle = _DeleteHandle(
            deleted_ids=("child-thread", "codex-thread"),
        )
        _resident(machine, root_handle)
        child_handle = _DeleteHandle()
        child_handle.thread_id = "child-thread"
        child = _mk_ctx("child-thread", "child-thread")
        child.engine = "codex"
        child.sdk = child_handle
        machine.sessions[child.key] = child

        original_cwd = tmp_path / "removed-repository"
        original_cwd.mkdir()
        subprocess.run(
            ["git", "-C", str(original_cwd), "init"],
            check=True,
            capture_output=True,
        )
        stale_journal = machine_module.CodexCheckpointJournal(
            str(original_cwd),
            machine.cfg.state_dir,
            "child-thread",
            profile_revision=machine._codex_profile_revision,
        )
        assert stale_journal.session_dir.exists()
        shutil.rmtree(original_cwd)
        fallback_cwd = tmp_path / "fallback"
        fallback_cwd.mkdir()
        child.cwd = str(fallback_cwd)
        child.codex_checkpoint = None
        root_handle.parents["child-thread"] = "codex-thread"
        _prepare(machine, monkeypatch)

        async def session_exists(sid):
            assert sid == "child-thread"
            return False

        monkeypatch.setattr(machine, "_codex_session_exists", session_exists)

        result = await machine._handle_delete_session(_delete_command())

        assert result is None
        assert child.codex_checkpoint is False
        assert not stale_journal.session_dir.exists()

    asyncio.run(run())


def test_descendant_checkpoint_oserror_does_not_abort_delete_cleanup(
    monkeypatch,
    tmp_path: Path,
):
    async def run():
        machine, _ = _mk_machine()
        root_handle = _DeleteHandle(
            deleted_ids=("child-thread", "codex-thread"),
        )
        _resident(machine, root_handle)
        child_handle = _DeleteHandle()
        child_handle.thread_id = "child-thread"
        child = _mk_ctx("child-thread", "child-thread")
        child.engine = "codex"
        child.sdk = child_handle
        child_checkpoint = _FailingCheckpoint(
            child.cwd,
            tmp_path / "state",
            "child-thread",
            profile_revision=machine._codex_profile_revision,
        )
        child.codex_checkpoint = child_checkpoint
        machine.sessions[child.key] = child
        root_handle.parents["child-thread"] = "codex-thread"
        _prepare(machine, monkeypatch)
        cleanup_calls = []
        machine._codex_controls = _DeleteRecorder(
            "controls",
            cleanup_calls,
        )
        machine._session_pins = _PinRecorder(cleanup_calls)
        list_calls = []

        async def session_exists(sid):
            assert sid == "child-thread"
            return False

        async def list_sessions(cmd):
            list_calls.append(cmd.cmd_id)

        monkeypatch.setattr(machine, "_codex_session_exists", session_exists)
        monkeypatch.setattr(machine, "_handle_list_sessions", list_sessions)

        result = await machine._handle_delete_session(_delete_command())

        assert result is None
        assert machine.sessions == {}
        assert child_checkpoint.cleanup_calls == [True]
        assert cleanup_calls == [
            ("controls", "codex-thread"),
            ("pin", "codex", "codex-thread", False),
            ("controls", "child-thread"),
            ("pin", "codex", "child-thread", False),
        ]
        assert machine._watch == {}
        assert list_calls == ["delete-1"]

    asyncio.run(run())


def test_confirmed_descendant_delete_evicts_resident_btw_by_native_id(
    monkeypatch,
):
    async def run():
        machine, _ = _mk_machine()
        root_handle = _DeleteHandle(
            deleted_ids=("child-thread", "codex-thread"),
        )
        _resident(machine, root_handle)
        child_handle = _DeleteHandle()
        child_handle.thread_id = "child-thread"
        child = _mk_ctx("btw-child")
        child.engine = "codex"
        child.btw = True
        child.sdk = child_handle
        child.codex_checkpoint = False
        machine.sessions[child.key] = child
        _prepare(machine, monkeypatch)
        cleanup_calls = []
        machine._codex_controls = _DeleteRecorder(
            "controls",
            cleanup_calls,
        )
        machine._session_plans = _DeleteRecorder("plan", cleanup_calls)
        machine._session_presentation = _PresentationDeleteRecorder(
            "presentation",
            cleanup_calls,
        )
        machine._session_pins = _PinRecorder(cleanup_calls)
        machine._watch[child.key] = {"engine": "codex"}
        machine._codex_sidebar_watches[child.key] = None
        owner_calls = []

        async def external_owner(sid):
            owner_calls.append(sid)
            return sid == "child-thread"

        monkeypatch.setattr(
            machine,
            "_codex_delete_external_owner",
            external_owner,
        )

        async def session_exists(sid):
            assert sid == "child-thread"
            return False

        monkeypatch.setattr(machine, "_codex_session_exists", session_exists)

        result = await machine._handle_delete_session(_delete_command())

        assert result is None
        assert machine.sessions == {}
        assert root_handle.calls == [
            ("delete", "codex-thread"),
            ("disconnect", None),
        ]
        assert child_handle.calls == [("disconnect", None)]
        assert owner_calls == ["codex-thread"]
        assert cleanup_calls == [
            ("controls", "codex-thread"),
            ("plan", "codex-thread"),
            ("presentation", "codex-thread"),
            ("pin", "codex", "codex-thread", False),
            ("controls", "child-thread"),
            ("plan", "child-thread"),
            ("presentation", "child-thread"),
            ("pin", "codex", "child-thread", False),
            ("controls", "btw-child"),
            ("plan", "btw-child"),
            ("presentation", "btw-child"),
            ("pin", "codex", "btw-child", False),
        ]
        assert "btw-child" not in machine._watch
        assert "btw-child" not in machine._codex_sidebar_watches

    asyncio.run(run())


def test_confirmed_cold_descendant_delete_cleans_all_metadata(
    monkeypatch,
):
    async def run():
        machine, _ = _mk_machine()
        root_handle = _DeleteHandle(
            deleted_ids=("codex-thread",),
        )
        root_handle.thread_delete_notifications_overflowed = True
        _resident(machine, root_handle)
        root_handle.delete_candidates = (("cold-child", False),)
        root_handle.parents["cold-child"] = "codex-thread"
        _prepare(machine, monkeypatch)
        cleanup_calls = []
        machine._codex_controls = _DeleteRecorder(
            "controls",
            cleanup_calls,
        )
        machine._session_plans = _DeleteRecorder("plan", cleanup_calls)
        machine._session_presentation = _PresentationDeleteRecorder(
            "presentation",
            cleanup_calls,
        )
        machine._session_pins = _PinRecorder(cleanup_calls)
        machine._watch = {
            "codex-thread": {"engine": "codex"},
            "cold-child": {
                "engine": "codex",
                "scan_complete": True,
                "active_external_turns": {},
            },
        }
        machine._watch["codex-thread"]["scan_complete"] = True
        machine._codex_sidebar_watches["cold-child"] = None
        checkpoint_calls = []

        async def session_exists(sid):
            assert sid == "cold-child"
            return False

        async def cleanup_checkpoint(sid):
            checkpoint_calls.append(sid)

        monkeypatch.setattr(
            machine,
            "_codex_session_exists",
            session_exists,
        )
        monkeypatch.setattr(
            machine,
            "_cleanup_cold_deleted_codex_checkpoint",
            cleanup_checkpoint,
        )

        result = await machine._handle_delete_session(_delete_command())

        assert result is None
        assert checkpoint_calls == ["cold-child"]
        assert cleanup_calls == [
            ("controls", "codex-thread"),
            ("plan", "codex-thread"),
            ("presentation", "codex-thread"),
            ("pin", "codex", "codex-thread", False),
            ("controls", "cold-child"),
            ("plan", "cold-child"),
            ("presentation", "cold-child"),
            ("pin", "codex", "cold-child", False),
        ]
        assert machine._watch == {}
        assert machine._codex_sidebar_watches == {}

    asyncio.run(run())


def test_parent_delete_rejects_queued_resident_descendant(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        root_handle = _DeleteHandle(
            deleted_ids=("child-thread", "codex-thread"),
        )
        _resident(machine, root_handle)
        child_handle = _DeleteHandle()
        child_handle.thread_id = "child-thread"
        child = _mk_ctx("child-thread", "child-thread")
        child.engine = "codex"
        child.sdk = child_handle
        child.codex_checkpoint = False
        queued = Query(
            sid="child-thread",
            prompt="keep this work",
            msg_id="queued-child",
            delivery="queue",
            cmd_id="queue-child",
            client_id="client-1",
        )
        child.queued_queries.append(queued)
        machine.sessions[child.key] = child
        root_handle.parents["child-thread"] = "codex-thread"
        _prepare(machine, monkeypatch)

        result = await machine._handle_delete_session(_delete_command())

        assert isinstance(result, Error)
        assert result.code == ERR_BUSY
        assert root_handle.parent_calls == ["child-thread"]
        assert root_handle.calls == []
        assert machine.sessions[child.key] is child
        assert child.queued_queries == [queued]

    asyncio.run(run())


@pytest.mark.parametrize("delivery", ["immediate", "queue", "replace"])
@pytest.mark.parametrize("target_sid", ["codex-thread", "child-thread"])
def test_delete_rejects_query_waiting_for_reconciliation(
    monkeypatch,
    delivery,
    target_sid,
):
    async def run():
        machine, _ = _mk_machine()
        root_handle = _DeleteHandle(
            deleted_ids=("child-thread", "codex-thread"),
        )
        root = _resident(machine, root_handle)
        child_handle = _DeleteHandle()
        child_handle.thread_id = "child-thread"
        child = _mk_ctx("child-thread", "child-thread")
        child.engine = "codex"
        child.sdk = child_handle
        child.codex_checkpoint = False
        machine.sessions[child.key] = child
        root_handle.delete_candidates = (("child-thread", False),)
        root_handle.parents["child-thread"] = "codex-thread"
        _prepare(machine, monkeypatch)
        reconciliation_started = asyncio.Event()
        release_reconciliation = asyncio.Event()

        async def session_exists(sid):
            assert sid == "child-thread"
            reconciliation_started.set()
            await release_reconciliation.wait()
            return False

        monkeypatch.setattr(
            machine,
            "_codex_session_exists",
            session_exists,
        )
        delete_task = asyncio.create_task(
            machine._handle_delete_session(_delete_command())
        )
        await reconciliation_started.wait()
        query = Query(
            sid=target_sid,
            prompt="do not lose this prompt",
            msg_id=f"racing-{delivery}",
            delivery=delivery,
            cmd_id=f"query-{delivery}",
            client_id="client-1",
        )
        query_task = asyncio.create_task(machine._handle_query(query))
        await asyncio.sleep(0)

        assert not query_task.done()
        release_reconciliation.set()
        assert await delete_task is None
        query_result = await query_task

        assert isinstance(query_result, Error)
        assert query_result.code == ERR_NOT_RUNNING
        assert root.queued_queries == []
        assert child.queued_queries == []
        assert "codex-thread" not in machine.sessions
        assert "child-thread" not in machine.sessions

    asyncio.run(run())


@pytest.mark.parametrize("owner_kind", ["private-app", "shared-cli"])
def test_parent_delete_rejects_externally_owned_descendant(
    monkeypatch,
    owner_kind,
):
    async def run():
        machine, _ = _mk_machine()
        root_handle = _DeleteHandle(
            deleted_ids=("child-thread", "codex-thread"),
        )
        _resident(machine, root_handle)
        child_handle = _DeleteHandle()
        child_handle.thread_id = "child-thread"
        child = _mk_ctx("child-thread", "child-thread")
        child.engine = "codex"
        child.sdk = child_handle
        child.codex_checkpoint = False
        machine.sessions[child.key] = child
        root_handle.parents["child-thread"] = "codex-thread"
        _prepare(machine, monkeypatch)
        ownership_calls = []

        async def external_owner(sid):
            ownership_calls.append(sid)
            return owner_kind == "private-app" and sid == "child-thread"

        monkeypatch.setattr(
            machine,
            "_prime_codex_ownership",
            external_owner,
        )
        if owner_kind == "shared-cli":
            machine._watch["child-thread"] = {
                "engine": "codex",
                "active_external_turns": {"cli-turn": 1.0},
            }

        result = await machine._handle_delete_session(_delete_command())

        assert isinstance(result, Error)
        assert result.code == ERR_BUSY
        assert ownership_calls == ["codex-thread", "child-thread"]
        assert root_handle.parent_calls == ["child-thread"]
        assert root_handle.calls == []
        assert machine.sessions[child.key] is child

    asyncio.run(run())


def test_parent_delete_rejects_active_cold_watched_descendant(
    monkeypatch,
):
    async def run():
        machine, _ = _mk_machine()
        root_handle = _DeleteHandle()
        _resident(machine, root_handle)
        root_handle.parents["cold-child"] = "codex-thread"
        _prepare(machine, monkeypatch)
        machine._watch["cold-child"] = {
            "engine": "codex",
            "active_external_turns": {"app-turn": 1.0},
        }
        ownership_calls = []

        async def prime_ownership(sid, *, extra_handles=()):
            ownership_calls.append((sid, extra_handles))
            return False

        monkeypatch.setattr(
            machine,
            "_prime_codex_ownership",
            prime_ownership,
        )

        result = await machine._handle_delete_session(_delete_command())

        assert isinstance(result, Error)
        assert result.code == ERR_BUSY
        assert root_handle.parent_calls == ["cold-child"]
        assert root_handle.calls == []
        assert ownership_calls == [
            ("codex-thread", ()),
            ("cold-child", (root_handle,)),
        ]

    asyncio.run(run())


def test_parent_delete_rejects_active_cold_catalog_descendant(
    monkeypatch,
):
    async def run():
        machine, _ = _mk_machine()
        root_handle = _DeleteHandle()
        _resident(machine, root_handle)
        root_handle.delete_candidates = (("cold-child", True),)
        root_handle.parents["cold-child"] = "codex-thread"
        _prepare(machine, monkeypatch)

        result = await machine._handle_delete_session(_delete_command())

        assert isinstance(result, Error)
        assert result.code == ERR_BUSY
        assert root_handle.parent_calls == ["cold-child"]
        assert root_handle.calls == []

    asyncio.run(run())


def test_parent_delete_ignores_stale_watch_only_thread(
    monkeypatch,
):
    async def run():
        machine, _ = _mk_machine()
        root_handle = _DeleteHandle()
        _resident(machine, root_handle)
        _prepare(machine, monkeypatch)
        machine._watch["stale-thread"] = {"engine": "codex"}

        async def read_parent(thread_id):
            root_handle.parent_calls.append(thread_id)
            raise CodexAppServerError({
                "code": -32600,
                "message": f"thread not loaded: {thread_id}",
            })

        root_handle.read_thread_parent = read_parent

        result = await machine._handle_delete_session(_delete_command())

        assert result is None
        assert root_handle.parent_calls == ["stale-thread"]
        assert root_handle.calls == [
            ("delete", "codex-thread"),
            ("disconnect", None),
        ]

    asyncio.run(run())


def test_unconfirmed_delete_notification_preserves_sibling_context(
    monkeypatch,
):
    async def run():
        machine, _ = _mk_machine()
        root_handle = _DeleteHandle(
            deleted_ids=("unrelated-thread", "codex-thread"),
        )
        _resident(machine, root_handle)
        sibling_handle = _DeleteHandle()
        sibling_handle.thread_id = "unrelated-thread"
        sibling = _mk_ctx("unrelated-thread", "unrelated-thread")
        sibling.engine = "codex"
        sibling.sdk = sibling_handle
        sibling.codex_checkpoint = False
        machine.sessions[sibling.key] = sibling
        _prepare(machine, monkeypatch)

        async def session_exists(sid):
            assert sid == "unrelated-thread"
            return True

        monkeypatch.setattr(machine, "_codex_session_exists", session_exists)

        result = await machine._handle_delete_session(_delete_command())

        assert result is None
        assert machine.sessions == {sibling.key: sibling}
        assert sibling_handle.calls == []

    asyncio.run(run())


def test_multi_profile_delete_keeps_wire_id_out_of_native_rpc(
    monkeypatch,
    tmp_path: Path,
):
    async def run():
        cfg = WrapperConfig()
        cfg.state_dir = tmp_path / "state"
        cfg.claude_work_root = tmp_path / "work" / "claude"
        cfg.codex_work_root = tmp_path / "work" / "codex"
        cfg.codex_profiles_json = json.dumps({
            "primary": {
                "label": "Primary",
                "home": str(tmp_path / "primary"),
                "default": True,
            },
            "stack": {
                "label": "Stack",
                "home": str(tmp_path / "stack"),
            },
        })
        machine = WrapperMachine(cfg, _Transport())
        handle = _DeleteHandle()
        ctx = _mk_ctx("stack@codex-thread", "codex-thread")
        ctx.engine = "codex"
        ctx.codex_profile_id = "stack"
        ctx.sdk = handle
        ctx.codex_checkpoint = False
        machine.sessions[ctx.key] = ctx
        _prepare(machine, monkeypatch)
        command = _delete_command().model_copy(update={
            "session_id": "stack@codex-thread",
        })

        result = await machine._handle_delete_session(command)

        assert result is None
        assert handle.calls[0] == ("delete", "codex-thread")
        assert machine.sessions == {}

    asyncio.run(run())


def test_resident_codex_work_delete_uses_loaded_connection(
    monkeypatch,
):
    async def run():
        machine, _ = _mk_machine()
        store = machine._work.for_engine("codex")
        profile_id = machine._codex_profiles.default.id
        record = store.create_session(codex_profile_id=profile_id)
        store.bind_session(
            record.work_id,
            "codex-thread",
            codex_profile_id=profile_id,
        )
        handle = _DeleteHandle()
        ctx = _mk_ctx("codex-thread", "codex-thread")
        ctx.engine = "codex"
        ctx.space = "work"
        ctx.work_id = record.work_id
        ctx.codex_profile_id = profile_id
        ctx.sdk = handle
        machine.sessions[ctx.key] = ctx
        _prepare(machine, monkeypatch)
        command = DeleteWorkSession(
            session_id="codex-thread",
            engine="codex",
            cmd_id="delete-work-1",
            client_id="client-1",
        )

        result = await machine._handle_delete_work_session(command)

        assert result is None
        assert handle.calls == [
            ("delete", "codex-thread"),
            ("disconnect", None),
        ]
        assert machine.sessions == {}
        assert store.get_by_session(
            "codex-thread",
            codex_profile_id=profile_id,
        ) is None

    asyncio.run(run())


def test_rejected_codex_work_delete_preserves_registry_and_context(
    monkeypatch,
):
    async def run():
        machine, _ = _mk_machine()
        store = machine._work.for_engine("codex")
        profile_id = machine._codex_profiles.default.id
        record = store.create_session(codex_profile_id=profile_id)
        store.bind_session(
            record.work_id,
            "codex-thread",
            codex_profile_id=profile_id,
        )
        failure = CodexAppServerError({
            "code": -32600,
            "message": "thread already has an active writer",
        })
        handle = _DeleteHandle(failure)
        ctx = _mk_ctx("codex-thread", "codex-thread")
        ctx.engine = "codex"
        ctx.space = "work"
        ctx.work_id = record.work_id
        ctx.codex_profile_id = profile_id
        ctx.sdk = handle
        machine.sessions[ctx.key] = ctx
        _prepare(machine, monkeypatch)
        command = DeleteWorkSession(
            session_id="codex-thread",
            engine="codex",
            cmd_id="delete-work-1",
            client_id="client-1",
        )

        result = await machine._handle_delete_work_session(command)

        assert isinstance(result, Error)
        assert result.request_id == command.cmd_id
        assert handle.calls == [("delete", "codex-thread")]
        assert machine.sessions == {ctx.key: ctx}
        assert store.get_by_session(
            "codex-thread",
            codex_profile_id=profile_id,
        ) is not None

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["missing", "running", "queued", "rpc"])
def test_work_delete_errors_are_correlated_to_request(monkeypatch, failure):
    async def run():
        machine, transport = _mk_machine()
        store = machine._work.for_engine("codex")
        profile_id = machine._codex_profiles.default.id
        if failure != "missing":
            record = store.create_session(codex_profile_id=profile_id)
            store.bind_session(record.work_id, "codex-thread",
                               codex_profile_id=profile_id)
        if failure in {"running", "queued"}:
            ctx = _mk_ctx("codex-thread", "codex-thread")
            ctx.engine, ctx.space = "codex", "work"
            ctx.work_id, ctx.codex_profile_id = record.work_id, profile_id
            ctx.sdk = _DeleteHandle()
            if failure == "running":
                ctx.state = "running"
            else:
                ctx.queued_query_starting_msg_id = "queued"
            machine.sessions[ctx.key] = ctx

        async def reject(*args, **kwargs):
            raise OSError("delete unavailable")

        monkeypatch.setattr(machine, "_codex_rpc_for_wire", reject)
        command = DeleteWorkSession(
            session_id="codex-thread", engine="codex",
            cmd_id="delete-work-failed", client_id="client-1",
        )
        result = await machine._handle_delete_work_session(command)
        assert isinstance(result, Error)
        assert result.request_id == command.cmd_id
        assert result.to == command.client_id
        assert result.sid == command.session_id
        assert result in transport.sent
        if failure != "missing":
            assert store.get_by_session("codex-thread",
                                        codex_profile_id=profile_id) is not None

    asyncio.run(run())


def test_cold_codex_delete_bypasses_full_resident_pool(
    monkeypatch,
    tmp_path: Path,
):
    async def run():
        machine, _ = _mk_machine()
        machine.cfg.cc_cwd = str(tmp_path)
        machine.cfg.max_concurrent_sessions = 1
        machine.WATCH_MAX = 1
        active = _mk_ctx("active-thread", "active-thread")
        active.state = "running"
        machine.sessions = {active.key: active}
        machine.focused_sid = active.key
        original_watch = {"watched-thread": {"engine": "claude"}}
        machine._watch = dict(original_watch)
        machine._codex_sidebar_watches["watched-thread"] = None
        _prepare(machine, monkeypatch)
        _ColdDeleteHandle.created = []
        monkeypatch.setattr(
            machine_module,
            "CodexHandle",
            _ColdDeleteHandle,
        )
        monkeypatch.setattr(
            machine,
            "_codex_cwd_for_wire",
            lambda _sid: str(tmp_path),
        )

        async def forbidden_spawn(*_args, **_kwargs):
            raise AssertionError("cold deletion must not enter the pool")

        monkeypatch.setattr(machine, "_spawn", forbidden_spawn)

        result = await machine._handle_delete_session(_delete_command())

        assert result is None
        assert machine.sessions == {active.key: active}
        assert machine.focused_sid == active.key
        assert machine._watch == original_watch
        assert list(machine._codex_sidebar_watches) == ["watched-thread"]
        assert len(_ColdDeleteHandle.created) == 1
        handle = _ColdDeleteHandle.created[0]
        assert handle.calls == [
            (
                "connect",
                {
                    "cwd": str(tmp_path),
                    "control_only": True,
                },
            ),
            ("delete", "codex-thread"),
            ("disconnect", None),
        ]

    asyncio.run(run())


def test_cold_codex_delete_disconnects_when_fork_journal_fails(
    monkeypatch,
    tmp_path: Path,
):
    async def run():
        machine, _ = _mk_machine()
        machine.cfg.cc_cwd = str(tmp_path)
        _prepare(machine, monkeypatch)
        _ColdDeleteHandle.created = []
        monkeypatch.setattr(
            machine_module,
            "CodexHandle",
            _ColdDeleteHandle,
        )
        monkeypatch.setattr(
            machine,
            "_codex_cwd_for_wire",
            lambda _sid: str(tmp_path),
        )

        def fail_begin_delete(_sid):
            raise ForkJournalError("disk full")

        monkeypatch.setattr(
            machine._codex_forks,
            "begin_delete",
            fail_begin_delete,
        )

        result = await machine._handle_delete_session(_delete_command())

        assert isinstance(result, Error)
        assert result.code == ERR_INTERNAL
        assert len(_ColdDeleteHandle.created) == 1
        assert _ColdDeleteHandle.created[0].calls == [
            (
                "connect",
                {
                    "cwd": str(tmp_path),
                    "control_only": True,
                },
            ),
            ("disconnect", None),
        ]

    asyncio.run(run())


def test_cold_codex_delete_disconnects_when_transient_context_is_busy(
    monkeypatch,
    tmp_path: Path,
):
    async def run():
        machine, _ = _mk_machine()
        machine.cfg.cc_cwd = str(tmp_path)
        _prepare(machine, monkeypatch)
        _ColdDeleteHandle.created = []
        monkeypatch.setattr(
            machine_module,
            "CodexHandle",
            _ColdDeleteHandle,
        )
        monkeypatch.setattr(
            machine,
            "_codex_cwd_for_wire",
            lambda _sid: str(tmp_path),
        )

        original_connect = _ColdDeleteHandle.connect

        async def connect_busy(handle, **kwargs):
            await original_connect(handle, **kwargs)
            handle.turn_active = True

        monkeypatch.setattr(_ColdDeleteHandle, "connect", connect_busy)

        result = await machine._handle_delete_session(_delete_command())

        assert isinstance(result, Error)
        assert result.code == ERR_BUSY
        assert len(_ColdDeleteHandle.created) == 1
        assert _ColdDeleteHandle.created[0].calls == [
            (
                "connect",
                {
                    "cwd": str(tmp_path),
                    "control_only": True,
                },
            ),
            ("disconnect", None),
        ]

    asyncio.run(run())


def test_cold_codex_delete_rejects_active_catalog_root_without_rollout(
    monkeypatch,
    tmp_path: Path,
):
    async def run():
        machine, _ = _mk_machine()
        machine.cfg.cc_cwd = str(tmp_path)
        _prepare(machine, monkeypatch)
        _ColdDeleteHandle.created = []
        monkeypatch.setattr(
            machine_module,
            "CodexHandle",
            _ColdDeleteHandle,
        )
        monkeypatch.setattr(
            machine,
            "_codex_cwd_for_wire",
            lambda _sid: str(tmp_path),
        )

        original_connect = _ColdDeleteHandle.connect

        async def connect_with_active_root(handle, **kwargs):
            await original_connect(handle, **kwargs)
            handle.delete_candidates = (("codex-thread", True),)

        monkeypatch.setattr(
            _ColdDeleteHandle,
            "connect",
            connect_with_active_root,
        )

        result = await machine._handle_delete_session(_delete_command())

        assert isinstance(result, Error)
        assert result.code == ERR_BUSY
        assert len(_ColdDeleteHandle.created) == 1
        assert _ColdDeleteHandle.created[0].calls == [
            (
                "connect",
                {
                    "cwd": str(tmp_path),
                    "control_only": True,
                },
            ),
            ("disconnect", None),
        ]

    asyncio.run(run())


def test_cold_codex_delete_does_not_resume_when_shared_control_is_unavailable(
    monkeypatch,
    tmp_path: Path,
):
    async def run():
        machine, _ = _mk_machine()
        machine.cfg.cc_cwd = str(tmp_path)
        _prepare(machine, monkeypatch)
        _ColdDeleteHandle.created = []
        monkeypatch.setattr(
            machine_module,
            "CodexHandle",
            _FallbackColdDeleteHandle,
        )
        monkeypatch.setattr(
            machine,
            "_codex_cwd_for_wire",
            lambda _sid: str(tmp_path),
        )

        result = await machine._handle_delete_session(_delete_command())

        assert isinstance(result, Error)
        assert result.code == ERR_NOT_RUNNING
        assert len(_ColdDeleteHandle.created) == 1
        shared = _ColdDeleteHandle.created[0]
        assert shared.daemon_mode == "auto"
        assert shared.calls == [
            (
                "connect",
                {
                    "cwd": str(tmp_path),
                    "control_only": True,
                },
            ),
            ("disconnect", None),
        ]

    asyncio.run(run())


def test_cold_codex_delete_cleans_checkpoint_without_fallback_cwd(
    monkeypatch,
    tmp_path: Path,
):
    async def run():
        machine, _ = _mk_machine()
        machine.cfg.cc_cwd = str(tmp_path)
        _prepare(machine, monkeypatch)
        _ColdDeleteHandle.created = []
        cleanup_calls = []
        monkeypatch.setattr(
            machine_module,
            "CodexHandle",
            _ColdDeleteHandle,
        )

        def cleanup_checkpoint(state_dir, sid):
            cleanup_calls.append((state_dir, sid))
            return 1

        def forbidden_checkpoint(*_args, **_kwargs):
            raise AssertionError(
                "cold root cleanup must not depend on fallback cwd"
            )

        monkeypatch.setattr(
            machine_module,
            "CodexCheckpointJournal",
            forbidden_checkpoint,
        )
        monkeypatch.setattr(
            machine_module,
            "cleanup_codex_checkpoint_session",
            cleanup_checkpoint,
        )
        monkeypatch.setattr(
            machine,
            "_codex_cwd_for_wire",
            lambda _sid: str(tmp_path / "removed-native-cwd"),
        )

        result = await machine._handle_delete_session(_delete_command())

        assert result is None
        assert cleanup_calls == [(
            machine.cfg.state_dir,
            "codex-thread",
        )]

    asyncio.run(run())


@pytest.mark.parametrize("watched", [False, True])
def test_cold_multi_profile_delete_excludes_its_control_proxy(
    monkeypatch,
    tmp_path: Path,
    watched: bool,
):
    async def run():
        cfg = WrapperConfig()
        cfg.state_dir = tmp_path / "state"
        cfg.cc_cwd = str(tmp_path)
        cfg.claude_work_root = tmp_path / "work" / "claude"
        cfg.codex_work_root = tmp_path / "work" / "codex"
        cfg.codex_profiles_json = json.dumps({
            "primary": {
                "label": "Primary",
                "home": str(tmp_path / "primary"),
                "default": True,
            },
            "stack": {
                "label": "Stack",
                "home": str(tmp_path / "stack"),
            },
        })
        machine = WrapperMachine(cfg, _Transport())
        _prepare(machine, monkeypatch)
        rollout = tmp_path / "rollout.jsonl"
        rollout.write_bytes(b"")
        _ColdDeleteHandle.created = []
        monkeypatch.setattr(
            machine_module,
            "CodexHandle",
            _ColdDeleteHandle,
        )
        monkeypatch.setattr(
            machine,
            "_codex_cwd_for_wire",
            lambda _sid: str(tmp_path),
        )
        monkeypatch.setattr(
            machine,
            "_codex_rollout_for_wire",
            lambda _sid: str(rollout),
        )
        if watched:
            monkeypatch.setattr(
                machine,
                "_prime_codex_ownership",
                WrapperMachine._prime_codex_ownership.__get__(machine),
            )
            machine._watch_session("primary@codex-thread")
        proxy = ProcessIdentity(4242, 42)

        def identity(pid, *, parent_pid=None):
            assert parent_pid is not None
            return proxy if pid == proxy.pid else None

        own_sets = []

        def holders(paths, own, **_kwargs):
            own_set = set(own)
            own_sets.append(own_set)
            client_proxies = {} if proxy in own_set else {proxy: 1}
            return HolderScan(
                holders={sid: set() for sid in paths},
                complete=True,
                passive_holders={sid: set() for sid in paths},
                client_proxies=client_proxies,
                private_holders={sid: set() for sid in paths},
            )

        tracker_calls = []

        class UnreadableTracker:
            @staticmethod
            def bindings(paths, proxies):
                tracker_calls.append((dict(paths), dict(proxies)))
                return {}, False

        monkeypatch.setattr(machine_module, "process_identity", identity)
        monkeypatch.setattr(
            machine_module,
            "writable_rollout_holders",
            holders,
        )
        machine._codex_tui_log_trackers = {
            profile.id: UnreadableTracker()
            for profile in machine._codex_profiles
        }

        command = _delete_command().model_copy(update={
            "session_id": "primary@codex-thread",
        })
        result = await machine._handle_delete_session(command)

        assert result is None
        assert own_sets == [{proxy}]
        assert tracker_calls == [
            ({"codex-thread": str(rollout)}, {}),
        ]
        assert _ColdDeleteHandle.created[0].calls == [
            (
                "connect",
                {
                    "cwd": str(tmp_path),
                    "control_only": True,
                },
            ),
            ("delete", "codex-thread"),
            ("disconnect", None),
        ]

    asyncio.run(run())


def test_cold_codex_delete_preserves_holderless_app_activity(
    monkeypatch,
    tmp_path: Path,
):
    async def run():
        machine, _ = _mk_machine()
        machine.cfg.cc_cwd = str(tmp_path)
        _prepare(machine, monkeypatch)
        rollout = tmp_path / "rollout.jsonl"
        rollout.write_bytes((json.dumps({
            "type": "event_msg",
            "payload": {
                "type": "task_started",
                "turn_id": "private-app-turn",
            },
        }) + "\n").encode())
        _ColdDeleteHandle.created = []
        monkeypatch.setattr(
            machine_module,
            "CodexHandle",
            _ColdDeleteHandle,
        )
        monkeypatch.setattr(
            machine,
            "_codex_cwd_for_wire",
            lambda _sid: str(tmp_path),
        )
        monkeypatch.setattr(
            machine,
            "_codex_rollout_for_wire",
            lambda _sid: str(rollout),
        )

        async def no_holders(paths, *, extra_handles=()):
            assert paths == {"codex-thread": str(rollout)}
            assert extra_handles == (_ColdDeleteHandle.created[0],)
            return HolderScan(
                holders={"codex-thread": set()},
                complete=True,
                passive_holders={"codex-thread": set()},
                private_holders={"codex-thread": set()},
            )

        monkeypatch.setattr(machine, "_probe_codex_holders", no_holders)

        result = await machine._handle_delete_session(_delete_command())

        assert isinstance(result, Error)
        assert result.code == ERR_BUSY
        assert _ColdDeleteHandle.created[0].calls == [
            (
                "connect",
                {
                    "cwd": str(tmp_path),
                    "control_only": True,
                },
            ),
            ("disconnect", None),
        ]

    asyncio.run(run())


def test_cold_codex_delete_rejects_active_watched_holder(
    monkeypatch,
    tmp_path: Path,
):
    async def run():
        machine, _ = _mk_machine()
        machine.cfg.cc_cwd = str(tmp_path)
        _prepare(machine, monkeypatch)
        machine._watch["codex-thread"] = {
            "engine": "codex",
            "scan_complete": False,
            "active_external_turns": {},
            "holders": set(),
        }
        _ColdDeleteHandle.created = []
        monkeypatch.setattr(
            machine_module,
            "CodexHandle",
            _ColdDeleteHandle,
        )
        monkeypatch.setattr(
            machine,
            "_codex_cwd_for_wire",
            lambda _sid: str(tmp_path),
        )

        async def active_holder(sid, *, extra_handles=()):
            assert sid == "codex-thread"
            assert extra_handles == (_ColdDeleteHandle.created[0],)
            watch = machine._watch[sid]
            watch["scan_complete"] = True
            watch["active_external_turns"] = {"turn-1": 1.0}
            watch["holders"] = {object()}
            return False

        monkeypatch.setattr(
            machine,
            "_prime_codex_ownership",
            active_holder,
        )

        result = await machine._handle_delete_session(_delete_command())

        assert isinstance(result, Error)
        assert result.code == ERR_BUSY
        assert len(_ColdDeleteHandle.created) == 1
        assert _ColdDeleteHandle.created[0].calls == [
            (
                "connect",
                {
                    "cwd": str(tmp_path),
                    "control_only": True,
                },
            ),
            ("disconnect", None),
        ]

    asyncio.run(run())


def test_delete_rejects_stale_idle_state_with_active_turn_task(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        handle = _DeleteHandle()
        ctx = _resident(machine, handle)
        _prepare(machine, monkeypatch)
        release = asyncio.Event()
        ctx.turn_task = asyncio.create_task(release.wait())

        try:
            result = await machine._handle_delete_session(_delete_command())
        finally:
            release.set()
            await ctx.turn_task

        assert isinstance(result, Error)
        assert result.code == ERR_BUSY
        assert handle.calls == []
        assert machine.sessions[ctx.key] is ctx

    asyncio.run(run())


@pytest.mark.parametrize(
    "attribute",
    ["turn_active", "turn_start_pending"],
)
def test_delete_rejects_sdk_turn_boundaries(monkeypatch, attribute):
    async def run():
        machine, _ = _mk_machine()
        handle = _DeleteHandle()
        setattr(handle, attribute, True)
        ctx = _resident(machine, handle)
        _prepare(machine, monkeypatch)

        result = await machine._handle_delete_session(_delete_command())

        assert isinstance(result, Error)
        assert result.code == ERR_BUSY
        assert handle.calls == []
        assert machine.sessions[ctx.key] is ctx

    asyncio.run(run())


def test_delete_rechecks_busy_state_after_control_preflight(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        handle = _DeleteHandle()
        ctx = _resident(machine, handle)
        _prepare(machine, monkeypatch)

        async def state_changes(*_args, **_kwargs):
            handle.turn_start_pending = True
            return None

        monkeypatch.setattr(
            machine,
            "_runtime_control_preflight",
            state_changes,
        )

        result = await machine._handle_delete_session(_delete_command())

        assert isinstance(result, Error)
        assert result.code == ERR_BUSY
        assert handle.calls == []
        assert machine.sessions[ctx.key] is ctx

    asyncio.run(run())


def test_delete_rejects_private_codex_app_owner(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        handle = _DeleteHandle()
        ctx = _resident(machine, handle)
        _prepare(machine, monkeypatch, external=True)

        result = await machine._handle_delete_session(_delete_command())

        assert isinstance(result, Error)
        assert result.code == ERR_BUSY
        assert handle.calls == []
        assert machine.sessions[ctx.key] is ctx

    asyncio.run(run())


@pytest.mark.parametrize("reason", ["missing-rollout", "watch-cap"])
def test_delete_fails_closed_without_owner_watch(
    monkeypatch,
    tmp_path: Path,
    reason: str,
):
    async def run():
        machine, _ = _mk_machine()
        handle = _DeleteHandle()
        ctx = _resident(machine, handle)
        _prepare(machine, monkeypatch)
        machine._watch.pop("codex-thread", None)

        if reason == "watch-cap":
            rollout = tmp_path / "rollout.jsonl"
            rollout.write_bytes(b"")
            monkeypatch.setattr(
                machine,
                "_codex_rollout_for_wire",
                lambda _sid: str(rollout),
            )
            machine.WATCH_MAX = 1
            machine._watch["protected-thread"] = {
                "engine": "codex",
                "external": True,
                "scan_complete": True,
            }

        result = await machine._handle_delete_session(_delete_command())

        assert isinstance(result, Error)
        assert result.code == ERR_BUSY
        assert handle.calls == []
        assert machine.sessions[ctx.key] is ctx
        assert "codex-thread" not in machine._watch

    asyncio.run(run())


def test_delete_fails_closed_when_owner_scan_is_incomplete(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        handle = _DeleteHandle()
        ctx = _resident(machine, handle)
        _prepare(machine, monkeypatch)
        machine._watch["codex-thread"]["scan_complete"] = False

        result = await machine._handle_delete_session(_delete_command())

        assert isinstance(result, Error)
        assert result.code == ERR_BUSY
        assert handle.calls == []
        assert machine.sessions[ctx.key] is ctx

    asyncio.run(run())


def test_delete_rejects_active_shared_cli_turn(monkeypatch):
    async def run():
        machine, _ = _mk_machine()
        handle = _DeleteHandle()
        ctx = _resident(machine, handle)
        _prepare(machine, monkeypatch)
        machine._watch["codex-thread"] = {
            "engine": "codex",
            "active_external_turns": {"cli-turn": 1.0},
        }

        result = await machine._handle_delete_session(_delete_command())

        assert isinstance(result, Error)
        assert result.code == ERR_BUSY
        assert handle.calls == []
        assert machine.sessions[ctx.key] is ctx

    asyncio.run(run())
