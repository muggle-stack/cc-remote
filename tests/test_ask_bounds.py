"""Bounds for model-originated ask_user payloads and client answers."""
from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from cc_remote.protocol import (
    ASK_ANSWER_MAX_CHARS,
    ASK_OPTION_DESCRIPTION_MAX_CHARS,
    ASK_OPTION_LABEL_MAX_CHARS,
    ASK_OPTION_MAX_COUNT,
    ASK_QUESTION_MAX_CHARS,
    AnswerQuestion,
    AskUser,
    UserMsg,
)
from cc_remote.wrapper.ask import _normalize_ask_arguments
from tests.test_multisession import _mk_ctx, _mk_machine


def test_protocol_bounds_question_options_and_answer():
    valid_options = [{"label": "one"}, {"label": "two", "ds": "details"}]
    assert AskUser(
        ask_id="ask-1", question="pick", options=valid_options).options == valid_options

    invalid = [
        {"ask_id": "ask-1", "question": "x" * (ASK_QUESTION_MAX_CHARS + 1),
         "options": valid_options},
        {"ask_id": "ask-1", "question": "pick", "options": [{"label": "only"}]},
        {"ask_id": "ask-1", "question": "pick",
         "options": [{"label": str(i)} for i in range(ASK_OPTION_MAX_COUNT + 1)]},
        {"ask_id": "ask-1", "question": "pick",
         "options": [{"label": "x" * (ASK_OPTION_LABEL_MAX_CHARS + 1)},
                     {"label": "two"}]},
        {"ask_id": "ask-1", "question": "pick",
         "options": [{"label": "one", "ds": "x" * (
             ASK_OPTION_DESCRIPTION_MAX_CHARS + 1)}, {"label": "two"}]},
        {"ask_id": "ask-1", "question": "pick",
         "options": [{"label": "one", "extra": "x"}, {"label": "two"}]},
    ]
    for payload in invalid:
        with pytest.raises(ValidationError):
            AskUser(**payload)

    with pytest.raises(ValidationError):
        AnswerQuestion(
            ask_id="ask-1", answer="x" * (ASK_ANSWER_MAX_CHARS + 1))


@pytest.mark.parametrize(
    "arguments,error_fragment",
    [
        ({"question": "", "options": [{"label": "a"}, {"label": "b"}]},
         "question"),
        ({"question": "q", "options": [{"label": "a"}]}, "2-5 options"),
        ({"question": "q", "options": [
            {"label": "a", "extra": True}, {"label": "b"}]}, "invalid option"),
        ({"question": "q", "options": [
            {"label": "x" * (ASK_OPTION_LABEL_MAX_CHARS + 1)},
            {"label": "b"}]}, "option label"),
    ],
)
def test_mcp_handler_rejects_invalid_model_arguments(arguments, error_fragment):
    question, options, error = _normalize_ask_arguments(arguments)
    assert question is None and options is None
    assert error is not None and error_fragment in error


def test_mcp_handler_normalizes_a_valid_ask():
    question, options, error = _normalize_ask_arguments({
        "question": "Choose",
        "options": [{"label": "A", "ds": "first"}, {"label": "B", "ds": ""}],
    })
    assert error is None and question == "Choose"
    assert options == [{"label": "A", "ds": "first"}, {"label": "B"}]


def test_machine_does_not_leak_pending_future_on_invalid_ask():
    async def run():
        machine, _ = _mk_machine()
        ctx = _mk_ctx("sid-1", "sid-1")
        with pytest.raises(ValidationError):
            await machine._on_ask(
                ctx,
                "x" * (ASK_QUESTION_MAX_CHARS + 1),
                [{"label": "one"}, {"label": "two"}],
            )
        assert ctx.pending_asks == {}

    asyncio.run(run())


def test_machine_ask_identity_does_not_consume_a_wire_sequence():
    async def run():
        machine, transport = _mk_machine()
        ctx = _mk_ctx("sid-1", "sid-1")
        await machine._emit(ctx, UserMsg(msg_id="m1", prompt="hello"))

        task = asyncio.create_task(machine._on_ask(
            ctx,
            "Choose",
            [{"label": "A"}, {"label": "B"}],
        ))
        while (not transport.sent
               or transport.sent[-1].type != "ask_user"):
            await asyncio.sleep(0)

        ask = transport.sent[-1]
        assert ask.type == "ask_user"
        assert ask.seq == 2
        assert ask.ask_id.startswith("ask-") and len(ask.ask_id) == 36

        replay = ctx.buffer.replay_from(
            1, cc_session_id="sid-1", state="running", generation="g")
        assert replay[0].from_seq == 2
        assert replay[0].truncated is False

        ctx.pending_asks[ask.ask_id].set_result("A")
        assert await task == "A"

    asyncio.run(run())
