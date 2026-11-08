"""0.167 control-plane contracts: no model calls or real account writes."""
import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from cc_remote.protocol import AcknowledgeCompletion, ListSessions, PinSession
from cc_remote.wrapper.codex_approvals import approval_choices, approval_prompt
from cc_remote.wrapper.codex_handle import CodexAppServerError, CodexHandle
from cc_remote.wrapper.codex_organization import PINNED_SECTION_ID, read_receipt_id
from cc_remote.wrapper.codex_rpc import CodexRpcOutcomeUnknown, CodexRpcRejected, _rpc_error
from cc_remote.wrapper.codex_sessions import _normalize_thread
from cc_remote.wrapper.session_presentation import SessionPresentationStore
from tests.test_codex_controls import _Cfg, _dispatch_request
from tests.test_codex_profiles import _context, _machine
from tests.test_presentation_sync import _success


NETWORK = {"applyNetworkPolicyAmendment": {"network_policy_amendment": {
    "host": "example.com", "action": "allow",
}}}
EXEC = {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["git", "status"]}}


@pytest.mark.parametrize("decision", [NETWORK, EXEC, "accept"])
def test_exact_offered_decision_crosses_handle_and_question_bridge(tmp_path, decision):
    async def run():
        machine, _ = _machine(tmp_path)
        params = {"availableDecisions": [decision, "decline"], "command": "git status"}
        options = approval_choices("item/commandExecution/requestApproval", params)
        machine._on_ask_optional = AsyncMock(return_value=options[0][0]["label"])
        handle = CodexHandle(_Cfg(), approval_callback=lambda method, data:
                             machine._on_codex_approval(None, method, data))
        handle.approval = "on-request"
        reply = await _dispatch_request(handle, "item/commandExecution/requestApproval", params)
        assert reply["result"]["decision"] == decision
        assert len(machine._on_ask_optional.call_args.args[2]) == 2
    asyncio.run(run())


@pytest.mark.parametrize("decision", ["acceptForSession", NETWORK, {"unknownGrant": True}])
def test_unoffered_grant_fails_closed(decision):
    async def run():
        handle = CodexHandle(_Cfg(), approval_callback=AsyncMock(return_value=decision))
        handle.approval = "on-request"
        result = await _dispatch_request(handle, "item/commandExecution/requestApproval", {
            "availableDecisions": ["accept", "decline", "cancel"],
        })
        assert result["result"] == {"decision": "decline"}
    asyncio.run(run())


def test_network_and_extra_permission_scope_is_visible():
    prompt = approval_prompt("item/commandExecution/requestApproval", {
        "command": "internal network operation",
        "networkApprovalContext": {"host": "example.com:8443", "protocol": "https"},
        "additionalPermissions": {"fileSystem": {"write": ["/work/output"]}},
    })
    assert "网络访问" in prompt and "example.com:8443" in prompt and "https" in prompt
    assert "/work/output" in prompt and "internal network operation" not in prompt


def test_native_sections_use_identity_and_keep_thread_routing():
    row = _normalize_thread({"id": "child", "sessionId": "root",
                             "section": {"id": PINNED_SECTION_ID, "name": "重命名"}}, archived=False)
    assert row["session_id"] == "child" and row["native_pinned"] is True
    assert _normalize_thread({"id": "a", "section": {"id": "custom", "name": "Pinned"}},
                             archived=False)["native_pinned"] is False
    assert "native_pinned" not in _normalize_thread({"id": "old"}, archived=False)


@pytest.mark.parametrize("pinned", [True, False])
def test_pin_uses_profile_scoped_native_metadata_only(tmp_path, monkeypatch, pinned):
    async def run():
        machine, _ = _machine(tmp_path)
        calls = []
        async def rpc(profile, method, params, **kwargs):
            calls.append((profile.id, method, params))
            if method == "thread/read":
                return {"thread": {"id": "thread", "section": None if pinned else {"id": PINNED_SECTION_ID}}}
            return {}
        monkeypatch.setattr(machine, "_codex_rpc_for_profile", rpc)
        assert await machine._set_native_codex_pin("stack@thread", pinned)
        assert calls == [
            ("stack", "thread/read", {"threadId": "thread", "includeTurns": False}),
            ("stack", "thread/section/move", {"threadId": "thread", "sectionId": PINNED_SECTION_ID if pinned else None}),
        ]
        assert not machine.sessions
    asyncio.run(run())


def test_unpin_preserves_custom_native_section_and_unknown_outcome(tmp_path):
    async def run():
        machine, _ = _machine(tmp_path)
        machine._codex_rpc_for_wire = AsyncMock(return_value={"thread": {
            "id": "thread", "section": {"id": "custom"},
        }})
        assert await machine._set_native_codex_pin("stack@thread", False)
        assert machine._codex_rpc_for_wire.await_count == 1
        machine._codex_rpc_for_wire = AsyncMock(side_effect=CodexRpcOutcomeUnknown())
        with pytest.raises(CodexRpcOutcomeUnknown):
            await machine._set_native_codex_pin("stack@thread", True)
    asyncio.run(run())


@pytest.mark.parametrize("conflict", [False, True])
def test_read_ack_never_uses_a_revision_the_client_did_not_see(tmp_path, conflict):
    async def run():
        machine, _ = _machine(tmp_path)
        seen = {"revision": "seen", "firstUnread": {"type": "turn", "turnId": "turn-1"}}
        receipt = read_receipt_id(seen)
        machine._session_presentation.sync_codex_read_states({"stack@thread": (receipt, True)})
        current = {**seen, "revision": "newer"} if conflict else seen
        calls = []
        async def rpc(profile, method, params, **kwargs):
            calls.append((profile.id, method, params))
            if method == "thread/read":
                return {"thread": {"id": "thread"}, "readState": current}
            assert params["expectedRevision"] == "seen"
            return {"readState": {"revision": "acknowledged", "firstUnread": None}}
        machine._codex_rpc_for_profile = rpc
        event = await machine._handle_acknowledge_completion(AcknowledgeCompletion(
            sid="stack@thread", completion_id=receipt))
        assert all(profile == "stack" for profile, _, _ in calls)
        assert len(calls) == (1 if conflict else 2)
        assert event.type == ("error" if conflict else "completion_state")
        assert machine._session_presentation.get("codex", "stack@thread").completion_unread is conflict
    asyncio.run(run())


def test_read_conflict_and_timeout_do_not_fall_back_or_retry(tmp_path):
    async def run():
        machine, _ = _machine(tmp_path)
        state = {"revision": "r", "firstUnread": {"type": "threadStart"}}
        receipt = read_receipt_id(state)
        machine._session_presentation.sync_codex_read_states({"stack@t": (receipt, True)})
        for error in [CodexRpcRejected("conflict", code=-32600), CodexRpcOutcomeUnknown()]:
            machine._codex_rpc_for_wire = AsyncMock(side_effect=[
                {"thread": {"id": "t"}, "readState": state}, error])
            await machine._handle_acknowledge_completion(AcknowledgeCompletion(sid="stack@t", completion_id=receipt))
            assert machine._codex_rpc_for_wire.await_count == 2
            assert machine._session_presentation.get("codex", "stack@t").completion_unread
    asyncio.run(run())


def test_native_receipt_roundtrip_keeps_existing_store_schema(tmp_path):
    store = SessionPresentationStore(tmp_path)
    store.mark_completion("claude", "same", "claude-completion")
    store.sync_codex_read_states({"same": ("codex-read-a", True)})
    first = store.get("codex", "same")
    store.sync_codex_read_states({"same": ("codex-read-a", True)})
    assert store.get("codex", "same") == first
    store.sync_codex_read_states({"same": ("codex-read-b", False)})
    reloaded = SessionPresentationStore(tmp_path)
    assert not reloaded.get("codex", "same").completion_unread
    assert reloaded.get("claude", "same").completion_unread
    assert reloaded.get("codex", "same").completion_revision == first.completion_revision + 1


def test_provider_mismatch_requires_authoritative_type():
    error = {"code": -32600, "message": "provider changed", "data": {"type": "modelProviderMismatch"}}
    assert CodexAppServerError(error).model_provider_mismatch
    assert _rpc_error(error).model_provider_mismatch
    assert not CodexAppServerError({"code": -32600, "message": "modelProviderMismatch"}).model_provider_mismatch


def test_metadata_notifications_invalidate_without_entering_turn_stream():
    async def run():
        handle = CodexHandle(_Cfg())
        handle._using_daemon_proxy = True
        handle.thread_id = "current"
        seen = []
        handle.metadata_changed_callback = seen.append
        for method in ["thread/readState/changed", "thread/section/changed"]:
            await handle._dispatch({"method": method, "params": {"threadId": "sibling"}})
        assert seen == ["sibling", "sibling"]
        assert handle.thread_id == "current" and not handle.turn_active
    asyncio.run(run())


def test_old_native_catalog_cannot_erase_new_local_completion(tmp_path):
    store = SessionPresentationStore(tmp_path)
    store.sync_codex_read_states({"stack@t": ("codex-read-old", True)})
    before_read = store.completion_revisions("codex")
    store.mark_completion("codex", "stack@t", "new-terminal")
    store.sync_codex_read_states({"stack@t": ("codex-read-old", False)},
                                expected_revisions=before_read)
    assert store.get("codex", "stack@t").completion_id == "new-terminal"
    assert store.get("codex", "stack@t").completion_unread


def test_native_catalog_projects_receipts_and_fetches_old_pins(tmp_path, monkeypatch):
    from cc_remote.wrapper import codex_sessions

    async def run():
        calls = []
        async def rpc(method, params, **kwargs):
            assert method == "thread/list"
            assert kwargs["codex_home"] == str(tmp_path)
            calls.append(params)
            if params.get("archived"):
                return {"data": [], "readStates": None}
            sid = "old-pin" if params.get("sectionId") else "recent"
            return {"data": [{"id": sid, "section": {"id": PINNED_SECTION_ID} if sid == "old-pin" else None}],
                    "readStates": {sid: {"revision": sid, "firstUnread": {"type": "threadStart"}},
                                   "unlisted": {"revision": "foreign"}}}
        monkeypatch.setattr(codex_sessions, "codex_rpc", rpc)
        rows = await codex_sessions.list_codex_sessions(1, codex_home=tmp_path)
        assert {row["session_id"] for row in rows} == {"recent", "old-pin"}
        assert all(row["native_read_state"]["revision"] == row["session_id"] for row in rows)
        assert len(calls) == 3 and calls[-1]["useStateDbOnly"] is True
    asyncio.run(run())


def test_many_approval_choices_use_existing_bounded_question_pages(tmp_path):
    async def run():
        machine, _ = _machine(tmp_path)
        machine._on_ask_optional = AsyncMock(side_effect=["下一页选项", "拒绝"])
        result = await machine._on_codex_approval(None, "item/commandExecution/requestApproval", {
            "availableDecisions": ["accept", "acceptForSession", EXEC, NETWORK, "decline", "cancel"],
        })
        assert result == "decline"
        assert all(len(call.args[2]) <= 5 for call in machine._on_ask_optional.call_args_list)
    asyncio.run(run())


def test_provider_mismatch_never_bootstraps_a_replacement_thread(tmp_path, monkeypatch):
    async def run():
        machine, transport = _machine(tmp_path)
        machine.cfg.cc_cwd = str(tmp_path)
        machine._resolve_codex_profile_model = AsyncMock(return_value=(None, []))
        connect = AsyncMock(side_effect=CodexAppServerError({
            "code": -32600, "data": {"type": "modelProviderMismatch"},
        }))
        disconnect = AsyncMock()
        monkeypatch.setattr(CodexHandle, "connect", connect)
        monkeypatch.setattr(CodexHandle, "disconnect", disconnect)
        result = await machine._spawn("old-thread", bootstrap=True, engine="codex",
                                      codex_profile_id="stack")
        assert result is None and not machine.sessions
        assert connect.await_count == 1
        assert connect.call_args.kwargs["resume_id"] == "old-thread"
        assert disconnect.await_count == 1
        assert any("切回原服务商" in getattr(event, "message", "") for event in transport.sent)
    asyncio.run(run())


@pytest.mark.parametrize("cached", [False, True])
def test_native_pin_outage_cannot_report_successful_unpin(tmp_path, cached):
    async def run():
        machine, transport = _machine(tmp_path)
        sid = "stack@t"
        machine._session_pins.set_pinned("codex", sid, True)
        if cached:
            machine._codex_session_list_cache = (time.monotonic(), [{
                "session_id": sid, "native_pinned": True,
            }], ())
        machine._set_native_codex_pin = AsyncMock(side_effect=CodexRpcOutcomeUnknown())
        await machine._handle_pin_session(PinSession(session_id=sid, engine="codex", pinned=False))
        assert sid in machine._session_pins.ids("codex")
        machine._set_native_codex_pin.assert_awaited_once_with(sid, False)
        assert any(event.type == "error" for event in transport.sent)
    asyncio.run(run())


def test_stale_cold_native_ack_returns_new_receipt_without_writing(tmp_path):
    async def run():
        machine, _ = _machine(tmp_path)
        machine._session_presentation.sync_codex_read_states({"stack@t": ("codex-read-new", True)})
        machine._codex_rpc_for_wire = AsyncMock(side_effect=AssertionError("no native mutation"))
        result = await machine._handle_acknowledge_completion(AcknowledgeCompletion(
            sid="stack@t", completion_id="codex-read-old"))
        assert result.type == "completion_state"
        assert result.completion_id == "codex-read-new" and result.unread
        machine._codex_rpc_for_wire.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("resident", [False, True])
@pytest.mark.parametrize("first_unread", ["live-turn", "earlier-turn"])
def test_live_completion_ack_updates_native_state_before_catalog_refresh(tmp_path, resident, first_unread):
    async def run():
        machine, transport = _machine(tmp_path)
        sid = "stack@thread"
        ctx = _context(sid, "thread", "stack")
        machine.sessions[sid] = ctx
        state = {"revision": "live-revision", "firstUnread": {"type": "turn", "turnId": first_unread}}
        calls = []

        async def rpc(profile, method, params, **kwargs):
            nonlocal state
            calls.append((profile.id, method, params))
            assert profile.id == "stack" and params["threadId"] == "thread"
            if method == "thread/read":
                return {"thread": {"id": "thread"}, "readState": state}
            if method == "thread/turns/list":
                assert params["limit"] == 1 and params["itemsView"] == "summary"
                return {"data": [{"id": "live-turn", "status": "completed"}]}
            assert method == "thread/readState/update"
            assert params["expectedRevision"] == "live-revision"
            assert params["operation"] == {"type": "read"}
            state = {"revision": "read-revision", "firstUnread": None}
            return {"readState": state}

        machine._codex_rpc_for_profile = rpc
        await machine._emit(ctx, _success("live-turn"))
        assert transport.sent[-1].completion_id == "live-turn"
        if not resident:
            machine.sessions.clear()
        event = await machine._handle_acknowledge_completion(AcknowledgeCompletion(
            sid=sid, completion_id="live-turn"))
        assert event.type == "completion_state" and not event.unread
        assert [method for _, method, _ in calls] == [
            "thread/read", "thread/turns/list", "thread/readState/update"]
        machine._session_presentation.sync_codex_read_states({
            sid: (read_receipt_id(state), state["firstUnread"] is not None)})
        assert not machine._session_presentation.get("codex", sid).completion_unread
    asyncio.run(run())


@pytest.mark.parametrize("race", [
    "newer-turn", "manual-unread", "in-progress", "newer-local",
    "newer-during-write", "conflict", "timeout",
])
def test_live_read_ack_cannot_clear_unseen_activity(tmp_path, race):
    async def run():
        machine, _ = _machine(tmp_path)
        sid = "stack@thread"
        machine._session_presentation.mark_completion("codex", sid, "seen-turn")
        calls = []

        async def rpc(_profile, method, params, **kwargs):
            calls.append(method)
            if method == "thread/read":
                # The earliest unread turn alone cannot prove which result is newest.
                position = {"type": "threadStart"} if race == "manual-unread" else {
                    "type": "turn", "turnId": "seen-turn"}
                return {"thread": {"id": "thread"}, "readState": {
                    "revision": "observed", "firstUnread": position}}
            if method == "thread/turns/list":
                if race == "newer-local":
                    machine._session_presentation.mark_completion("codex", sid, "new-turn")
                return {"data": [{"id": "new-turn" if race == "newer-turn" else "seen-turn",
                                  "status": "inProgress" if race == "in-progress" else "completed"}]}
            assert method == "thread/readState/update" and params["expectedRevision"] == "observed"
            if race == "newer-during-write":
                machine._session_presentation.mark_completion("codex", sid, "new-turn")
                return {"readState": {"revision": "read", "firstUnread": None}}
            if race == "conflict":
                raise CodexRpcRejected("revision changed", code=-32600)
            if race == "timeout":
                raise CodexRpcOutcomeUnknown()
            raise AssertionError("unseen activity must not be acknowledged")

        machine._codex_rpc_for_profile = rpc
        await machine._handle_acknowledge_completion(AcknowledgeCompletion(
            sid=sid, completion_id="seen-turn"))
        assert machine._session_presentation.get("codex", sid).completion_unread
        assert calls.count("thread/readState/update") == int(race in {"newer-during-write", "conflict", "timeout"})
    asyncio.run(run())


@pytest.mark.parametrize("resident", [False, True])
def test_work_completion_stays_local_without_reading_code_metadata(tmp_path, resident):
    async def run():
        machine, _ = _machine(tmp_path)
        record = machine._work.for_engine("codex").create_session(codex_profile_id="stack")
        machine._work.for_engine("codex").bind_session(record.work_id, "thread", codex_profile_id="stack")
        sid = "stack@thread"
        if resident:
            ctx = _context(sid, "thread", "stack")
            ctx.space = "work"
            ctx.work_id = record.work_id
            machine.sessions[sid] = ctx
        machine._session_presentation.mark_completion("codex", sid, "work-turn")
        machine._codex_rpc_for_wire = AsyncMock(side_effect=AssertionError("Code metadata must not be read"))
        event = await machine._handle_acknowledge_completion(AcknowledgeCompletion(
            sid=sid, completion_id="work-turn"))
        assert event.type == "completion_state" and not event.unread
        machine._codex_rpc_for_wire.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("metadata", ["missing", "unsupported", "unavailable", "malformed"])
def test_live_read_local_fallback_requires_legacy_contract(tmp_path, metadata):
    async def run():
        machine, _ = _machine(tmp_path)
        sid = "stack@thread"
        machine._session_presentation.mark_completion("codex", sid, "turn")
        response = {"thread": {"id": "thread"}}
        if metadata in {"unavailable", "malformed"}:
            response["readState"] = None if metadata == "unavailable" else {"revision": 42}
        machine._codex_rpc_for_wire = AsyncMock(return_value=response)
        if metadata == "unsupported":
            machine._codex_rpc_for_wire.side_effect = CodexRpcRejected("unsupported method", code=-32601)
        event = await machine._handle_acknowledge_completion(AcknowledgeCompletion(sid=sid, completion_id="turn"))
        if metadata in {"missing", "unsupported"}:
            assert event.type == "completion_state" and not event.unread
        else:
            assert event.type == "error" and machine._session_presentation.get("codex", sid).completion_unread
        assert machine._codex_rpc_for_wire.await_count == 1
    asyncio.run(run())


@pytest.mark.parametrize("catalog", [None, False, True])
def test_legacy_and_native_unpin_uses_authoritative_state(tmp_path, catalog):
    async def run():
        machine, _ = _machine(tmp_path)
        sid = "stack@thread"
        machine._session_pins.set_pinned("codex", sid, True)
        if catalog is not None:
            machine._codex_session_list_cache = (time.monotonic(), [{
                "session_id": sid, "native_pinned": catalog}], ())
        machine._list_codex_sessions = AsyncMock()
        calls = []

        async def rpc(profile, method, params, **kwargs):
            calls.append(method)
            assert profile.id == "stack" and params["threadId"] == "thread"
            if method == "thread/read":
                return {"thread": {"id": "thread", "section": {"id": PINNED_SECTION_ID}}}
            assert method == "thread/section/move" and params["sectionId"] is None
            return {}

        machine._codex_rpc_for_profile = rpc
        await machine._handle_pin_session(PinSession(session_id=sid, engine="codex", pinned=False))
        assert calls == ["thread/read", "thread/section/move"]
        assert sid not in machine._session_pins.ids("codex")
    asyncio.run(run())


@pytest.mark.parametrize("local_state", ["read", "unread", "none"])
@pytest.mark.parametrize("native_unread", [False, True])
def test_catalog_refresh_preserves_work_receipts_and_syncs_code(tmp_path, local_state, native_unread):
    async def run():
        machine, _ = _machine(tmp_path)
        store = machine._work.for_engine("codex")
        record = store.create_session(codex_profile_id="stack")
        store.bind_session(record.work_id, "shared-id", codex_profile_id="stack")
        work_sid = "stack@shared-id"
        if local_state != "none":
            machine._session_presentation.mark_completion("codex", work_sid, "work-result")
        machine._codex_rpc_for_wire = AsyncMock(side_effect=AssertionError("Work ack must remain local"))
        if local_state == "read":
            await machine._handle_acknowledge_completion(AcknowledgeCompletion(
                sid=work_sid, completion_id="work-result"))
        before = machine._session_presentation.get("codex", work_sid)
        native_state = {"revision": "native", "firstUnread": (
            {"type": "turn", "turnId": "native-result"} if native_unread else None)}
        rows = [{
            "session_id": f"{profile}@shared-id", "native_session_id": "shared-id",
            "codex_profile_id": profile, "status": "idle",
            "cwd": record.cwd if profile == "stack" else str(tmp_path),
            "native_read_state": native_state,
        } for profile in ("primary", "stack")]
        machine._read_codex_profile_catalog = AsyncMock(return_value=(rows, ()))
        for _ in range(2):
            refreshed = await machine._refresh_codex_session_catalog()
            assert len(refreshed) == 2  # Both spaces still need their catalog rows.
            assert machine._session_presentation.get("codex", work_sid) == before
            code = machine._session_presentation.get("codex", "primary@shared-id")
            assert code.completion_id == read_receipt_id(native_state)
            assert code.completion_unread is native_unread
            listing = await machine._send_codex_session_list(
                ListSessions(engine="codex", space="work"), refreshed)
            assert len(listing.sessions) == 1
            assert listing.sessions[0].completion_id == before.completion_id
            assert listing.sessions[0].completion_unread == (
                None if local_state == "none" else before.completion_unread)
        machine._codex_rpc_for_wire.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("profile_id", [None, "stack"])
def test_pending_work_binding_is_not_imported_as_code_read_state(tmp_path, profile_id):
    async def run():
        machine, _ = _machine(tmp_path)
        store = machine._work.for_engine("codex")
        record = store.create_session(codex_profile_id=profile_id)
        owner = profile_id or "primary"
        state = {"revision": "unread", "firstUnread": {"type": "threadStart"}}
        # A crash may leave the native thread durable before its Work binding.
        # A sibling account using the same path must remain independent.
        rows = [{
            "session_id": f"{profile}@shared-id", "native_session_id": "shared-id",
            "codex_profile_id": profile, "status": "idle", "cwd": record.cwd,
            "native_read_state": state,
        } for profile in ("primary", "stack")]
        machine._read_codex_profile_catalog = AsyncMock(return_value=(rows, ()))
        refreshed = await machine._refresh_codex_session_catalog()
        assert machine._session_presentation.get("codex", f"{owner}@shared-id").completion_id is None
        other = "stack" if owner == "primary" else "primary"
        assert machine._session_presentation.get("codex", f"{other}@shared-id").completion_id == read_receipt_id(state)
        await machine._send_codex_session_list(ListSessions(engine="codex", space="work"), refreshed)
        assert store.get_by_session("shared-id", codex_profile_id=owner) is not None
        await machine._refresh_codex_session_catalog()
        assert machine._session_presentation.get("codex", f"{owner}@shared-id").completion_id is None
    asyncio.run(run())
