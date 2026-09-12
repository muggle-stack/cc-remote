from types import SimpleNamespace
import asyncio
import json

from claude_agent_sdk.types import SystemMessage

from cc_remote.wrapper.claude_model_fallback import model_fallback_event
from cc_remote.wrapper.history_store import materialize_history_turns
from cc_remote.wrapper.stream import (
    StreamTranslator, extract_model, recover_claude_native_metadata,
    translate_history,
)


def fallback(**changes):
    return dict({
        "type": "system", "subtype": "model_refusal_fallback",
        "uuid": "00000000-0000-4000-8000-000000000002",
        "originalModel": "claude-fable-5-1[1m]",
        "fallbackModel": "claude-opus-4-8[1m]",
        "scope": "session", "trigger": "refusal",
        "timestamp": "2026-09-08T01:00:01Z",
        "content": "untrusted provider content SECRET",
    }, **changes)


def test_official_fallback_is_bounded_stable_and_changes_only_session_model():
    data = fallback()
    msg = SystemMessage(subtype=data["subtype"], data=data)
    event, = StreamTranslator(1000, turn_id="turn").feed(msg)
    assert event.tool == "model_refusal_fallback"
    assert "SECRET" not in event.model_dump_json()
    assert extract_model(msg) == data["fallbackModel"]
    assert model_fallback_event(fallback(timestamp=None)).item_id == event.item_id
    transient = fallback(scope="turn")
    assert extract_model(SystemMessage(subtype=transient["subtype"], data=transient)) is None
    assert model_fallback_event(fallback(fallbackModel="https://provider/SECRET")) is None
    assert model_fallback_event(fallback(subtype="init")) is None
    assert model_fallback_event(fallback(parent_tool_use_id="subagent")) is None
    assert model_fallback_event(fallback(parentToolUseID="subagent")) is None
    assert model_fallback_event(fallback(isSidechain=True)) is None


def test_live_fallback_updates_effective_model_persists_and_notifies_once():
    from cc_remote.wrapper.sdk import SdkHandle
    from tests.test_multisession import _mk_ctx, _mk_machine

    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("fallback-session", "fallback-session")
        ctx.active_msg_id = "turn"
        sdk = ctx.sdk = SdkHandle(machine.cfg)
        sdk.model = "claude-fable-5-1[1m]"
        persisted = []
        async def persist(target):
            persisted.append(target.sdk.model)
        machine._persist_claude_session_controls = persist
        message = SystemMessage(subtype="model_refusal_fallback", data=fallback())
        sdk._observe_model_fallback(message)
        assert sdk.model == "claude-opus-4-8[1m]"
        await machine._observe_claude_model_fallback(ctx, message)
        await machine._observe_claude_model_fallback(ctx, message)
        assert len([event for event in transport.sent if event.type == "notice"]) == 1
        assert len([event for event in transport.sent if event.type == "model"]) == 1
        assert persisted[-1] == sdk.model
        sdk.model = "claude-fable-5-1[1m]"  # a newer explicit selection won
        await machine._observe_claude_model_fallback(ctx, message)
        assert persisted[-1] == "claude-opus-4-8[1m]"  # not overwritten by the late callback
        transient = SystemMessage(subtype="model_refusal_fallback", data=fallback(scope="turn"))
        sdk._observe_model_fallback(transient)
        assert sdk.model == "claude-fable-5-1[1m]"
    asyncio.run(run())


def test_history_restores_fallback_without_sidechain_or_prompt_injection(tmp_path):
    human = {
        "type": "user", "uuid": "00000000-0000-4000-8000-000000000001",
        "parentUuid": None, "timestamp": "2026-09-08T01:00:00Z",
        "message": {"role": "user", "content": "hello"},
    }
    notice = fallback(parentUuid=human["uuid"])
    answer = {
        "type": "assistant", "uuid": "00000000-0000-4000-8000-000000000003",
        "parentUuid": notice["uuid"], "timestamp": "2026-09-08T01:00:02Z",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn"},
    }
    side = fallback(uuid="side", parentUuid=human["uuid"], isSidechain=True)
    later_human = dict(human, uuid="later-user", parentUuid=answer["uuid"])
    later_notice = fallback(uuid="later-notice", parentUuid=later_human["uuid"])
    later_answer = dict(answer, uuid="later-answer", parentUuid=later_notice["uuid"])
    path = tmp_path / "session.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in (
        human, notice, answer, side, later_human, later_notice, later_answer)))
    messages = [SimpleNamespace(type=row["type"], uuid=row["uuid"], message=row["message"])
                for row in (human, answer)]
    timestamps = {}
    internal = {}
    restored = recover_claude_native_metadata(
        "session", messages, path=str(path), timestamps=timestamps, internal_events=internal)
    assert [row.uuid for row in restored] == [human["uuid"], notice["uuid"], answer["uuid"]]
    assert list(internal) == [notice["uuid"]]
    events = translate_history(restored, 1000, timestamps=timestamps, internal_user_events=internal)
    assert len([event for event in events if event.type == "user_msg"]) == 1
    summary, = materialize_history_turns([event.model_dump() for event in events])
    notices = [block for block in summary["blocks"] if block.get("tool") == "model_refusal_fallback"]
    assert len(notices) == 1
    assert summary["processDetailState"] == "none"
    assert summary["detailEventCount"] == 0
    assert "SECRET" not in json.dumps(summary)
    # A page starting after the switch must not inject an earlier notice.
    late = recover_claude_native_metadata(
        "session", messages[1:], path=str(path), timestamps={}, internal_events={})
    assert [row.uuid for row in late] == [answer["uuid"]]
