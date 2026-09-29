"""A rejected send is private command feedback, never a failed engine turn."""

import asyncio

import pytest

from cc_remote.protocol import CommandAck, Error, Query, StateEvent
from tests.test_claude_autocompact import (
    SESSION_ID, _AutoCompactSdk, _machine_with_sdk,
)


@pytest.mark.parametrize("race", ["running", "notification", "preflight"])
def test_rejected_query_is_private_and_reliable_without_touching_active_turn(race):
    async def run():
        machine, transport, ctx = _machine_with_sdk(_AutoCompactSdk())
        ctx.active_msg_id = "original-task"
        if race == "running":
            ctx.state = "running"
        elif race == "notification":
            ctx.claude_background_followup_pending = True
        else:
            async def ownership(_sid):
                ctx.claude_background_followup_pending = True
                return False

            machine._prime_claude_ownership = ownership

        await machine._emit(ctx, StateEvent(
            state=ctx.state, msg_id="original-task"))
        original_seq = ctx.buffer.tail_seq
        command = Query(sid=SESSION_ID, msg_id="rejected-message",
                        prompt="new instruction", client_id="sender",
                        cmd_id="send-command")
        await machine._process_command(command)
        assert ctx.state == ("running" if race == "running" else "idle")
        assert ctx.active_msg_id == "original-task"
        # Lost ACK: a reliable retry must replay the rejection privately, never
        # rerun the query after the original background response becomes idle.
        ctx.state = "idle"
        ctx.claude_background_followup_pending = False
        await machine._process_command(command)

        errors = [event for event in transport.sent if isinstance(event, Error)]
        assert len(errors) == 2
        assert all(event.code == "busy" for event in errors)
        assert all(event.to == "sender" for event in errors)
        assert all(event.request_id == "send-command" for event in errors)
        assert all(event.msg_id == "rejected-message" for event in errors)
        assert all(event.sid == SESSION_ID for event in errors)
        assert ctx.active_msg_id == "original-task"
        assert ctx.turn_task is None
        assert ctx.buffer.tail_seq == original_seq
        assert not any(isinstance(event, Error) for event in ctx.buffer.replay_from(
            0, cc_session_id=SESSION_ID, state=ctx.state, rebuild=True))
        assert len([e for e in transport.sent if isinstance(e, CommandAck)]) == 2

    asyncio.run(run())


def test_missing_session_query_rejection_stays_correlated_to_sender():
    async def run():
        machine, transport, _ctx = _machine_with_sdk(_AutoCompactSdk())
        result = await machine._handle_query(Query(
            sid="missing-session", msg_id="unaccepted-message", prompt="hi",
            client_id="sender", cmd_id="send-command"))
        assert isinstance(result, Error)
        assert result.code == "not_running"
        assert result.to == "sender"
        assert result.request_id == "send-command"
        assert result.sid == "missing-session"
        assert result.msg_id == "unaccepted-message"
        assert transport.sent[-1] is result

    asyncio.run(run())
