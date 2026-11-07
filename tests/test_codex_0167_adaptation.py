"""0.167 control-plane contracts: no model calls or real account writes."""
import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from cc_remote.protocol import AcknowledgeCompletion, PinSession
from cc_remote.wrapper.codex_approvals import approval_choices, approval_prompt
from cc_remote.wrapper.codex_handle import CodexAppServerError, CodexHandle
from cc_remote.wrapper.codex_organization import PINNED_SECTION_ID, read_receipt_id
from cc_remote.wrapper.codex_rpc import CodexRpcOutcomeUnknown, CodexRpcRejected, _rpc_error
from cc_remote.wrapper.codex_sessions import _normalize_thread
from cc_remote.wrapper.session_presentation import SessionPresentationStore
from tests.test_codex_controls import _Cfg, _dispatch_request
from tests.test_codex_profiles import _machine


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


def test_native_pin_outage_cannot_report_successful_unpin(tmp_path):
    async def run():
        machine, transport = _machine(tmp_path)
        sid = "stack@t"
        machine._session_pins.set_pinned("codex", sid, True)
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
