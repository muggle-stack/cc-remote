"""Consumed Claude guidance shares its physical response's real terminal."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock, UserMessage

from cc_remote.protocol import History, UserMsg
from cc_remote.wrapper import claude_steer
from cc_remote.wrapper.collaboration import Collaboration
from cc_remote.wrapper.stream import StreamTranslator, translate_history
from tests.test_collaboration import native_named_member, send, store_named_member
from tests.test_multisession import _mk_ctx


async def native_response(*, stale=False):
    machine, _transport, controller, sid, native, _title = native_named_member("claude")
    store, room_id, member = await store_named_member(controller, "claude", sid, native)
    ctx = _mk_ctx(sid, native)
    ctx.cwd = "/tmp"
    ctx.state = "running"
    machine.sessions[sid] = ctx
    controller.watch(ctx, member["id"])
    unrelated = send(store, room_id, message_id="unrelated", request="unrelated") if stale else None
    if unrelated:
        store.consume(unrelated["id"], "unrelated-native-turn")
    first = send(store, room_id)
    native_user = "33333333-3333-4333-8333-333333333333"
    ctx.active_msg_id = first["id"]
    ctx.translator = StreamTranslator(10000, turn_id=first["id"])
    ctx.translator.feed(UserMessage(content="first", uuid=native_user))
    await machine._emit(ctx, UserMsg(msg_id=native_user, client_msg_id=first["id"], prompt="first"))
    return SimpleNamespace(machine=machine, controller=controller, store=store, room_id=room_id,
                           member=member, ctx=ctx, first=first, unrelated=unrelated,
                           users=[(native_user, first["id"], "first")], guides=[])


async def consume_guidance(response, index, *, room_input=True):
    delivery = None
    if room_input:
        delivery = send(response.store, response.room_id, message_id=f"guide-{index}", request=f"guide-{index}")
        assert response.store.claim(delivery["id"])
        response.store.submit(delivery["id"], "steer")
    logical = delivery["id"] if delivery else f"native-ui-{index}"
    native = f"44444444-4444-4444-8444-{index:012d}"
    if delivery:
        response.store.expect_input(delivery["id"], native)
    message = UserMessage(content=f"guide {index}", uuid=native)
    message._cc_steer = {"id": logical, "prompt": f"guide {index}"}
    frame = await claude_steer.apply_echo(response.machine, response.ctx, message, native)
    await response.machine._emit(response.ctx, frame)
    for frame in response.ctx.translator.feed(message):
        await response.machine._emit(response.ctx, frame)
    response.users.append((native, logical, f"guide {index}"))
    if delivery:
        response.guides.append(delivery)


async def finish_response(response, subtype="success"):
    assistant = AssistantMessage(content=[TextBlock(text="combined answer")], model="claude-test",
                                 uuid="55555555-5555-4555-8555-555555555555", stop_reason="end_turn")
    result = ResultMessage(subtype=subtype, duration_ms=1, duration_api_ms=1, num_turns=1,
                           is_error=subtype != "success", session_id=response.ctx.session_id)
    for message in (assistant, result):
        for frame in response.ctx.translator.feed(message):
            await response.machine._emit(response.ctx, frame)


def response_history(response):
    messages = [SimpleNamespace(type="user", uuid=native, is_queued_prompt=index > 0,
                                message={"role": "user", "content": prompt})
                for index, (native, _logical, prompt) in enumerate(response.users)]
    messages.append(SimpleNamespace(type="assistant", uuid="55555555-5555-4555-8555-555555555555",
                                   parent_tool_use_id=None, message={"role": "assistant", "content": [
                                       {"type": "text", "text": "combined answer"}]}))
    return translate_history(messages, 10000,
                             client_message_ids={native: logical for native, logical, _prompt in response.users})


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 3])
@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("subtype,expected", [("success", "succeeded"), ("error", "failed"), ("interrupted", "cancelled")])
async def test_claude_real_result_settles_only_its_consumed_guidance(count, stale, subtype, expected):
    response = await native_response(stale=stale)
    for index in range(count):
        await consume_guidance(response, index)
    await finish_response(response, subtype)
    for delivery in [response.first, *response.guides]:
        assert response.store.delivery(delivery["id"])["status"] == expected
    if response.unrelated:
        assert response.store.delivery(response.unrelated["id"])["status"] == "running"
    assert len(response.store.deliveries(response.room_id, active=True)) == int(stale)
    replies = [row for row in response.store.messages("owner", response.room_id) if row["role"] == "assistant"]
    assert len(replies) == 1
    assert set(replies[0]["reply_to"]) == {"message", *(f"guide-{index}" for index in range(count))}


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 3])
async def test_native_page_guidance_also_settles_the_prior_room_delivery(count):
    response = await native_response()
    for index in range(count):
        await consume_guidance(response, index, room_input=False)
    await finish_response(response)
    assert response.store.delivery(response.first["id"])["status"] == "succeeded"
    replies = [row for row in response.store.messages("owner", response.room_id) if row["role"] == "assistant"]
    assert [row["text"] for row in replies] == ["combined answer"]
    assert replies[0]["reply_to"] == ["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("paged", [False, True])
async def test_claude_guidance_history_repairs_predecessors_after_restart(monkeypatch, legacy, paged):
    response = await native_response(stale=True)
    await consume_guidance(response, 0)
    await consume_guidance(response, 1)
    await finish_response(response)
    # Reproduce the persisted state left by older versions after the final
    # guidance succeeds while its original input remains active.
    with response.store.db() as db:
        db.execute("UPDATE deliveries SET status='running' WHERE id=?", (response.first["id"],))
        if legacy:
            columns = {row[1] for row in db.execute("PRAGMA table_info(deliveries)")}
            for name in ("response_owner", "response_msg_id"):
                if name in columns:
                    db.execute(f"UPDATE deliveries SET {name}=NULL")
    controller = Collaboration(response.machine)
    response.machine._collaboration = controller
    store = await controller.database()
    monkeypatch.setattr(controller, "schedule_reconcile", lambda _member: None)
    await controller.restore_observers()
    events = [frame.model_dump() for frame in response_history(response)]
    boundary = next(index for index, frame in enumerate(events) if frame["type"] == "turn_end" and frame["result"]["subtype"] == "steered") + 1

    async def read_page(command, _sid):
        older = command.before == "older"
        frames = events[:boundary] if older else events[boundary:] if paged else events
        return History(session_id=response.ctx.key, revision="full", live_seq=response.ctx.seq,
                       events=frames, before=command.before, has_more=paged and not older,
                       oldest_id="older" if paged and not older else None)

    response.machine._history_page_singleflight = AsyncMock(side_effect=read_page)
    response.machine._handle_query = AsyncMock()
    response.machine._spawn = AsyncMock()
    await controller.reconcile(store.member_by_id(response.member["id"]))
    assert store.delivery(response.first["id"])["status"] == "succeeded"
    assert store.delivery(response.unrelated["id"])["status"] == "running"
    replies = [row for row in store.messages("owner", response.room_id) if row["role"] == "assistant"]
    assert len(replies) == 1
    assert set(replies[0]["reply_to"]) == {"message", "guide-0", "guide-1"}
    response.machine._handle_query.assert_not_awaited()
    response.machine._spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_reply_reconciliation_merges_consumed_input_references():
    response = await native_response()
    response.store.publish(response.member["id"], "reply", "combined answer", ["message"])
    response.store.publish(response.member["id"], "reply", "combined answer", ["guide-0"])
    response.store.publish(response.member["id"], "reply", "combined answer", ["message"])
    reply = response.store.messages("owner", response.room_id)[-1]
    assert reply["reply_to"] == ["message", "guide-0"]


@pytest.mark.asyncio
async def test_claude_terminal_keeps_unconsumed_guidance_pending():
    response = await native_response()
    await consume_guidance(response, 0)
    pending = send(response.store, response.room_id, message_id="not-consumed", request="not-consumed")
    assert response.store.claim(pending["id"])
    response.store.submit(pending["id"], "steer")
    response.store.set_delivery(pending["id"], "accepted")
    await finish_response(response)
    assert response.store.delivery(response.first["id"])["status"] == "succeeded"
    assert response.store.delivery(response.guides[0]["id"])["status"] == "succeeded"
    assert response.store.delivery(pending["id"])["status"] == "accepted"
    reply = response.store.messages("owner", response.room_id)[-1]
    assert set(reply["reply_to"]) == {"message", "guide-0"}


@pytest.mark.asyncio
async def test_claude_response_link_and_consumption_roll_back_together(monkeypatch):
    import sqlite3

    response = await native_response()
    original = response.store._steer_response
    failures = 1

    def fail_after_link(*args, **kwargs):
        nonlocal failures
        original(*args, **kwargs)
        if failures:
            failures -= 1
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(response.store, "_steer_response", fail_after_link)
    await consume_guidance(response, 0)
    assert response.store.delivery(response.first["id"])["response_owner"] == response.users[0][0]
    assert response.store.delivery(response.guides[0]["id"])["status"] == "submitting"
    await finish_response(response)
    assert not [row for row in response.store.messages("owner", response.room_id) if row["role"] == "assistant"]
    events = [frame.model_dump() for frame in response_history(response)]
    response.machine._history_page_singleflight = AsyncMock(return_value=History(
        session_id=response.ctx.key, revision="full", live_seq=response.ctx.seq, events=events,
    ))
    response.machine._handle_query = AsyncMock()
    response.machine._spawn = AsyncMock()
    await response.controller.reconcile(response.store.member_by_id(response.member["id"]))
    assert not response.store.deliveries(response.room_id, active=True)
    reply = response.store.messages("owner", response.room_id)[-1]
    assert reply["reply_to"] == ["message", "guide-0"]
    response.machine._handle_query.assert_not_awaited()
    response.machine._spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_delivery_schema_adds_private_response_ownership():
    from cc_remote.collaboration_store import CollaborationStore

    response = await native_response()
    with response.store.db() as db:
        for name in ("response_owner", "response_msg_id"):
            db.execute(f"ALTER TABLE deliveries DROP COLUMN {name}")
    response.store = CollaborationStore(response.machine.cfg.state_dir)
    response.controller.store = response.store
    await consume_guidance(response, 0)
    await finish_response(response)
    assert not response.store.deliveries(response.room_id, active=True)
    assert all(not {"response_owner", "response_msg_id"} & row.keys()
               for row in response.store.public_deliveries(response.room_id))
