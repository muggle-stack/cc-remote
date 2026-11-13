"""Native question ownership and room history share one answer lifecycle."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cc_remote.collaboration import CollaborationMemberSpec, shared_prompt
from cc_remote.collaboration_store import CollaborationStore
from cc_remote.protocol import AnswerQuestion, CollaborationCommand, UserMsg
from tests.test_collaboration import native_named_member, store_named_member
from tests.test_multisession import _mk_ctx


async def question_fixture(engine):
    machine, transport, controller, sid, native, _title = native_named_member(engine)
    store, room_id, member = await store_named_member(controller, engine, sid, native)
    ctx = _mk_ctx(sid, native)
    ctx.engine = engine
    ctx.space = "code"
    ctx.cwd = "/tmp"
    ctx.state = "running"
    ctx.sdk = SimpleNamespace(native_id=native)
    machine.sessions[sid] = ctx
    controller.watch(ctx, member["id"])
    return machine, transport, controller, store, room_id, member, ctx


async def native_question(machine, ctx):
    if ctx.engine == "claude":
        return await machine._on_claude_ask_user_question(
            ctx,
            {
                "questions": [
                    {
                        "question": "选择哪个目录？",
                        "header": "工作目录",
                        "options": [{"label": "项目目录"}, {"label": "临时目录"}],
                    }
                ]
            },
        )
    if ctx.engine == "codex":
        return await machine._on_codex_interaction(
            ctx,
            "item/tool/requestUserInput",
            {
                "questions": [
                    {
                        "id": "directory",
                        "question": "选择哪个目录？",
                        "options": [{"label": "项目目录"}, {"label": "临时目录"}],
                    }
                ],
            },
        )
    return await machine._dsh.question_outcome(
        ctx,
        {
            "event": "user-questions/request",
            "request": {
                "questions": [
                    {
                        "id": "directory",
                        "question": "选择哪个目录？",
                        "options": [{"label": "项目目录"}, {"label": "临时目录"}],
                    }
                ]
            },
        },
        {"signature": "native-question", "answers": []},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["claude", "codex", "dsh"])
@pytest.mark.parametrize("answer_surface", ["collaboration", "native"])
async def test_native_question_and_answer_are_shared_and_close_on_both_surfaces(
    engine, answer_surface
):
    (
        machine,
        transport,
        controller,
        store,
        room_id,
        member,
        ctx,
    ) = await question_fixture(engine)
    task = asyncio.create_task(native_question(machine, ctx))
    try:
        async with asyncio.timeout(2):
            while not ctx.pending_asks:
                await asyncio.sleep(0)
        pending = next(iter(ctx.pending_asks.values())).event
        snapshot = await controller.snapshot("owner", room_id)
        assert snapshot["questions"][0]["question"]["ask_id"] == pending.ask_id
        messages = store.messages("owner", room_id)
        assert len(messages) == 1
        assert messages[0]["question"]["status"] == "pending"
        assert "选择哪个目录？" in messages[0]["text"]
        assert "项目目录" in messages[0]["text"]
        values = (
            {"answers": ["项目目录"]} if pending.questions else {"answer": "项目目录"}
        )
        if answer_surface == "collaboration":
            result = await controller.handle(
                CollaborationCommand(
                    operation="answer",
                    owner_id="owner",
                    client_id="browser",
                    cmd_id="answer",
                    room_id=room_id,
                    member_id=member["id"],
                    ask_id=pending.ask_id,
                    **values,
                )
            )
            assert result.error is None
        else:
            assert (
                await machine._handle_answer_question(
                    AnswerQuestion(
                        sid=ctx.key,
                        client_id="browser",
                        cmd_id="answer",
                        ask_id=pending.ask_id,
                        **values,
                    )
                )
                is None
            )
        result = await asyncio.wait_for(task, 2)
        if engine == "claude":
            assert result.updated_input["answers"] == {"选择哪个目录？": "项目目录"}
        elif engine == "codex":
            assert result == {"answers": {"directory": {"answers": ["项目目录"]}}}
        else:
            assert result == {
                "kind": "result",
                "value": {
                    "answers": [
                        {
                            "id": "directory",
                            "selected": ["项目目录"],
                        }
                    ]
                },
            }
        assert not ctx.pending_asks
        assert (await controller.snapshot("owner", room_id))["questions"] == []
        assert any(
            frame.type == "ask_user_closed"
            and frame.reason == "answered"
            and frame.ask_id == pending.ask_id
            for frame in transport.sent
        )
        messages = store.messages("owner", room_id)
        assert messages[0]["question"]["status"] == "answered"
        assert messages[1]["role"] == "user"
        assert messages[1]["reply_to"] == [messages[0]["id"]]
        assert "项目目录" in messages[1]["text"]
        store.record_native_question(member["id"], pending)
        assert len(store.messages("owner", room_id)) == 2
        assert store.messages("owner", room_id)[0]["question"]["status"] == "answered"
        spec, account = controller.prepare_member(
            CollaborationMemberSpec(engine=engine, cwd="/tmp")
        )
        store.mutate(
            "owner",
            "add-peer",
            "add_member",
            {"room_id": room_id},
            prepared_members=[(spec, account)],
        )
        peer = store.room("owner", room_id)["members"][-1]
        store.mutate(
            "owner",
            "ask-peer",
            "send",
            {
                "room_id": room_id,
                "message_id": "peer-input",
                "prompt": "按照用户刚才的回答继续",
                "targets": [peer["id"]],
            },
        )
        delivery = store.deliveries(room_id)[-1]
        assert store.claim(delivery["id"])
        delivery = store.submit(delivery["id"], "query")
        public, current = store.context(delivery)
        prompt = shared_prompt(store.room("owner", room_id), peer, public, current)
        assert "选择哪个目录？" in prompt and "回答：项目目录" in prompt
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["claude", "codex", "dsh"])
async def test_sensitive_question_answers_remain_private(engine):
    (
        machine,
        _transport,
        controller,
        store,
        room_id,
        _member,
        ctx,
    ) = await question_fixture(engine)
    task = asyncio.create_task(
        machine._on_ask(ctx, "请填写访问密钥", [], allow_text=True, secret=True)
    )
    try:
        async with asyncio.timeout(2):
            while not ctx.pending_asks:
                await asyncio.sleep(0)
        await controller.snapshot("owner", room_id)
        ask_id = next(iter(ctx.pending_asks))
        secret = "private-test-value"
        assert (
            await machine._handle_answer_question(
                AnswerQuestion(sid=ctx.key, ask_id=ask_id, answer=secret)
            )
            is None
        )
        assert await task == secret
        reopened = CollaborationStore(machine.cfg.state_dir)
        messages = reopened.messages("owner", room_id)
        assert secret not in str(messages)
        assert "请填写访问密钥" not in str(messages)
        assert messages[0]["question"]["status"] == "answered"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def publish_async_question(machine, ctx, message_id="async-question", count=1):
    from cc_remote.wrapper.codex_stream import CodexStreamTranslator

    questions = [
        {"title": f"选择目录 {index + 1}？", "options": ["项目目录", "临时目录"]}
        for index in range(count)
    ]
    frames = CodexStreamTranslator(8000).feed(
        {
            "method": "item/completed",
            "params": {
                "threadId": ctx.session_id,
                "turnId": "native-turn",
                "item": {
                    "id": message_id,
                    "type": "agentMessage",
                    "phase": "commentary",
                    "delivery": "async",
                    "text": "请确认工作目录。",
                    "questions": questions,
                },
            },
        }
    )
    for frame in frames:
        await machine._emit(ctx, frame)
    return questions


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("count", [1, 16])
async def test_async_answers_keep_the_native_envelope_and_do_not_skip_room_context(
    running, count
):
    (
        machine,
        _transport,
        controller,
        store,
        room_id,
        member,
        ctx,
    ) = await question_fixture("codex")
    questions = await publish_async_question(machine, ctx, count=count)
    message = store.messages("owner", room_id)[0]
    assert message["question"]["kind"] == "async"
    assert message["question"]["status"] == "pending"
    assert all(question["title"] in message["text"] for question in questions)
    prompt = "补充回答：\n\n" + "\n\n".join(
        f"问题：{question['title']}\n回答：项目目录" for question in questions
    )
    command = CollaborationCommand(
        operation="send",
        owner_id="owner",
        client_id="browser",
        cmd_id="answer",
        room_id=room_id,
        message_id="answer-message",
        ask_id=message["id"],
        targets=[member["id"]],
        prompt=prompt,
    )
    assert (await controller.handle(command)).error is None
    delivery = store.deliveries(room_id)[0]
    assert store.messages("owner", room_id)[0]["question"]["status"] == "submitting"
    ctx.state = "running" if running else "idle"

    async def accept(query):
        assert query.sid == ctx.key
        assert query.prompt == prompt
        await machine._emit(ctx, UserMsg(msg_id=query.msg_id, prompt=query.prompt))

    machine._handle_query = AsyncMock(side_effect=accept)
    machine._handle_steer = AsyncMock(side_effect=accept)
    machine.focused_sid = "original"
    await controller.deliver(delivery)
    assert machine._handle_steer.await_count == int(running)
    assert machine._handle_query.await_count == int(not running)
    assert machine.focused_sid == "original"
    messages = store.messages("owner", room_id)
    assert len(messages) == 2
    assert messages[0]["question"]["status"] == "answered"
    assert messages[1]["text"] == prompt
    assert store.member_by_id(member["id"])["synced_seq"] == 0
    assert (await controller.handle(command)).error is None
    assert len(store.deliveries(room_id)) == 1


@pytest.mark.asyncio
async def test_answering_an_async_question_in_the_native_surface_updates_the_room():
    (
        machine,
        _transport,
        _controller,
        store,
        room_id,
        _member,
        ctx,
    ) = await question_fixture("codex")
    questions = await publish_async_question(machine, ctx)
    prompt = f"补充回答：\n\n问题：{questions[0]['title']}\n回答：项目目录"
    await machine._emit(ctx, UserMsg(msg_id="native-answer", prompt=prompt))
    messages = store.messages("owner", room_id)
    assert messages[0]["question"]["status"] == "answered"
    assert messages[1]["text"] == prompt
    assert store.deliveries(room_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["claude", "codex", "dsh"])
async def test_failed_room_answer_write_keeps_the_native_question_open_for_retry(
    engine,
):
    (
        machine,
        _transport,
        controller,
        store,
        room_id,
        member,
        ctx,
    ) = await question_fixture(engine)
    task = asyncio.create_task(native_question(machine, ctx))
    try:
        async with asyncio.timeout(2):
            while not ctx.pending_asks:
                await asyncio.sleep(0)
        await controller.snapshot("owner", room_id)
        pending = next(iter(ctx.pending_asks.values())).event
        with store.db() as db:
            db.execute(
                "CREATE TRIGGER reject_question_answer BEFORE UPDATE OF question ON messages "
                "WHEN json_extract(NEW.question,'$.status')='answered' "
                "BEGIN SELECT RAISE(ABORT,'injected question write failure'); END"
            )
        values = (
            {"answers": ["项目目录"]} if pending.questions else {"answer": "项目目录"}
        )
        command = CollaborationCommand(
            operation="answer",
            owner_id="owner",
            client_id="browser",
            cmd_id="answer",
            room_id=room_id,
            member_id=member["id"],
            ask_id=pending.ask_id,
            **values,
        )
        assert (await controller.handle(command)).error
        assert pending.ask_id in ctx.pending_asks and not task.done()
        assert len(store.messages("owner", room_id)) == 1
        assert store.messages("owner", room_id)[0]["question"]["status"] == "pending"
        with store.db() as db:
            db.execute("DROP TRIGGER reject_question_answer")
        assert (await controller.handle(command)).error is None
        await asyncio.wait_for(task, 2)
        assert len(store.messages("owner", room_id)) == 2
        assert not ctx.pending_asks
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_slow_snapshot_does_not_reopen_an_answered_question():
    (
        machine,
        _transport,
        controller,
        store,
        room_id,
        _member,
        ctx,
    ) = await question_fixture("codex")
    task = asyncio.create_task(native_question(machine, ctx))
    snapshot = None
    try:
        async with asyncio.timeout(2):
            while not ctx.pending_asks:
                await asyncio.sleep(0)
        await controller.snapshot("owner", room_id)
        pending = next(iter(ctx.pending_asks.values())).event
        stale = copy.deepcopy(store.messages("owner", room_id))
        original = store.messages
        read, release = asyncio.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()

        def delayed_messages(*_args, **_kwargs):
            loop.call_soon_threadsafe(read.set)
            asyncio.run_coroutine_threadsafe(release.wait(), loop).result(timeout=2)
            return stale

        store.messages = delayed_messages
        snapshot = asyncio.create_task(controller.snapshot("owner", room_id))
        await asyncio.wait_for(read.wait(), 2)
        assert (
            await machine._handle_answer_question(
                AnswerQuestion(sid=ctx.key, ask_id=pending.ask_id, answer="项目目录")
            )
            is None
        )
        release.set()
        result = await snapshot
        assert result["questions"] == []
        assert result["messages"][0]["question"]["status"] == "answered"
        store.messages = original
    finally:
        if snapshot and not snapshot.done():
            snapshot.cancel()
        task.cancel()
        await asyncio.gather(
            task, *([snapshot] if snapshot else []), return_exceptions=True
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failed", "cancelled", "unknown"])
async def test_async_answer_retry_follows_the_native_delivery_outcome(outcome):
    (
        machine,
        _transport,
        controller,
        store,
        room_id,
        member,
        ctx,
    ) = await question_fixture("codex")
    questions = await publish_async_question(machine, ctx)
    message = store.messages("owner", room_id)[0]
    prompt = f"补充回答：\n\n问题：{questions[0]['title']}\n回答：项目目录"
    values = {
        "room_id": room_id,
        "targets": [member["id"]],
        "ask_id": message["id"],
        "prompt": prompt,
    }
    store.mutate("owner", "first", "send", {**values, "message_id": "first"})
    delivery = store.deliveries(room_id)[0]
    store.set_delivery(delivery["id"], outcome, error="原生投递失败")
    question = store.messages("owner", room_id)[0]["question"]
    if outcome == "unknown":
        assert question["status"] == "submitting"
        with pytest.raises(ValueError, match="确认送达"):
            store.mutate("owner", "retry", "send", {**values, "message_id": "retry"})
        assert len(store.deliveries(room_id)) == 1
    else:
        assert question["status"] == "pending"
        store.mutate("owner", "retry", "send", {**values, "message_id": "retry"})
        assert len(store.deliveries(room_id)) == 2
        assert store.messages("owner", room_id)[0]["question"]["status"] == "submitting"


@pytest.mark.asyncio
async def test_legacy_room_database_migrates_without_changing_existing_messages():
    (
        machine,
        _transport,
        _controller,
        store,
        room_id,
        member,
        _ctx,
    ) = await question_fixture("claude")
    store.publish(member["id"], "legacy-message", "原有公开回复", [])
    with store.db() as db:
        db.execute("ALTER TABLE messages DROP COLUMN question")
    reopened = CollaborationStore(machine.cfg.state_dir)
    messages = reopened.messages("owner", room_id)
    assert messages[0]["text"] == "原有公开回复"
    assert "question" not in messages[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery_write_fails", [False, True])
async def test_cached_claude_answer_recovers_a_failed_room_write(recovery_write_fails):
    from cc_remote.claude_service.client import callback_identity
    from tests.test_claude_service import environment, released

    (
        machine,
        transport,
        controller,
        store,
        room_id,
        _member,
        ctx,
    ) = await question_fixture("claude")

    async def question():
        token = callback_identity.set("same-native-question")
        try:
            return await machine._on_ask(
                ctx,
                "选择哪个目录？",
                [
                    {"label": "项目目录"},
                    {"label": "临时目录"},
                ],
            )
        finally:
            callback_identity.reset(token)

    def fail_answer_writes():
        with store.db() as db:
            db.execute(
                "CREATE TRIGGER reject_cached_answer BEFORE UPDATE OF question ON messages "
                "WHEN json_extract(NEW.question,'$.status')='answered' "
                "BEGIN SELECT RAISE(ABORT,'injected question write failure'); END"
            )

    async with environment() as (service, attach):
        first = await attach()
        ctx.sdk = SimpleNamespace(client=first, ask_server=None)
        task = asyncio.create_task(question())
        recovered = None
        try:
            async with asyncio.timeout(2):
                while not ctx.pending_asks:
                    await asyncio.sleep(0)
            ask_id = next(iter(ctx.pending_asks))
            await controller.snapshot("owner", room_id)
            fail_answer_writes()
            answer = AnswerQuestion(sid=ctx.key, ask_id=ask_id, answer="项目目录")
            assert await machine._handle_answer_question(answer)
            assert service.sessions[first.id].question_answers[ask_id] == "项目目录"
            assert len(store.messages("owner", room_id)) == 1
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await first.detach()
            await released(service.sessions[first.id])
            second = await attach()
            ctx.sdk = SimpleNamespace(client=second, ask_server=None)
            transport.sent.clear()
            if not recovery_write_fails:
                with store.db() as db:
                    db.execute("DROP TRIGGER reject_cached_answer")
            recovered = asyncio.create_task(question())
            if recovery_write_fails:
                async with asyncio.timeout(2):
                    while ask_id not in ctx.pending_asks:
                        await asyncio.sleep(0)
                assert not recovered.done()
                with store.db() as db:
                    db.execute("DROP TRIGGER reject_cached_answer")
                assert await machine._handle_answer_question(answer) is None
            assert await asyncio.wait_for(recovered, 2) == "项目目录"
            assert not ctx.pending_asks
            assert (
                bool([frame for frame in transport.sent if frame.type == "ask_user"])
                == recovery_write_fails
            )
            messages = store.messages("owner", room_id)
            assert len(messages) == 2
            assert messages[0]["question"]["status"] == "answered"
            assert "回答：项目目录" in messages[1]["text"]
        finally:
            task.cancel()
            if recovered:
                recovered.cancel()
            await asyncio.gather(
                task, *([recovered] if recovered else []), return_exceptions=True
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["claude", "codex", "dsh"])
async def test_same_account_questions_are_scoped_to_the_exact_room_member_and_session(
    engine,
):
    (
        machine,
        _transport,
        controller,
        store,
        room_id,
        member,
        first,
    ) = await question_fixture(engine)
    spec, account = controller.prepare_member(CollaborationMemberSpec(**member["spec"]))
    native = first.session_id.replace("11111111", "22222222")
    sid = first.key.replace("11111111", "22222222")
    store.mutate(
        "owner",
        "add-session",
        "add_member",
        {"room_id": room_id},
        prepared_members=[
            (spec, account, {"sid": sid, "native_sid": native, "cwd": "/tmp"}),
        ],
    )
    second_member = store.room("owner", room_id)["members"][-1]
    second = _mk_ctx(sid, native)
    second.engine, second.space, second.cwd, second.state = (
        engine,
        "code",
        "/tmp",
        "running",
    )
    second.sdk = SimpleNamespace(native_id=native)
    machine.sessions[sid] = second
    controller.watch(second, second_member["id"])
    tasks = [
        asyncio.create_task(native_question(machine, ctx)) for ctx in (first, second)
    ]
    try:
        async with asyncio.timeout(2):
            while not first.pending_asks or not second.pending_asks:
                await asyncio.sleep(0)
        pending = next(iter(second.pending_asks.values())).event
        values = (
            {"answers": ["临时目录"]} if pending.questions else {"answer": "临时目录"}
        )
        command = {
            "operation": "answer",
            "owner_id": "owner",
            "client_id": "browser",
            "cmd_id": "reply",
            "room_id": room_id,
            "member_id": second_member["id"],
            "ask_id": pending.ask_id,
            **values,
        }
        foreign = CollaborationCommand(**{**command, "owner_id": "foreign-owner"})
        assert (await controller.handle(foreign)).error
        if pending.ask_id not in first.pending_asks:
            wrong_member = CollaborationCommand(
                **{**command, "member_id": member["id"]}
            )
            assert (await controller.handle(wrong_member)).error
        missing = CollaborationCommand(**{**command, "ask_id": "missing-question"})
        assert (await controller.handle(missing)).error
        assert first.pending_asks and second.pending_asks
        assert (await controller.handle(CollaborationCommand(**command))).error is None
        await asyncio.wait_for(tasks[1], 2)
        assert first.pending_asks and not tasks[0].done()
        assert not second.pending_asks
        messages = store.messages("owner", room_id)
        answered = [
            message
            for message in messages
            if message.get("question", {}).get("status") == "answered"
        ]
        assert [message["member_id"] for message in answered] == [second_member["id"]]
        duplicate = await controller.handle(
            CollaborationCommand(**{**command, "cmd_id": "duplicate"})
        )
        assert duplicate.error
        assert (
            len(
                [
                    message
                    for message in store.messages("owner", room_id)
                    if message["role"] == "user"
                ]
            )
            == 1
        )
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", ["foreign-owner", "wrong-member", "wrong-question", "different-prompt"]
)
async def test_async_answer_validation_keeps_other_members_questions_untouched(invalid):
    (
        machine,
        _transport,
        _controller,
        store,
        room_id,
        member,
        ctx,
    ) = await question_fixture("codex")
    await publish_async_question(machine, ctx)
    question = store.messages("owner", room_id)[0]
    body = {
        "room_id": room_id,
        "message_id": "answer",
        "ask_id": question["id"],
        "targets": [member["id"]],
        "prompt": "补充回答：\n\n问题：选择目录 1？\n回答：项目目录",
    }
    if invalid == "wrong-member":
        body["targets"] = ["another-member"]
    elif invalid == "wrong-question":
        body["ask_id"] = "another-question"
    elif invalid == "different-prompt":
        body["prompt"] = "补充回答：\n\n问题：其他问题？\n回答：项目目录"
    with pytest.raises(ValueError):
        store.mutate(
            "foreign-owner" if invalid == "foreign-owner" else "owner",
            "answer",
            "send",
            body,
        )
    assert store.deliveries(room_id) == []
    assert len(store.messages("owner", room_id)) == 1
    assert store.messages("owner", room_id)[0]["question"]["status"] == "pending"


@pytest.mark.asyncio
async def test_ambiguous_native_async_answers_do_not_guess_which_question_was_answered():
    (
        machine,
        _transport,
        _controller,
        store,
        room_id,
        _member,
        ctx,
    ) = await question_fixture("codex")
    await publish_async_question(machine, ctx, "question-one")
    await publish_async_question(machine, ctx, "question-two")
    prompt = "补充回答：\n\n问题：选择目录 1？\n回答：项目目录"
    await machine._emit(ctx, UserMsg(msg_id="native-answer", prompt=prompt))
    messages = store.messages("owner", room_id)
    assert len(messages) == 2
    assert all(message["question"]["status"] == "pending" for message in messages)


@pytest.mark.asyncio
async def test_an_explicit_async_question_reference_survives_identical_question_text():
    (
        machine,
        _transport,
        controller,
        store,
        room_id,
        member,
        ctx,
    ) = await question_fixture("codex")
    await publish_async_question(machine, ctx, "question-one")
    await publish_async_question(machine, ctx, "question-two")
    first, _second = store.messages("owner", room_id)
    prompt = (
        '补充回答：\n\n问题 ID："question-one"\n\n问题：选择目录 1？\n回答：项目目录'
    )
    store.mutate(
        "owner",
        "answer",
        "send",
        {
            "room_id": room_id,
            "message_id": "answer",
            "ask_id": first["id"],
            "targets": [member["id"]],
            "prompt": prompt,
        },
    )

    async def accept(query):
        assert query.prompt == prompt
        await machine._emit(ctx, UserMsg(msg_id=query.msg_id, prompt=query.prompt))

    ctx.state = "idle"
    machine._handle_query = AsyncMock(side_effect=accept)
    await controller.deliver(store.deliveries(room_id)[0])
    messages = store.messages("owner", room_id)
    assert [message["question"]["status"] for message in messages[:2]] == [
        "answered",
        "pending",
    ]
    assert len(messages) == 3


@pytest.mark.asyncio
async def test_an_old_answered_async_question_still_counts_for_native_ambiguity():
    (
        machine,
        _transport,
        _controller,
        store,
        room_id,
        _member,
        ctx,
    ) = await question_fixture("codex")
    await publish_async_question(machine, ctx, "question-one")
    prompt = "补充回答：\n\n问题：选择目录 1？\n回答：项目目录"
    await machine._emit(ctx, UserMsg(msg_id="first-answer", prompt=prompt))
    await publish_async_question(machine, ctx, "question-two")
    await machine._emit(ctx, UserMsg(msg_id="ambiguous-answer", prompt=prompt))
    messages = store.messages("owner", room_id)
    assert len(messages) == 3
    assert messages[0]["question"]["status"] == "answered"
    assert messages[2]["question"]["status"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["native", "async"])
async def test_question_receipts_stay_visible_after_the_question_leaves_the_latest_page(
    kind,
):
    (
        machine,
        _transport,
        controller,
        store,
        room_id,
        member,
        ctx,
    ) = await question_fixture("codex")
    task = None
    try:
        if kind == "native":
            task = asyncio.create_task(native_question(machine, ctx))
            async with asyncio.timeout(2):
                while not ctx.pending_asks:
                    await asyncio.sleep(0)
            await controller.snapshot("owner", room_id)
        else:
            await publish_async_question(machine, ctx)
        question = store.messages("owner", room_id)[0]
        for index in range(45):
            store.publish(member["id"], f"reply-{index}", "后续公开消息", [])
        if kind == "native":
            ask_id = next(iter(ctx.pending_asks))
            assert (
                await machine._handle_answer_question(
                    AnswerQuestion(
                        sid=ctx.key,
                        ask_id=ask_id,
                        answer="项目目录",
                    )
                )
                is None
            )
            await asyncio.wait_for(task, 2)
        else:
            await machine._emit(
                ctx,
                UserMsg(
                    msg_id="native-answer",
                    prompt="补充回答：\n\n问题：选择目录 1？\n回答：项目目录",
                ),
            )
        snapshot = await controller.snapshot("owner", room_id)
        assert question["id"] not in [message["id"] for message in snapshot["messages"]]
        assert snapshot["question_states"][question["id"]]["status"] == "answered"
        assert snapshot["has_more"]
        assert len(snapshot["messages"]) == 40
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failed", "cancelled"])
async def test_failed_native_async_answer_turn_can_be_retried_after_acceptance(outcome):
    (
        machine,
        _transport,
        controller,
        store,
        room_id,
        member,
        ctx,
    ) = await question_fixture("codex")
    await publish_async_question(machine, ctx)
    question = store.messages("owner", room_id)[0]
    prompt = (
        '补充回答：\n\n问题 ID："async-question"\n\n问题：选择目录 1？\n回答：项目目录'
    )
    body = {
        "room_id": room_id,
        "message_id": "answer",
        "ask_id": question["id"],
        "targets": [member["id"]],
        "prompt": prompt,
    }
    store.mutate("owner", "answer", "send", body)

    async def accept(query):
        await machine._emit(ctx, UserMsg(msg_id=query.msg_id, prompt=query.prompt))

    ctx.state = "idle"
    machine._handle_query = AsyncMock(side_effect=accept)
    await controller.deliver(store.deliveries(room_id)[0])
    assert store.messages("owner", room_id)[0]["question"]["status"] == "answered"
    store.finish(member["id"], outcome, error="原生回答回合未完成")
    snapshot = await controller.snapshot("owner", room_id)
    assert snapshot["messages"][0]["question"]["status"] == "pending"
    assert snapshot["question_states"][question["id"]]["status"] == "pending"
    store.mutate("owner", "retry", "send", {**body, "message_id": "retry"})
    assert len(store.deliveries(room_id)) == 2


@pytest.mark.asyncio
async def test_a_native_async_answer_cancels_an_unsubmitted_room_answer():
    (
        machine,
        _transport,
        controller,
        store,
        room_id,
        member,
        ctx,
    ) = await question_fixture("codex")
    await publish_async_question(machine, ctx)
    question = store.messages("owner", room_id)[0]
    prompt = (
        '补充回答：\n\n问题 ID："async-question"\n\n问题：选择目录 1？\n回答：项目目录'
    )
    store.mutate(
        "owner",
        "answer",
        "send",
        {
            "room_id": room_id,
            "message_id": "answer",
            "ask_id": question["id"],
            "targets": [member["id"]],
            "prompt": prompt,
        },
    )
    delivery = store.deliveries(room_id)[0]
    await machine._emit(
        ctx,
        UserMsg(msg_id="native-answer", prompt=prompt.replace("项目目录", "临时目录")),
    )
    assert store.delivery(delivery["id"])["status"] == "cancelled"
    machine._handle_query = AsyncMock()
    await controller.deliver(delivery)
    machine._handle_query.assert_not_awaited()
    public = store.messages("owner", room_id)[0]["question"]
    assert public["status"] == "answered" and public["answers"] == ["临时目录"]
