"""Native Codex App cross-thread inputs: live, official history and rollout."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from cc_remote.protocol import UserMsg, TurnBinding, ConversationTurn, serialize, deserialize
from cc_remote.wrapper.codex_delegation import parse_codex_delegation, codex_message_target
from cc_remote.wrapper.codex_external import visible_codex_user_message
from cc_remote.wrapper.codex_handle import CodexHandle
from cc_remote.wrapper.codex_history import CodexOfficialHistory
from cc_remote.wrapper.codex_stream import (
    codex_live_user_message, codex_translate_history, codex_history_window_info,
)
from cc_remote.wrapper.history_store import materialize_history_turns
from tests.test_codex_history import _agent, _turn
from tests.test_codex_spontaneous_stream import _notification
from tests.test_multisession import _mk_ctx, _mk_machine

SOURCE = "01a01234-1234-7890-abcd-123456789abc"
ENVELOPE = (f"<codex_delegation>\n<source_thread_id>{SOURCE}</source_thread_id>\n"
            "<input>Check &lt;code&gt; &amp; &amp;lt;literal&amp;gt;</input>\n</codex_delegation>")
PROMPT = "Check <code> & &lt;literal&gt;"


def _input(kind, item_id="incoming"):
    if kind == "legacy":
        return {"type": "userMessage", "id": item_id,
                "content": [{"type": "text", "text": ENVELOPE}]}
    return {"type": "functionCallOutput", "id": item_id, "namespace": "codex_app",
            "name": "send_message_to_thread", "output": ENVELOPE}


@pytest.mark.parametrize("kind", ["legacy", "native"])
def test_same_native_identity_and_provenance_in_live_history_and_rollout(tmp_path, kind):
    item = _input(kind)
    user = codex_live_user_message(_notification("item/completed", "task", item=item))
    assert (user.message_id, user.prompt, user.source_thread_id) == ("incoming", PROMPT, SOURCE)

    async def run():
        async def rpc(method, params, *_):
            assert method == "thread/turns/list"
            return {"data": [_turn("task", [item, _agent("answer", "Checked.")])], "nextCursor": None}
        page = await CodexOfficialHistory(65536, rpc=rpc).summary_page(
            "receiver", before=None, limit=4)
        assert len(page.turns) == 1
        assert page.turns[0]["id"] == user.message_id
        assert page.turns[0]["sourceThreadId"] == SOURCE
        assert page.turns[0]["prompt"] == PROMPT
        ConversationTurn.model_validate(page.turns[0])
    asyncio.run(run())

    rollout_item = {**item, "type": "UserMessage" if kind == "legacy" else "FunctionCallOutput"}
    rows = [
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "task"}},
        {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "task", "item": rollout_item}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "Checked."}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "task"}},
    ]
    path = tmp_path / "rollout.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    events, _ = codex_translate_history(str(path), 65536)
    users = [e for e in events if isinstance(e, UserMsg)]
    assert [(u.msg_id, u.prompt, u.source_thread_id) for u in users] == [("incoming", PROMPT, SOURCE)]
    turn = materialize_history_turns([e.model_dump(mode="json") for e in events])[0]
    assert turn["sourceThreadId"] == SOURCE
    assert deserialize(serialize(users[0])).source_thread_id == SOURCE
    window = codex_history_window_info(str(path), before=None, limit=1)
    assert window is not None


@pytest.mark.parametrize("kind", ["legacy", "native", "native-partial"])
def test_spontaneous_delivery_publishes_one_native_user_with_no_placeholder(kind):
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("thread-spontaneous", "thread-spontaneous")
        ctx.engine = "codex"
        handle = CodexHandle(machine.cfg)
        handle.thread_id = ctx.session_id
        handle.proc = SimpleNamespace(returncode=None)
        ctx.sdk = handle
        machine.sessions[ctx.key] = ctx
        handle.turn_lifecycle_callback = lambda phase, turn_id: machine._on_codex_turn_lifecycle(ctx, phase, turn_id)
        item = _input(kind)
        for message in [
            _notification("turn/started", "task", turn={"id": "task"}),
            _notification("item/started", "task", item={**item, "output": ""} if kind == "native-partial" else item),
            _notification("item/completed", "task", item=item),
            _notification("item/completed", "task", item=_agent("answer", "Checked.")),
            _notification("turn/completed", "task", turn={"id": "task", "status": "completed"}),
        ]:
            await handle._dispatch(message)
        await asyncio.wait_for(ctx.codex_spontaneous_task, 1)
        users = [e for e in transport.sent if isinstance(e, UserMsg)]
        assert [(u.msg_id, u.prompt, u.source_thread_id) for u in users] == [("incoming", PROMPT, SOURCE)]
        assert [(e.msg_id, e.turn_id) for e in transport.sent if isinstance(e, TurnBinding)] == [("incoming", "task")]
    asyncio.run(run())


def test_unknown_markup_and_foreign_tool_outputs_remain_unattributed():
    for text in [ENVELOPE + "extra", ENVELOPE.replace(SOURCE, "other@thread"),
                 ENVELOPE.replace("&lt;code&gt;", "<code>"), "<input>literal</input>"]:
        assert parse_codex_delegation(text) is None
        assert visible_codex_user_message(text) == text
    for override in [{"namespace": "foreign"}, {"name": "read_thread"}, {"name": []}, {"id": "../bad"}]:
        assert codex_live_user_message(_notification("item/completed", "task", item={**_input("native"), **override})) is None
    assert parse_codex_delegation(
        f"<codex_delegation><source_thread_id>{SOURCE}</source_thread_id><input>"
        + " " * (512 * 1024)) is None


def test_sender_receipt_requires_native_tool_and_retains_failure():
    assert codex_message_target("send_message_to_thread", {"threadId": SOURCE}, "foreign") is None
    events = [
        {"type": "user_msg", "msg_id": "user", "prompt": "review"},
        {"type": "tool_use", "message_id": "assistant", "tool_use_id": "send", "tool": "send_message_to_thread",
         "server": "codex_app", "input": {"threadId": SOURCE, "input": "private tool body"}},
        {"type": "tool_result", "tool_use_id": "send", "content": "denied", "is_error": True},
    ]
    turn = materialize_history_turns(events)[0]
    assert turn["sessionMessages"] == [{"itemId": "send", "threadId": SOURCE, "status": "failed"}]
    assert "private tool body" not in json.dumps(turn)


@pytest.mark.parametrize("kind", ["mcpToolCall", "dynamicToolCall"])
@pytest.mark.parametrize("success", [True, False])
def test_official_sender_tool_outcome_becomes_summary_receipt(kind, success):
    tool = {
        "id": "send-call", "type": kind, "tool": "send_message_to_thread",
        "arguments": {"threadId": SOURCE, "input": "Check the code"},
        "status": "completed" if success else "failed",
    }
    if kind == "mcpToolCall":
        tool.update(server="codex_app", result={"content": []} if success else None,
                    error=None if success else {"message": "Target unavailable"})
    else:
        tool.update(namespace="codex_app", success=success, contentItems=[])

    async def run():
        async def rpc(method, params, *_):
            return {"data": [_turn("task", [
                _input("legacy"), tool, _agent("answer", "Finished."),
            ])], "nextCursor": None}
        page = await CodexOfficialHistory(65536, rpc=rpc).summary_page(
            "receiver", before=None, limit=4)
        assert page.turns[0]["sessionMessages"] == [{
            "itemId": "send-call", "threadId": SOURCE,
            "status": "sent" if success else "failed",
        }]
    asyncio.run(run())


def test_native_steers_keep_separate_item_identities_and_sources():
    async def run():
        async def rpc(method, params, *_):
            return {"data": [_turn("task", [
                _input("legacy", "first"), _agent("comment", "Working.", phase="commentary"),
                _input("native", "second"), _agent("answer", "Done."),
            ])], "nextCursor": None}
        page = await CodexOfficialHistory(65536, rpc=rpc).summary_page("receiver", before=None, limit=4)
        assert [turn["id"] for turn in page.turns] == ["first", "second"]
        assert [turn["prompt"] for turn in page.turns] == [PROMPT, PROMPT]
        assert [turn["sourceThreadId"] for turn in page.turns] == [SOURCE, SOURCE]
    asyncio.run(run())
